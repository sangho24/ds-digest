"""구독자 명단, 수신자별 렌더, 구독자별 선정과 발송, 파이프라인 통합 검증.

실제 Supabase·Resend·SMTP 로는 아무것도 나가지 않는다. app.db 의 구독자 함수와
resend.Emails.send 를 가짜로 바꾼다(conftest 가 service role 클라이언트 생성도 막는다).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
import resend
import structlog

import app.analyzer as analyzer
import app.db as db
import app.interest_model as interest_model
import app.jobs.daily_digest as job
import app.jobs.send_subscribers as send_job
import app.newsletter as newsletter
import app.subscriber_delivery as delivery
import app.subscribers as subscribers
from app.config import get_settings
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    RawContent,
    SourceType,
)
from app.pool import ScoredCandidate, save_pool, snapshot_pool
from app.subscribers import Subscriber

ENDPOINT = "https://proj.supabase.co/functions/v1/digest-feedback"
DATE = "2026-11-16"
OWNER = Subscriber(id="00000000-owner", email="me@x.com", token="tok-owner-AAAA", is_owner=True)
ALICE = Subscriber(id="11111111-alice", email="alice@x.com", token="tok-alice-BBBB")
BOB = Subscriber(id="22222222-bob", email="bob@x.com", token="tok-bob-CCCC")


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch, **env: str) -> None:
    """관련 env 를 전부 명시한다(개발자 .env 값이 새지 않게)."""
    values = {
        "EMAIL_PROVIDER": "",
        "RESEND_API_KEY": "re_test",
        "EMAIL_FROM": "digest@test.local",
        "EMAIL_TO": "me@x.com",
        "ALERT_EMAIL_TO": "",
        "DRY_RUN": "false",
        "SMTP_HOST": "",
        "SMTP_PORT": "",
        "SUPABASE_URL": "https://proj.supabase.co",
        "SUPABASE_KEY": "",
        "SUPABASE_SERVICE_KEY": "service-key",
        "DELIVERY_CHANNELS": "email",
        "DISCORD_BOT_TOKEN": "",
        "DISCORD_CHANNEL_ID": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        # 빈 값은 기본값(자리 1, 클릭 기록 켬)으로 되돌아간다.
        "EFFECT_HOLDOUT_SLOTS": "",
        "EFFECT_HOLDOUT_RATE": "",
        "TRACK_CLICKS": "",
    }
    values.update(env)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _item(n: int, score: int = 8) -> DigestItem:
    return DigestItem(
        raw=RawContent(
            source_type=SourceType.RSS,
            source_name=f"src{n}",
            source_key=f"src{n}",
            title=f"아이템 {n}",
            url=f"https://example.com/post?id={n}&ref=rss",
        ),
        analysis=ContentAnalysis(
            relevance_score=score,
            one_line_summary=f"요약 {n}",
            tags=[f"tag{n}"],
            evidence_level=EvidenceLevel.FULL,
        ),
    )


def _pool(count: int = 5) -> list[ScoredCandidate]:
    """점수가 10, 9, 8, ... 로 내려가는 후보. 출처가 전부 달라 다양성 감점이 없다."""
    return [
        ScoredCandidate(item=_item(n, score=10 - n), absolute_score=10 - n)
        for n in range(count)
    ]


@pytest.fixture
def selection(monkeypatch):
    """선정이 개발자 컴퓨터의 설정·퀴즈 기록에 기대지 않게 고정한다(정원 2건)."""
    monkeypatch.setattr(
        analyzer,
        "get_settings",
        lambda: SimpleNamespace(
            relevance_floor=4,
            max_items_per_digest=2,
            max_items_per_source=2,
            source_diversity_strength=1.0,
        ),
    )
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())


class FakeTables:
    """app.db 의 구독자 함수 대역. sent_items 를 메모리에 둔다."""

    def __init__(self, monkeypatch, rows: list[Subscriber] | None = None) -> None:
        self.rows = rows or []
        self.sent: dict[str, dict[str, str]] = {}
        self.events: list[dict[str, Any]] = []
        self.record_calls: list[tuple[str, list[str], str]] = []
        # 구독자 id → 마지막 기록의 비교군 목록(urls 와 같은 순서). arms 없이 불리면 None.
        self.arms: dict[str, list[str | None] | None] = {}
        monkeypatch.setattr(db, "fetch_subscribers", self.fetch_subscribers)
        monkeypatch.setattr(db, "fetch_sent_item_urls", self.fetch_sent_item_urls)
        monkeypatch.setattr(db, "has_sent_on_date", self.has_sent_on_date)
        monkeypatch.setattr(db, "record_sent_items", self.record_sent_items)
        monkeypatch.setattr(db, "fetch_feedback_events", self.fetch_feedback_events)

    def fetch_subscribers(self, active_only: bool = False) -> list[dict[str, Any]]:
        rows = [s.model_dump() for s in self.rows]
        return [r for r in rows if r["status"] == "active"] if active_only else rows

    def fetch_sent_item_urls(self, subscriber_id: str, urls: list[str]) -> set[str]:
        return set(self.sent.get(subscriber_id, {})) & set(urls)

    def has_sent_on_date(self, subscriber_id: str, digest_date: str) -> bool:
        return digest_date in self.sent.get(subscriber_id, {}).values()

    def record_sent_items(
        self,
        subscriber_id: str,
        urls: list[str],
        digest_date: str,
        arms: list[str | None] | None = None,
    ) -> bool:
        self.record_calls.append((subscriber_id, list(urls), digest_date))
        self.arms[subscriber_id] = list(arms) if arms is not None else None
        self.sent.setdefault(subscriber_id, {}).update({u: digest_date for u in urls})
        return True

    def fetch_feedback_events(self, subscriber_ids: list[str]) -> list[dict[str, Any]]:
        return [e for e in self.events if e["subscriber_id"] in subscriber_ids]


def _record_resend(monkeypatch, fail_for: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _send(params: dict[str, Any]) -> dict[str, str]:
        calls.append(params)
        if params["to"][0] in fail_for:
            raise RuntimeError(f"rejected: {params['to']}")
        return {"id": f"msg-{len(calls)}"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_send))
    return calls


def _to(calls: list[dict[str, Any]]) -> list[str]:
    return [c["to"][0] for c in calls]


# ── 명단과 CLI ──────────────────────────────────────────────────────────


def test_user_id_rule():
    """is_owner 는 기존 단일 사용자 키("default"), 나머지는 subscriber.id."""
    assert OWNER.user_id == "default"
    assert ALICE.user_id == ALICE.id


def test_masking_helpers():
    assert subscribers.mask_email("alice@x.com") == "a***@x.com"
    assert subscribers.mask_email("이상한값") == "***"
    assert subscribers.mask_token("tok-alice-BBBB") == "tok-…"


def test_list_active_is_empty_without_supabase(monkeypatch):
    _configure(monkeypatch, SUPABASE_URL="", SUPABASE_SERVICE_KEY="")
    FakeTables(monkeypatch, [ALICE])
    assert subscribers.list_active_subscribers() == []


def test_list_active_is_fail_open_when_fetch_fails(monkeypatch):
    """conftest 가 클라이언트 생성을 막아 두었다. 조회 실패는 빈 목록이다."""
    _configure(monkeypatch)
    with structlog.testing.capture_logs() as logs:
        assert subscribers.list_active_subscribers() == []
    assert [e for e in logs if e["event"] == "subscribers_fetch_failed"]


def test_list_active_skips_unsubscribed_and_broken_rows(monkeypatch):
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch, [ALICE, BOB.model_copy(update={"status": "unsubscribed"})])
    monkeypatch.setattr(
        db, "fetch_subscribers",
        lambda active_only=False: tables.fetch_subscribers() + [{"id": "x", "email": "no-token@x.com"}],
    )
    assert [s.email for s in subscribers.list_active_subscribers()] == ["alice@x.com"]


def test_add_generates_token_and_normalizes_email(monkeypatch):
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    inserted: list[dict[str, Any]] = []

    def _insert(row: dict[str, Any]) -> dict[str, Any]:
        inserted.append(row)
        return {**row, "id": "33333333-new"}

    monkeypatch.setattr(db, "insert_subscriber", _insert)

    first = subscribers.add_subscriber("  New@X.com ", name="새 사람")
    second = subscribers.add_subscriber("other@x.com", is_owner=True)

    assert first is not None and first.email == "new@x.com" and first.name == "새 사람"
    assert second is not None and second.is_owner and second.user_id == "default"
    tokens = [row["token"] for row in inserted]
    assert all(len(t) >= 32 for t in tokens) and len(set(tokens)) == 2
    assert inserted[0]["status"] == "active" and inserted[0]["is_owner"] is False


def test_add_existing_reactivates_and_keeps_token(monkeypatch):
    _configure(monkeypatch)
    FakeTables(monkeypatch, [BOB.model_copy(update={"status": "unsubscribed"})])
    updates: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(db, "update_subscriber", lambda sid, fields: updates.append((sid, fields)) or True)
    monkeypatch.setattr(db, "insert_subscriber", lambda row: pytest.fail("새로 넣으면 안 된다"))

    again = subscribers.add_subscriber("bob@x.com")

    assert again is not None and again.token == BOB.token and again.status == "active"
    assert updates == [(BOB.id, {"status": "active", "unsubscribed_at": None})]


def test_cli_list_masks_tokens(monkeypatch, capsys):
    _configure(monkeypatch)
    FakeTables(monkeypatch, [OWNER, ALICE])

    assert subscribers.main(["list"]) == 0

    out = capsys.readouterr().out
    assert "alice@x.com" in out and "합계 2명" in out
    assert "tok-…" in out
    assert ALICE.token not in out and OWNER.token not in out


def test_cli_remove_marks_unsubscribed(monkeypatch, capsys):
    _configure(monkeypatch)
    FakeTables(monkeypatch, [ALICE])
    updates: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(db, "update_subscriber", lambda sid, fields: updates.append((sid, fields)) or True)

    assert subscribers.main(["remove", "ALICE@x.com"]) == 0
    assert updates[0][0] == ALICE.id and updates[0][1]["status"] == "unsubscribed"
    assert updates[0][1]["unsubscribed_at"]
    assert subscribers.main(["remove", "nobody@x.com"]) == 1


def test_cli_requires_supabase(monkeypatch, capsys):
    _configure(monkeypatch, SUPABASE_URL="", SUPABASE_SERVICE_KEY="")
    assert subscribers.main(["list"]) == 1


# ── 수신자별 링크와 헤더 ─────────────────────────────────────────────────


def test_default_render_has_no_subscriber_links(monkeypatch):
    """기존 EMAIL_TO 경로의 렌더에는 피드백·수신거부 링크가 없다."""
    _configure(monkeypatch)
    html = newsletter.render_digest_email([_item(1), _item(2)], date_str="2026년 11월 16일")
    assert "feedback.html" not in html
    assert "읽고 싶다" not in html and "수신거부" not in html


def test_recipient_render_has_per_item_links_and_unsubscribe(monkeypatch):
    _configure(monkeypatch)
    items = [_item(1), _item(2)]

    _subject, html, headers = delivery.render_for_subscriber(ALICE, items, DATE)

    page = f"{newsletter.ARCHIVE_BASE}/feedback.html"
    for item in items:
        encoded = quote(item.raw.url, safe="")
        assert "&" not in encoded and "?" not in encoded
        for action in ("want", "skip"):
            link = f"{page}?t={ALICE.token}&a={action}&u={encoded}&d={DATE}"
            # href 안에서는 & 가 &amp; 로 이스케이프된다.
            assert f'href="{link.replace("&", "&amp;")}"' in html
    assert html.count("읽고 싶다") == 2 and html.count("아니다") == 2
    assert f'href="{page}?t={ALICE.token}&amp;a=unsubscribe"' in html
    assert BOB.token not in html

    assert headers == {
        "List-Unsubscribe": f"<{ENDPOINT}?t={ALICE.token}&a=unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert "mailto:" not in headers["List-Unsubscribe"]


def test_link_block_follows_email_template_rules(monkeypatch):
    """추가한 블록도 테이블 + 인라인 스타일 관례를 지킨다(margin, flex, style 안 큰따옴표 금지)."""
    _configure(monkeypatch)
    plain = newsletter.render_digest_email([_item(1)], date_str="d")
    linked = delivery.render_for_subscriber(ALICE, [_item(1)], DATE)[1]
    added = "\n".join(line for line in linked.splitlines() if line not in set(plain.splitlines()))
    assert "fb-want" in added and "fb-skip" in added
    assert "margin" not in added and "flex" not in added and "<div" not in added


def test_no_unsubscribe_header_without_supabase_url(monkeypatch):
    _configure(monkeypatch, SUPABASE_URL="")
    assert newsletter.unsubscribe_headers("tok") == {}


def test_feedback_config_holds_only_the_endpoint(monkeypatch, tmp_path: Path):
    _configure(monkeypatch)
    path = delivery.write_feedback_config(tmp_path)
    assert path == tmp_path / "feedback-config.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"endpoint": ENDPOINT}
    assert "service-key" not in path.read_text(encoding="utf-8")


def test_feedback_config_not_written_without_supabase_url(monkeypatch, tmp_path: Path):
    _configure(monkeypatch, SUPABASE_URL="")
    assert delivery.write_feedback_config(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


# ── 구독자별 선정과 발송 ─────────────────────────────────────────────────


def test_each_subscriber_gets_own_mail_and_sent_items(monkeypatch, selection):
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    pool = _pool()

    summary = delivery.deliver_to_subscribers([ALICE, BOB], pool, DATE)

    assert summary.as_dict()["sent"] == 2 and summary.failed == 0
    assert _to(calls) == ["alice@x.com", "bob@x.com"]
    assert ALICE.token in calls[0]["html"] and BOB.token not in calls[0]["html"]
    assert calls[0]["headers"]["List-Unsubscribe"] == f"<{ENDPOINT}?t={ALICE.token}&a=unsubscribe>"
    assert calls[1]["headers"]["List-Unsubscribe"] == f"<{ENDPOINT}?t={BOB.token}&a=unsubscribe>"
    top_two = [pool[0].item.raw.url, pool[1].item.raw.url]
    assert tables.record_calls == [(ALICE.id, top_two, DATE), (BOB.id, top_two, DATE)]


def test_already_received_urls_are_excluded(monkeypatch, selection):
    """sent_items 에 있는 url 은 그 구독자의 후보에서만 빠진다."""
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch)
    pool = _pool()
    tables.sent[ALICE.id] = {pool[0].item.raw.url: "2026-11-15"}
    calls = _record_resend(monkeypatch)

    delivery.deliver_to_subscribers([ALICE, BOB], pool, DATE)

    assert "아이템 0" not in calls[0]["html"]
    assert "아이템 1" in calls[0]["html"] and "아이템 2" in calls[0]["html"]
    assert "아이템 0" in calls[1]["html"] and "아이템 2" not in calls[1]["html"]


def test_one_failure_does_not_block_other_subscribers(monkeypatch, selection):
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch, fail_for=frozenset({"alice@x.com"}))

    with structlog.testing.capture_logs() as logs:
        summary = delivery.deliver_to_subscribers([ALICE, BOB], _pool(), DATE)

    assert _to(calls) == ["alice@x.com", "bob@x.com"]
    assert [o.status for o in summary.outcomes] == ["failed", "sent"]
    # 실패한 구독자는 sent_items 에 남지 않는다(내일 다시 후보가 된다).
    assert [call[0] for call in tables.record_calls] == [BOB.id]
    # 주소(가린 형태 포함)와 token 은 로그에 남지 않는다. 식별은 id 앞 8자뿐이다.
    dumped = json.dumps(logs, ensure_ascii=False, default=str)
    assert "@" not in dumped
    assert ALICE.token not in dumped and BOB.token not in dumped
    assert ALICE.short_id in dumped
    assert summary.outcomes[0].error and "@" not in summary.outcomes[0].error


def test_exception_inside_one_subscriber_is_contained(monkeypatch, selection):
    """발송부 밖(선정, 렌더)에서 예외가 나도 다음 구독자는 받는다."""
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    real_render = delivery.render_for_subscriber

    def _render(subscriber: Subscriber, items: list[DigestItem], date_iso: str):
        if subscriber.id == ALICE.id:
            raise ValueError("템플릿 오류")
        return real_render(subscriber, items, date_iso)

    monkeypatch.setattr(delivery, "render_for_subscriber", _render)

    summary = delivery.deliver_to_subscribers([ALICE, BOB], _pool(), DATE)

    assert [o.status for o in summary.outcomes] == ["failed", "sent"]
    assert _to(calls) == ["bob@x.com"]


def test_sent_items_lookup_failure_is_fail_open(monkeypatch, selection):
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    monkeypatch.setattr(db, "fetch_sent_item_urls", lambda sid, urls: None)
    monkeypatch.setattr(db, "has_sent_on_date", lambda sid, d: None)
    calls = _record_resend(monkeypatch)

    summary = delivery.deliver_to_subscribers([ALICE], _pool(), DATE)

    assert summary.as_dict()["sent"] == 1 and len(calls) == 1


def test_same_date_rerun_does_not_send_twice(monkeypatch, selection):
    """재실행은 이미 받은 사람을 건너뛴다. 남은 후보로 두 번째 메일을 만들지 않는다."""
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    pool = _pool()

    delivery.deliver_to_subscribers([ALICE], pool, DATE)
    second = delivery.deliver_to_subscribers([ALICE, BOB], pool, DATE)

    assert _to(calls) == ["alice@x.com", "bob@x.com"]
    assert [o.status for o in second.outcomes] == ["already_sent", "sent"]


def test_owner_gets_the_operator_selection_not_a_new_one(monkeypatch, selection):
    """is_owner 는 선정을 새로 계산하지 않는다. 풀 상위가 아니어도 운영자 목록 그대로다."""
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    pool = _pool()
    owner_items = [pool[3].item, pool[4].item]

    summary = delivery.deliver_to_subscribers([OWNER], pool, DATE, owner_items=owner_items)

    assert summary.as_dict()["sent"] == 1
    assert "아이템 3" in calls[0]["html"] and "아이템 4" in calls[0]["html"]
    assert "아이템 0" not in calls[0]["html"]
    assert OWNER.token in calls[0]["html"]
    assert tables.record_calls == [(OWNER.id, [i.raw.url for i in owner_items], DATE)]
    # 같은 후보로 재현되지 않는 목록이라 비교군은 붙지 않는다(목록은 그대로).
    assert tables.arms[OWNER.id] is None


def test_owner_already_delivered_is_not_sent_again(monkeypatch, selection):
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    pool = _pool()

    summary = delivery.deliver_to_subscribers(
        [OWNER, ALICE], pool, DATE, owner_items=[pool[0].item], already_delivered={"Me@X.com"}
    )

    assert _to(calls) == ["alice@x.com"]
    assert [o.status for o in summary.outcomes] == ["already_sent", "sent"]
    assert tables.record_calls[0] == (OWNER.id, [pool[0].item.raw.url], DATE)


def test_empty_pool_skips_non_owner(monkeypatch, selection):
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)

    summary = delivery.deliver_to_subscribers([ALICE], [], DATE)

    assert calls == [] and [o.status for o in summary.outcomes] == ["no_items"]


# ── send_digest 의 운영자 주소 처리 ───────────────────────────────────────


def test_send_digest_owner_address_gets_linked_mail_once(monkeypatch):
    """EMAIL_TO 의 운영자 주소는 링크 든 메일 한 통만, 다른 주소는 기존 모습 그대로."""
    _configure(monkeypatch, EMAIL_TO="Me@x.com, friend@x.com")
    calls = _record_resend(monkeypatch)
    delivered: set[str] = set()
    items = [_item(1)]

    ok = asyncio.run(
        newsletter.send_digest(items, date_iso=DATE, owners={"me@x.com": OWNER.token},
                               owner_delivered=delivered)
    )

    assert ok is True and _to(calls) == ["Me@x.com", "friend@x.com"]
    assert OWNER.token in calls[0]["html"] and "List-Unsubscribe" in calls[0]["headers"]
    assert "headers" not in calls[1] and "feedback.html" not in calls[1]["html"]
    assert calls[1]["html"] == newsletter.render_digest_email(
        items, date_str=newsletter.kst_date_label(DATE),
        archive_url=f"{newsletter.ARCHIVE_BASE}/{DATE}.html",
    )
    assert delivered == {"me@x.com"}


def test_send_digest_owner_link_failure_falls_back_to_plain_mail(monkeypatch):
    """링크 붙이기에 실패해도 운영자 메일은 기존 모습으로 나간다."""
    _configure(monkeypatch, EMAIL_TO="me@x.com")
    calls = _record_resend(monkeypatch)
    delivered: set[str] = set()
    monkeypatch.setattr(newsletter, "unsubscribe_headers", lambda token: 1 / 0)

    ok = asyncio.run(
        newsletter.send_digest([_item(1)], date_iso=DATE, owners={"me@x.com": OWNER.token},
                               owner_delivered=delivered)
    )

    assert ok is True and len(calls) == 1
    assert "headers" not in calls[0] and "feedback.html" not in calls[0]["html"]
    assert delivered == set()


# ── 관심도 라벨 병합 ─────────────────────────────────────────────────────


def _write_labels(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def test_subscriber_labels_come_from_feedback_events(monkeypatch, tmp_path: Path):
    """구독자 user_id 는 feedback_events 의 url 별 최신 want/skip 을 읽는다."""
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch, [OWNER, ALICE, BOB])
    tables.events = [
        {"subscriber_id": ALICE.id, "item_url": "https://a/1", "action": "want",
         "created_at": "2026-11-10T00:00:00+00:00", "digest_date": "2026-11-10"},
        {"subscriber_id": ALICE.id, "item_url": "https://a/1", "action": "skip",
         "created_at": "2026-11-11T00:00:00+00:00", "digest_date": "2026-11-10"},
        {"subscriber_id": ALICE.id, "item_url": "https://a/2", "action": "want",
         "created_at": "2026-11-11T00:00:00+00:00", "digest_date": None},
        {"subscriber_id": BOB.id, "item_url": "https://a/3", "action": "want",
         "created_at": "2026-11-11T00:00:00+00:00", "digest_date": None},
    ]
    path = tmp_path / "labels.jsonl"
    _write_labels(path, [{"url": "https://local/only", "human": "want"}])

    labels = interest_model.load_interest_labels(ALICE.user_id, path=path)

    assert {u: r["human"] for u, r in labels.items()} == {"https://a/1": "skip", "https://a/2": "want"}


def test_default_merges_owner_events_with_jsonl_latest_wins(monkeypatch, tmp_path: Path):
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch, [OWNER, ALICE])
    tables.events = [
        # jsonl 보다 늦다: 이긴다
        {"subscriber_id": OWNER.id, "item_url": "https://a/1", "action": "skip",
         "created_at": "2026-11-12T00:00:00+00:00", "digest_date": None},
        # jsonl 보다 이르다: 진다
        {"subscriber_id": OWNER.id, "item_url": "https://a/2", "action": "skip",
         "created_at": "2026-11-01T00:00:00+00:00", "digest_date": None},
        # jsonl 의 나중 unsure 가 이 표시를 지운다
        {"subscriber_id": OWNER.id, "item_url": "https://a/3", "action": "want",
         "created_at": "2026-11-01T00:00:00+00:00", "digest_date": None},
        {"subscriber_id": OWNER.id, "item_url": "https://a/4", "action": "want",
         "created_at": "2026-11-01T00:00:00+00:00", "digest_date": None},
        # 다른 구독자의 표시는 섞이지 않는다
        {"subscriber_id": ALICE.id, "item_url": "https://a/9", "action": "want",
         "created_at": "2026-11-12T00:00:00+00:00", "digest_date": None},
    ]
    path = tmp_path / "labels.jsonl"
    _write_labels(path, [
        {"url": "https://a/1", "human": "want", "labeled_at": "2026-11-10T09:00:00+09:00"},
        {"url": "https://a/2", "human": "want", "labeled_at": "2026-11-10T09:00:00+09:00"},
        {"url": "https://a/3", "human": "unsure", "labeled_at": "2026-11-10T09:00:00+09:00"},
    ])

    labels = interest_model.load_interest_labels(path=path)

    assert {u: r["human"] for u, r in labels.items()} == {
        "https://a/1": "skip", "https://a/2": "want", "https://a/4": "want",
    }


def test_labels_fall_back_to_jsonl_when_supabase_fails(monkeypatch, tmp_path: Path):
    """conftest 가 클라이언트 생성을 막아 조회가 실패한다. jsonl 만으로 계속 간다."""
    _configure(monkeypatch)
    path = tmp_path / "labels.jsonl"
    _write_labels(path, [{"url": "https://a/1", "human": "want"}])

    assert set(interest_model.load_interest_labels(path=path)) == {"https://a/1"}
    assert interest_model.load_interest_labels(ALICE.user_id, path=path) == {}


def test_label_features_are_joined_from_the_pool_too(tmp_path: Path):
    """구독자만 받은 아이템은 정본에 없다. 풀에서 특징을 찾는다."""
    pool = [ScoredCandidate(item=_item(n, score=8), absolute_score=8) for n in range(3)]
    for c in pool:
        c.item.raw.source_key = "blog.example"
        c.item.analysis.domain = ["mlops"]
    save_pool(snapshot_pool(pool, []), DATE, base_dir=tmp_path)
    labels: dict[str, dict[str, object]] = {
        c.item.raw.url: {"url": c.item.raw.url, "human": "want"} for c in pool
    }
    for n in range(20):
        labels[f"https://pad/{n}"] = {"url": f"https://pad/{n}", "human": "skip"}

    without = interest_model.build_interest_model(labels, tmp_path / "no_records",
                                                   pool_dir=tmp_path / "no_pool")
    with_pool = interest_model.build_interest_model(labels, tmp_path / "no_records",
                                                     pool_dir=tmp_path / "data" / "pool")

    assert without.active and "domain" not in without.features
    assert with_pool.features["domain"]["mlops"].want == 3


# ── 수동 실행 CLI ────────────────────────────────────────────────────────


def _save_pool_file(tmp_path: Path, selected: int = 2) -> list[ScoredCandidate]:
    pool = _pool()
    save_pool(snapshot_pool(pool, [c.item for c in pool[:selected]]), DATE, base_dir=tmp_path)
    return pool


def test_cli_dry_run_writes_html_without_sending_or_recording(monkeypatch, selection, tmp_path: Path, capsys):
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch, [OWNER, ALICE])
    calls = _record_resend(monkeypatch)
    _save_pool_file(tmp_path)

    code = send_job.main(["--date", DATE, "--dry-run"], base_dir=tmp_path)

    assert code == 0
    assert calls == [] and tables.record_calls == []
    out_dir = tmp_path / "data" / "dryrun" / "subscribers" / DATE
    assert sorted(p.name for p in out_dir.iterdir()) == [f"{OWNER.short_id}.html", f"{ALICE.short_id}.html"]
    alice_html = (out_dir / f"{ALICE.short_id}.html").read_text(encoding="utf-8")
    assert ALICE.token in alice_html and "읽고 싶다" in alice_html
    out = capsys.readouterr().out
    assert "드라이런 2" in out and "alice@x.com" not in out


def test_cli_only_sends_to_one_subscriber(monkeypatch, selection, tmp_path: Path):
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch, [OWNER, ALICE, BOB])
    calls = _record_resend(monkeypatch)
    _save_pool_file(tmp_path)

    code = send_job.main(["--date", DATE, "--only", "BOB@x.com"], base_dir=tmp_path)

    assert code == 0 and _to(calls) == ["bob@x.com"]
    assert [call[0] for call in tables.record_calls] == [BOB.id]


def test_cli_owner_uses_stored_default_selection(monkeypatch, selection, tmp_path: Path):
    """정본이 없으면 풀의 selected_default 표시가 운영자 목록이다."""
    _configure(monkeypatch)
    FakeTables(monkeypatch, [OWNER])
    calls = _record_resend(monkeypatch)
    _save_pool_file(tmp_path, selected=3)

    assert send_job.main(["--date", DATE], base_dir=tmp_path) == 0
    assert all(f"아이템 {n}" in calls[0]["html"] for n in range(3))
    assert "아이템 3" not in calls[0]["html"]


def test_cli_exit_codes(monkeypatch, selection, tmp_path: Path, capsys):
    _configure(monkeypatch)
    FakeTables(monkeypatch, [ALICE])
    _record_resend(monkeypatch, fail_for=frozenset({"alice@x.com"}))

    assert send_job.main(["--date", "2026-01-01"], base_dir=tmp_path) == 1  # 풀 없음
    _save_pool_file(tmp_path)
    assert send_job.main(["--date", DATE, "--only", "nobody@x.com"], base_dir=tmp_path) == 1
    assert send_job.main(["--date", DATE], base_dir=tmp_path) == 1  # 발송 실패
    assert "alice@x.com" not in capsys.readouterr().out


# ── 파이프라인 통합: 운영자 흐름은 그대로다 ──────────────────────────────────


class Pipeline:
    """run_daily_digest 를 수집·분석·외부 저장 없이 태우는 대역 묶음."""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.pool = _pool()
        self.selected = [c.item for c in self.pool[:2]]
        self.alerts: list[str] = []
        self.marked: list[list[str]] = []
        self.saved_pools: list[int] = []
        self.send_digest_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def _collect(*a: Any, **k: Any):
            return [], [c.item.raw for c in self.pool]

        async def _none(*a: Any, **k: Any) -> list:
            return []

        async def _feedback() -> dict:
            return {}

        async def _directive(items: list) -> SimpleNamespace:
            return SimpleNamespace(is_empty=lambda: True)

        async def _transcripts(items: list, *a: Any) -> list:
            return items

        async def _analyze(raw_items: list, profile: Any, directive: Any, pool_out: list) -> list:
            pool_out.extend(snapshot_pool(self.pool, self.selected))
            return self.selected

        async def _alert(message: str) -> None:
            self.alerts.append(message)

        async def _mirror(*a: Any, **k: Any) -> None:
            return None

        import app.collectors as collectors
        import app.collectors_newsletter as collectors_newsletter
        import app.pool as pool_module
        import app.records as records
        import app.source_stats as source_stats

        # 산출물 경로(root)는 __file__ 에서 계산된다. 실제 docs/ 와 data/ 를 건드리지 않게 옮긴다.
        monkeypatch.setattr(job, "__file__", str(tmp_path / "app" / "jobs" / "daily_digest.py"))
        monkeypatch.setattr(job, "collect_all", _collect)
        monkeypatch.setattr(collectors, "fetch_arxiv_recent", _none)
        monkeypatch.setattr(collectors, "fetch_hackernews_recent", _none)
        monkeypatch.setattr(collectors_newsletter, "fetch_newsletters_recent", _none)
        monkeypatch.setattr(job, "_process_pending_feedback", _feedback)
        monkeypatch.setattr(job, "load_profile", lambda *a, **k: SimpleNamespace(
            preferred_topics=[], keyword_requests=[]))
        monkeypatch.setattr(job, "_deduplicate", lambda items: items)
        monkeypatch.setattr(job, "resolve_directives", _directive)
        monkeypatch.setattr(job, "resolve_youtube_transcripts", _transcripts)
        monkeypatch.setattr(job, "filter_and_analyze", _analyze)
        monkeypatch.setattr(job, "_send_error_alert", _alert)
        monkeypatch.setattr(job, "_mark_sent", lambda items: self.marked.append([i.raw.url for i in items]))
        monkeypatch.setattr(source_stats, "record", lambda *a, **k: None)
        monkeypatch.setattr(records, "save_digest_records", lambda *a, **k: tmp_path / "record.json")
        monkeypatch.setattr(db, "save_digest_records_to_db", _mirror)
        monkeypatch.setattr(db, "cleanup_seen_urls", lambda *a, **k: 0)
        monkeypatch.setattr(pool_module, "save_pool",
                            lambda entries, *a, **k: self.saved_pools.append(len(entries)) or None)

    def spy_send_digest(self, monkeypatch, result: bool = True) -> None:
        async def _send(*args: Any, **kwargs: Any) -> bool:
            self.send_digest_calls.append((args, kwargs))
            return result

        monkeypatch.setattr(job, "send_digest", _send)

    def run(self) -> dict:
        return asyncio.run(job.run_daily_digest())


def test_pipeline_without_subscribers_is_unchanged(monkeypatch, selection, tmp_path: Path):
    """구독자 0명: send_digest 는 예전 인자 그대로 불리고, 구독자 단계는 흔적이 없다."""
    _configure(monkeypatch)
    FakeTables(monkeypatch, [])
    pipeline = Pipeline(monkeypatch, tmp_path)
    pipeline.spy_send_digest(monkeypatch)
    resend_calls = _record_resend(monkeypatch)
    monkeypatch.setattr(delivery, "deliver_to_subscribers", lambda *a, **k: pytest.fail("호출 금지"))

    result = pipeline.run()

    assert pipeline.send_digest_calls == [((pipeline.selected,), {})]
    assert result["status"] == "ok" and result["delivery"] == {"email": True}
    assert set(result) == {"status", "collected", "analyzed", "delivery", "elapsed_seconds"}
    assert resend_calls == [] and pipeline.alerts == []
    assert pipeline.marked == [[i.raw.url for i in pipeline.selected]]
    assert pipeline.saved_pools == [5]


def test_pipeline_without_supabase_is_unchanged(monkeypatch, selection, tmp_path: Path):
    """Supabase 미설정: 명단을 읽으려 하지도 않고 feedback-config.json 도 쓰지 않는다."""
    _configure(monkeypatch, SUPABASE_URL="", SUPABASE_SERVICE_KEY="")
    monkeypatch.setattr(db, "fetch_subscribers", lambda *a, **k: pytest.fail("조회 금지"))
    pipeline = Pipeline(monkeypatch, tmp_path)
    pipeline.spy_send_digest(monkeypatch)

    result = pipeline.run()

    assert pipeline.send_digest_calls == [((pipeline.selected,), {})]
    assert "subscribers" not in result
    assert not (tmp_path / "docs" / "feedback-config.json").exists()
    assert (tmp_path / "docs" / "index.html").exists()


def test_pipeline_subscriber_list_failure_is_unchanged(monkeypatch, selection, tmp_path: Path):
    """명단 조회가 실패해도(conftest 가 막아 둔 상태) 운영자 흐름은 그대로 끝난다."""
    _configure(monkeypatch)
    pipeline = Pipeline(monkeypatch, tmp_path)
    pipeline.spy_send_digest(monkeypatch)

    result = pipeline.run()

    assert result["status"] == "ok" and "subscribers" not in result
    assert pipeline.send_digest_calls == [((pipeline.selected,), {})]


def test_pipeline_sends_to_subscribers_after_operator_flow(monkeypatch, selection, tmp_path: Path):
    """운영자(EMAIL_TO 에 있는 is_owner)는 링크 든 메일 한 통, 구독자는 각자 한 통."""
    _configure(monkeypatch, EMAIL_TO="me@x.com")
    tables = FakeTables(monkeypatch, [OWNER, ALICE])
    pipeline = Pipeline(monkeypatch, tmp_path)
    calls = _record_resend(monkeypatch)

    result = pipeline.run()

    assert _to(calls) == ["me@x.com", "alice@x.com"], "운영자 주소로 두 통이 가면 안 된다"
    assert OWNER.token in calls[0]["html"] and ALICE.token in calls[1]["html"]
    assert result["delivery"] == {"email": True}
    assert result["subscribers"] == {
        "total": 2, "sent": 1, "failed": 0, "already_sent": 1, "no_items": 0, "dry_run": 0,
    }
    assert {call[0] for call in tables.record_calls} == {OWNER.id, ALICE.id}
    assert pipeline.alerts == []
    config = json.loads((tmp_path / "docs" / "feedback-config.json").read_text(encoding="utf-8"))
    assert config == {"endpoint": ENDPOINT}


def test_pipeline_owner_not_in_email_channel_gets_one_mail(monkeypatch, selection, tmp_path: Path):
    """이메일 채널이 꺼져 있으면 운영자 주소는 구독자 단계에서 한 번만 받는다."""
    _configure(monkeypatch, DELIVERY_CHANNELS="discord")
    FakeTables(monkeypatch, [OWNER])
    pipeline = Pipeline(monkeypatch, tmp_path)

    async def _discord(items: list) -> bool:
        return True

    monkeypatch.setattr(job, "send_discord_digest", _discord)
    calls = _record_resend(monkeypatch)

    result = pipeline.run()

    assert _to(calls) == ["me@x.com"]
    assert all(f"아이템 {n}" in calls[0]["html"] for n in range(2))
    assert result["delivery"] == {"discord": True} and result["subscribers"]["sent"] == 1


def test_pipeline_survives_subscriber_send_failures(monkeypatch, selection, tmp_path: Path):
    """구독자 발송이 전부 실패해도 운영자 결과는 그대로고, 알림에는 주소가 없다."""
    _configure(monkeypatch, EMAIL_TO="other@x.com")
    FakeTables(monkeypatch, [ALICE, BOB])
    pipeline = Pipeline(monkeypatch, tmp_path)
    calls = _record_resend(monkeypatch, fail_for=frozenset({"alice@x.com", "bob@x.com"}))

    result = pipeline.run()

    assert _to(calls) == ["other@x.com", "alice@x.com", "bob@x.com"]
    assert result["status"] == "ok" and result["delivery"] == {"email": True}
    assert result["subscribers"]["failed"] == 2
    assert pipeline.marked == [[i.raw.url for i in pipeline.selected]]
    assert len(pipeline.alerts) == 1 and "구독자 발송 실패 2/2명" in pipeline.alerts[0]
    assert "@" not in pipeline.alerts[0]


def test_pipeline_survives_subscriber_step_crash(monkeypatch, selection, tmp_path: Path):
    """구독자 단계가 통째로 터져도 예외가 올라오지 않는다(워크플로 성공 유지)."""
    _configure(monkeypatch, EMAIL_TO="other@x.com")
    FakeTables(monkeypatch, [ALICE])
    pipeline = Pipeline(monkeypatch, tmp_path)
    pipeline.spy_send_digest(monkeypatch)

    def _boom(*a: Any, **k: Any):
        raise RuntimeError("구독자 단계 붕괴")

    monkeypatch.setattr(delivery, "deliver_to_subscribers", _boom)

    with structlog.testing.capture_logs() as logs:
        result = pipeline.run()

    assert result["status"] == "ok" and "subscribers" not in result
    assert [e for e in logs if e["event"] == "subscriber_delivery_failed"]
    assert [e for e in logs if e["event"] == "digest_complete"]


def test_pipeline_dry_run_does_not_touch_subscribers(monkeypatch, selection, tmp_path: Path):
    _configure(monkeypatch, DRY_RUN="true")
    monkeypatch.setattr(db, "fetch_subscribers", lambda *a, **k: pytest.fail("조회 금지"))
    pipeline = Pipeline(monkeypatch, tmp_path)
    calls = _record_resend(monkeypatch)

    result = pipeline.run()

    assert result["status"] == "ok" and "subscribers" not in result and calls == []


# ── 비교군(arm) 배정 (PROGRESS §57) ─────────────────────────────────────


def _limit(monkeypatch, limit: int) -> None:
    """정원만 바꾼 선정 설정(selection fixture 와 같은 방식)."""
    monkeypatch.setattr(
        analyzer,
        "get_settings",
        lambda: SimpleNamespace(
            relevance_floor=4,
            max_items_per_digest=limit,
            max_items_per_source=2,
            source_diversity_strength=1.0,
        ),
    )
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())


def _interest(monkeypatch, by_title: dict[str, float]) -> list[str]:
    """관심도 모델이 켜진 것처럼 제목별 조정값을 준다. 불린 user_id 를 돌려준다."""
    seen: list[str] = []

    def _adjust(candidates: list[DigestItem], user_id: str) -> dict[int, tuple[float, list[str]]]:
        seen.append(user_id)
        return {
            id(d): (by_title[d.raw.title], ["test"]) for d in candidates if d.raw.title in by_title
        }

    monkeypatch.setattr(analyzer, "_interest_adjustments", _adjust)
    return seen


# 정원 3, 후보 점수 10, 9, 8, 7, 6, 5 에서 개인화는 0, 3, 4 를, 기준은 0, 1, 2 를 고른다.
DIVERGING = {"아이템 1": -2.0, "아이템 2": -2.0, "아이템 3": 1.5, "아이템 4": 1.5}


def _arms_of(placements: list[delivery.Placement]) -> list[tuple[str, str | None]]:
    return [(p.item.raw.title, p.arm) for p in placements]


def test_arms_are_shared_when_interest_model_is_inactive(monkeypatch, selection):
    """표시가 모자라 모델이 꺼져 있으면 기준 선정을 따로 돌리지 않고 전부 shared 다."""
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    real = analyzer.select_for_user
    flags: list[bool] = []

    def _spy(*args: Any, **kwargs: Any):
        flags.append(kwargs.get("apply_interest", True))
        return real(*args, **kwargs)

    monkeypatch.setattr(analyzer, "select_for_user", _spy)

    placements = delivery.plan_for_subscriber(ALICE, _pool(), None)

    assert _arms_of(placements) == [("아이템 0", "shared"), ("아이템 1", "shared")]
    assert flags == [True]


def test_arms_are_shared_when_both_selections_agree(monkeypatch, selection):
    """조정이 걸려도 고른 목록이 같으면 비교할 것이 없다."""
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    _interest(monkeypatch, {"아이템 0": 1.0, "아이템 4": 0.5})

    placements = delivery.plan_for_subscriber(ALICE, _pool(), None)

    assert _arms_of(placements) == [("아이템 0", "shared"), ("아이템 1", "shared")]


# 정원 3 에서 개인화는 0, 1, 3 을, 기준은 0, 1, 2 를 고른다(한 건만 다르다).
ONE_APART = {"아이템 3": 1.5}


def _dates(count: int = 40) -> list[str]:
    return [f"2026-{11 + d // 30:02d}-{d % 30 + 1:02d}" for d in range(count)]


def test_one_item_apart_days_split_into_both_arms(monkeypatch):
    """두 목록이 한 건만 다른 날에도 날짜에 따라 personal 과 baseline 이 갈린다."""
    _configure(monkeypatch)
    _limit(monkeypatch, 3)
    FakeTables(monkeypatch)
    _interest(monkeypatch, ONE_APART)

    outcomes = [
        tuple(_arms_of(delivery.plan_for_subscriber(ALICE, _pool(6), None, date_iso=day)))
        for day in _dates()
    ]

    as_personal = (("아이템 0", "shared"), ("아이템 1", "shared"), ("아이템 3", "personal"))
    as_baseline = (("아이템 0", "shared"), ("아이템 1", "shared"), ("아이템 2", "baseline"))
    assert set(outcomes) == {as_personal, as_baseline}
    # 기본 확률 0.5 다. 40일이면 어느 쪽도 한 자릿수로 쏠리지 않는다.
    assert 10 <= outcomes.count(as_baseline) <= 30


def test_assignment_is_deterministic_per_subscriber_and_date(monkeypatch):
    """같은 구독자와 날짜면 다시 계산해도 같다(재발송 안전). 사람이 다르면 따로 뽑는다."""
    _configure(monkeypatch)
    _limit(monkeypatch, 3)
    FakeTables(monkeypatch)
    _interest(monkeypatch, ONE_APART)

    def run(subscriber: Subscriber) -> list[list[tuple[str, str | None]]]:
        return [
            _arms_of(delivery.plan_for_subscriber(subscriber, _pool(6), None, date_iso=day))
            for day in _dates()
        ]

    assert run(ALICE) == run(ALICE)
    assert run(ALICE) != run(BOB)
    assert delivery.holdout_draw(ALICE.id, DATE, 0) == delivery.holdout_draw(ALICE.id, DATE, 0)
    assert 0.0 <= delivery.holdout_draw(ALICE.id, DATE, 0) < 1.0
    assert delivery.holdout_draw(ALICE.id, DATE, 0) != delivery.holdout_draw(ALICE.id, DATE, 1)


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        ("0", ("아이템 3", "personal")),
        ("1", ("아이템 2", "baseline")),
        # 범위를 벗어난 값은 0~1 로 잘린다.
        ("-3", ("아이템 3", "personal")),
        ("7", ("아이템 2", "baseline")),
    ],
)
def test_holdout_rate_zero_and_one(monkeypatch, rate, expected):
    _configure(monkeypatch, EFFECT_HOLDOUT_RATE=rate)
    _limit(monkeypatch, 3)
    FakeTables(monkeypatch)
    _interest(monkeypatch, ONE_APART)

    for day in _dates(10):
        placements = delivery.plan_for_subscriber(ALICE, _pool(6), None, date_iso=day)
        assert _arms_of(placements) == [("아이템 0", "shared"), ("아이템 1", "shared"), expected]


def test_holdout_rate_empty_or_garbage_falls_back_to_half(monkeypatch):
    for raw in ("", "  ", "abc"):
        _configure(monkeypatch, EFFECT_HOLDOUT_RATE=raw)
        assert get_settings().effect_holdout_rate == 0.5


def test_only_the_experimental_pair_is_labelled(monkeypatch):
    """짝은 (개인화 순위 낮은 것, 기준 순위 높은 것) 순서로 묶고, 실험 밖의 개인화 전용은 None 이다."""
    _limit(monkeypatch, 3)
    FakeTables(monkeypatch)
    _interest(monkeypatch, DIVERGING)

    # 짝: (4, 1), (3, 2). 자리 1개면 첫 짝만 실험 대상이다.
    _configure(monkeypatch, EFFECT_HOLDOUT_RATE="1")
    assert _arms_of(delivery.plan_for_subscriber(ALICE, _pool(6), None, date_iso=DATE)) == [
        ("아이템 0", "shared"),
        ("아이템 1", "baseline"),
        ("아이템 3", None),
    ]
    _configure(monkeypatch, EFFECT_HOLDOUT_RATE="0")
    assert _arms_of(delivery.plan_for_subscriber(ALICE, _pool(6), None, date_iso=DATE)) == [
        ("아이템 0", "shared"),
        ("아이템 3", None),
        ("아이템 4", "personal"),
    ]


def test_holdout_zero_keeps_the_personal_list(monkeypatch):
    _configure(monkeypatch, EFFECT_HOLDOUT_SLOTS="0", EFFECT_HOLDOUT_RATE="1")
    _limit(monkeypatch, 3)
    FakeTables(monkeypatch)
    _interest(monkeypatch, DIVERGING)

    placements = delivery.plan_for_subscriber(ALICE, _pool(6), None, date_iso=DATE)

    # 실험 대상 짝이 없으므로 개인화 전용 아이템은 어느 비교군도 아니다.
    assert _arms_of(placements) == [
        ("아이템 0", "shared"),
        ("아이템 3", None),
        ("아이템 4", None),
    ]


def test_holdout_slots_are_capped_by_the_difference(monkeypatch):
    """자리 수를 크게 줘도 서로 다른 아이템 수만큼만 짝이 생기고 정원은 그대로다."""
    _configure(monkeypatch, EFFECT_HOLDOUT_SLOTS="9", EFFECT_HOLDOUT_RATE="1")
    _limit(monkeypatch, 3)
    FakeTables(monkeypatch)
    _interest(monkeypatch, DIVERGING)

    placements = delivery.plan_for_subscriber(ALICE, _pool(6), None, date_iso=DATE)

    assert _arms_of(placements) == [
        ("아이템 0", "shared"),
        ("아이템 1", "baseline"),
        ("아이템 2", "baseline"),
    ]


def test_quota_is_kept_for_every_draw(monkeypatch):
    _configure(monkeypatch, EFFECT_HOLDOUT_SLOTS="2")
    _limit(monkeypatch, 3)
    FakeTables(monkeypatch)
    _interest(monkeypatch, DIVERGING)

    seen: set[tuple[str | None, ...]] = set()
    for day in _dates():
        placements = delivery.plan_for_subscriber(ALICE, _pool(6), None, date_iso=day)
        assert len(placements) == 3 and len({p.item.raw.url for p in placements}) == 3
        scores = [p.item.analysis.relevance_score for p in placements]
        assert scores == sorted(scores, reverse=True)
        seen.add(tuple(sorted(str(p.arm) for p in placements)))
    # 두 짝이 따로 뽑힌다(둘 다 개인화, 둘 다 기준, 하나씩). 비교군 구성으로는 세 가지다.
    assert len(seen) == 3 and ("baseline", "personal", "shared") in seen


def test_display_order_does_not_reveal_the_arm():
    """표시 점수가 같으면 후보 풀 순서로 간다. 기준 자리가 동점 묶음의 끝으로 몰리지 않는다."""
    a, b, c, d = _item(0, 9), _item(1, 8), _item(2, 8), _item(3, 8)
    order = {item.raw.url: i for i, item in enumerate([a, b, c, d])}
    scores = {id(a): 9.0, id(c): 9.5, id(d): 8.6}

    def assign(draw: float) -> list[tuple[str, str | None]]:
        return _arms_of(
            delivery.assign_arms(
                personal=[a, c, d], baseline=[a, b, c], personal_score=lambda i: scores[id(i)],
                pool_order=order, holdout_slots=1, holdout_rate=0.5, draw=lambda index: draw,
            )
        )

    # 난수가 확률보다 작으면 기준 쪽(b)이 나간다. b 는 c 와 동점이고 풀에서 앞이다.
    assert assign(0.49) == [("아이템 0", "shared"), ("아이템 1", "baseline"), ("아이템 2", "shared")]
    # 아니면 개인화 쪽(d)이 나간다. 목록과 순서는 개인화 선정 그대로다.
    assert assign(0.5) == [("아이템 0", "shared"), ("아이템 2", "shared"), ("아이템 3", "personal")]


def test_arm_failure_still_delivers_the_personal_list(monkeypatch):
    """기준 선정이 죽어도 메일은 나가고 arm 만 비운다(fail-open)."""
    _configure(monkeypatch)
    _limit(monkeypatch, 3)
    tables = FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    _interest(monkeypatch, DIVERGING)
    real = analyzer.select_for_user

    def _flaky(*args: Any, **kwargs: Any):
        if kwargs.get("apply_interest") is False:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(analyzer, "select_for_user", _flaky)

    summary = delivery.deliver_to_subscribers([ALICE], _pool(6), DATE)

    assert summary.as_dict()["sent"] == 1 and len(calls) == 1
    assert [u.rsplit("id=", 1)[1][0] for u in tables.record_calls[0][1]] == ["0", "3", "4"]
    assert tables.arms[ALICE.id] is None


def test_delivery_records_arm_in_page_order(monkeypatch):
    _configure(monkeypatch, EFFECT_HOLDOUT_RATE="1")
    _limit(monkeypatch, 3)
    tables = FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    _interest(monkeypatch, DIVERGING)
    pool = _pool(6)

    delivery.deliver_to_subscribers([ALICE], pool, DATE)

    urls = [pool[0].item.raw.url, pool[1].item.raw.url, pool[3].item.raw.url]
    assert tables.record_calls == [(ALICE.id, urls, DATE)]
    # 실험 밖의 개인화 전용 아이템(3)은 None 으로 기록된다.
    assert tables.arms[ALICE.id] == ["shared", "baseline", None]
    html = calls[0]["html"]
    # 메일 안의 순서가 기록 순서(position)와 같고, 비교군 이름은 지면에 나오지 않는다.
    assert html.index("아이템 0") < html.index("아이템 1") < html.index("아이템 3")
    assert "아이템 4" not in html
    for arm in ("baseline", "personal", "shared"):
        assert arm not in html


def test_owner_list_is_unchanged_and_only_labelled(monkeypatch, selection):
    """is_owner 는 기준 자리를 받지 않는다. 양쪽에 다 있는 것만 shared 이고 나머지는 None 이다."""
    _configure(monkeypatch)
    tables = FakeTables(monkeypatch)
    calls = _record_resend(monkeypatch)
    users = _interest(monkeypatch, {"아이템 2": 3.0})
    pool = _pool()
    # 운영자 흐름이 고른 것(개인화 선정 0, 2). 기준 선정이라면 0, 1 이다.
    owner_items = [pool[0].item, pool[2].item]

    delivery.deliver_to_subscribers([OWNER], pool, DATE, owner_items=owner_items)

    assert tables.record_calls == [(OWNER.id, [i.raw.url for i in owner_items], DATE)]
    # 운영자의 개인화 전용 아이템은 personal 로 세지 않는다(무작위 배정이 아니다).
    assert tables.arms[OWNER.id] == ["shared", None]
    assert "아이템 1" not in calls[0]["html"]
    assert set(users) == {"default"}


def test_owner_arm_is_none_when_item_is_not_in_the_pool(monkeypatch, selection):
    _configure(monkeypatch)
    _interest(monkeypatch, {"아이템 2": 3.0})
    pool = _pool()
    owner_items = [pool[0].item, _item(99)]

    placements = delivery.plan_for_subscriber(OWNER, pool, owner_items)

    assert [p.item for p in placements] == owner_items
    assert [p.arm for p in placements] == [None, None]


def test_owner_arm_survives_selection_crash(monkeypatch, selection):
    _configure(monkeypatch)
    pool = _pool()
    owner_items = [pool[0].item, pool[1].item]

    def _boom(*a: Any, **k: Any):
        raise RuntimeError("boom")

    monkeypatch.setattr(analyzer, "select_for_user", _boom)

    placements = delivery.plan_for_subscriber(OWNER, pool, owner_items)

    assert [(p.item, p.arm) for p in placements] == [(i, None) for i in owner_items]


def test_pipeline_passes_operator_context_for_owner_arms(monkeypatch, selection, tmp_path: Path):
    """운영자 선정이 풀에서 재현되므로 is_owner 의 발송 기록에 비교군이 붙는다."""
    _configure(monkeypatch)
    from app.directives import Directive
    from app.models import UserProfile

    tables = FakeTables(monkeypatch, [OWNER])
    pipeline = Pipeline(monkeypatch, tmp_path)
    _record_resend(monkeypatch)
    # 대역 묶음의 프로필과 지시는 속성 몇 개만 흉내 낸다. 선정을 실제로 돌리려면 진짜가 필요하다.
    monkeypatch.setattr(job, "load_profile", lambda *a, **k: UserProfile(user_id="default"))

    async def _directive(items: list) -> Directive:
        return Directive()

    monkeypatch.setattr(job, "resolve_directives", _directive)

    pipeline.run()

    assert tables.arms[OWNER.id] == ["shared", "shared"]


# ── sent_items 의 arm, position 과 컬럼 없음 폴백 ───────────────────────


class _FakeSentItems:
    """supabase 클라이언트 대역. upsert 페이로드를 모으고, 지정한 컬럼이 오면 거절한다."""

    def __init__(self, reject_columns: frozenset[str] = frozenset(), always_fail: bool = False):
        self.payloads: list[list[dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        self.reject_columns = reject_columns
        self.always_fail = always_fail
        self._pending: list[dict[str, Any]] = []
        self.delete_result: list[dict[str, Any]] = [{"id": "x"}]
        self._mode = ""

    def table(self, name: str) -> "_FakeSentItems":
        self.name = name
        return self

    def upsert(self, rows: list[dict[str, Any]], on_conflict: str = "") -> "_FakeSentItems":
        assert on_conflict == "subscriber_id,item_url"
        self._pending, self._mode = rows, "upsert"
        return self

    def delete(self) -> "_FakeSentItems":
        self._mode = "delete"
        return self

    def eq(self, column: str, value: str) -> "_FakeSentItems":
        self.deleted.append((column, value))
        return self

    def execute(self) -> SimpleNamespace:
        if self._mode == "delete":
            return SimpleNamespace(data=self.delete_result)
        self.payloads.append(self._pending)
        if self.always_fail or any(self.reject_columns & set(row) for row in self._pending):
            raise RuntimeError("PGRST204 Could not find the 'arm' column of 'sent_items'")
        return SimpleNamespace(data=self._pending)


def test_record_sent_items_writes_arm_and_position(monkeypatch):
    client = _FakeSentItems()
    monkeypatch.setattr(db, "get_supabase_service", lambda: client)

    assert db.record_sent_items("sub-1", ["u1", "u2", "u1"], DATE, arms=["shared", "baseline", "shared"])

    assert client.payloads == [[
        {"subscriber_id": "sub-1", "item_url": "u1", "digest_date": DATE, "position": 1, "arm": "shared"},
        {"subscriber_id": "sub-1", "item_url": "u2", "digest_date": DATE, "position": 2, "arm": "baseline"},
    ]]


def test_record_sent_items_without_arms_leaves_the_arm_column_alone(monkeypatch):
    client = _FakeSentItems()
    monkeypatch.setattr(db, "get_supabase_service", lambda: client)

    assert db.record_sent_items("sub-1", ["u1"], DATE)

    assert client.payloads == [[
        {"subscriber_id": "sub-1", "item_url": "u1", "digest_date": DATE, "position": 1}
    ]]


def test_record_sent_items_falls_back_when_columns_are_missing(monkeypatch):
    """마이그레이션 적용 전에도 발송 기록은 남는다. 두 컬럼 없이 한 번 더 쓴다."""
    client = _FakeSentItems(reject_columns=frozenset({"arm", "position"}))
    monkeypatch.setattr(db, "get_supabase_service", lambda: client)

    with structlog.testing.capture_logs() as logs:
        assert db.record_sent_items("sub-1", ["u1", "u2"], DATE, arms=["personal", "shared"])

    assert len(client.payloads) == 2
    assert client.payloads[1] == [
        {"subscriber_id": "sub-1", "item_url": "u1", "digest_date": DATE},
        {"subscriber_id": "sub-1", "item_url": "u2", "digest_date": DATE},
    ]
    assert [e["log_level"] for e in logs if e["event"] == "sent_items_arm_columns_rejected"] == ["warning"]


def test_record_sent_items_gives_up_after_one_retry(monkeypatch):
    client = _FakeSentItems(always_fail=True)
    monkeypatch.setattr(db, "get_supabase_service", lambda: client)

    assert db.record_sent_items("sub-1", ["u1"], DATE, arms=["shared"]) is False
    assert len(client.payloads) == 2


# ── 원문 링크의 클릭 기록 페이지 ────────────────────────────────────────


def _go_link(subscriber: Subscriber, item: DigestItem) -> str:
    return (
        f"{newsletter.ARCHIVE_BASE}/go.html?t={subscriber.token}"
        f"&u={quote(item.raw.url, safe='')}&d={DATE}"
    )


def test_subscriber_mail_title_links_go_through_the_click_page(monkeypatch):
    _configure(monkeypatch)
    items = [_item(1), _item(2)]

    html = delivery.render_for_subscriber(ALICE, items, DATE)[1]

    for item in items:
        assert f'href="{_go_link(ALICE, item).replace("&", "&amp;")}"' in html
        # 원문 주소는 인코딩된 형태로만 들어 있다(직접 가는 링크가 없다).
        assert f'href="{item.raw.url}"' not in html
    assert html.count("go.html?") == 2


def test_click_page_is_only_in_subscriber_mail(monkeypatch):
    """EMAIL_TO 기존 경로와 웹 아카이브는 원문 주소 그대로다."""
    _configure(monkeypatch)
    items = [_item(1), _item(2)]

    plain = newsletter.render_digest_email(items, date_str="d")
    web = newsletter.render_digest_web(items, date_str="d")

    for html in (plain, web):
        assert "go.html" not in html
    for item in items:
        assert f'href="{item.raw.url}"' in plain


def test_track_clicks_off_keeps_original_links(monkeypatch):
    _configure(monkeypatch, TRACK_CLICKS="false")
    item = _item(1)

    html = delivery.render_for_subscriber(ALICE, [item], DATE)[1]

    assert "go.html" not in html and f'href="{item.raw.url}"' in html
    # 피드백 링크는 그대로 붙는다.
    assert "feedback.html" in html


def test_email_to_recipients_keep_original_links_in_send_digest(monkeypatch):
    _configure(monkeypatch, EMAIL_TO="me@x.com,friend@x.com")
    calls = _record_resend(monkeypatch)
    item = _item(1)

    assert asyncio.run(newsletter.send_digest([item], date_iso=DATE))

    assert _to(calls) == ["me@x.com", "friend@x.com"]
    for call in calls:
        assert "go.html" not in call["html"] and f'href="{item.raw.url}"' in call["html"]


# ── 구독자 기록 삭제(purge) ─────────────────────────────────────────────


def test_cli_purge_requires_yes(monkeypatch, capsys):
    _configure(monkeypatch)
    FakeTables(monkeypatch, [ALICE])
    monkeypatch.setattr(db, "delete_subscriber", lambda sid: pytest.fail("--yes 없이 삭제 금지"))

    assert subscribers.main(["purge", "alice@x.com"]) == 1

    err = capsys.readouterr().err
    assert "--yes" in err and "alice@x.com" not in err and "a***@x.com" in err


def test_cli_purge_deletes_the_subscriber_row(monkeypatch, capsys):
    _configure(monkeypatch)
    FakeTables(monkeypatch, [ALICE, BOB])
    deleted: list[str] = []
    monkeypatch.setattr(db, "delete_subscriber", lambda sid: deleted.append(sid) or True)

    assert subscribers.main(["purge", "ALICE@x.com", "--yes"]) == 0

    assert deleted == [ALICE.id]
    out = capsys.readouterr().out
    assert "alice@x.com" not in out and ALICE.token not in out


def test_cli_purge_reports_missing_target_and_failure(monkeypatch, capsys):
    _configure(monkeypatch)
    FakeTables(monkeypatch, [ALICE])
    monkeypatch.setattr(db, "delete_subscriber", lambda sid: False)

    assert subscribers.main(["purge", "nobody@x.com", "--yes"]) == 1
    assert subscribers.main(["purge", "alice@x.com", "--yes"]) == 1


def test_delete_subscriber_relies_on_cascade(monkeypatch):
    """subscribers 한 행만 지운다. 연결 기록은 외래키 cascade 가 지운다."""
    client = _FakeSentItems()
    monkeypatch.setattr(db, "get_supabase_service", lambda: client)

    assert db.delete_subscriber("sub-1") is True
    assert client.name == "subscribers" and client.deleted == [("id", "sub-1")]

    client.delete_result = []
    assert db.delete_subscriber("sub-1") is False


def test_delete_subscriber_failure_is_false(monkeypatch):
    # conftest 가 클라이언트 생성을 막아 두었다.
    assert db.delete_subscriber("sub-1") is False


# ── 공개 로그에 주소가 남지 않는다 ──────────────────────────────────────


def test_scrub_error_drops_any_text_that_still_has_an_address():
    assert subscribers.scrub_error("rejected: Alice@x.com", "alice@x.com") == "rejected: (수신자)"
    # 모르는 주소가 섞여 있으면 문구를 통째로 버린다(가린 형태도 마찬가지).
    assert subscribers.scrub_error("bounce other@y.com", "alice@x.com") == subscribers.ERROR_REDACTED
    assert subscribers.scrub_error("to a***@x.com failed") == subscribers.ERROR_REDACTED
    assert subscribers.scrub_error(RuntimeError("x" * 500)) == "x" * 200
    assert subscribers.scrub_error("timeout") == "timeout"


def test_delivery_logs_and_outcomes_have_no_address(monkeypatch, selection):
    """발송 라이브러리가 다른 주소까지 섞어 오류를 내도 로그와 결과에 '@' 가 없다."""
    _configure(monkeypatch)
    FakeTables(monkeypatch)

    def _send(params: dict[str, Any]) -> dict[str, str]:
        raise RuntimeError(f"550 relay denied for <{params['to'][0]}> via postmaster@mx.example")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_send))

    with structlog.testing.capture_logs() as logs:
        summary = delivery.deliver_to_subscribers([ALICE, BOB], _pool(), DATE)

    assert summary.failed == 2
    dumped = json.dumps(logs, ensure_ascii=False, default=str)
    assert "@" not in dumped and "x.com" not in dumped
    assert [o.error for o in summary.outcomes] == [subscribers.ERROR_REDACTED] * 2
    assert ALICE.short_id in dumped and BOB.short_id in dumped


def test_exception_text_with_address_is_dropped_from_logs(monkeypatch, selection):
    _configure(monkeypatch)
    FakeTables(monkeypatch)
    _record_resend(monkeypatch)

    def _render(subscriber: Subscriber, items: list[DigestItem], date_iso: str):
        raise ValueError(f"bad recipient {subscriber.email.upper()} / cc someone@else.org")

    monkeypatch.setattr(delivery, "render_for_subscriber", _render)

    with structlog.testing.capture_logs() as logs:
        summary = delivery.deliver_to_subscribers([ALICE], _pool(), DATE)

    assert summary.outcomes[0].error == f"ValueError: {subscribers.ERROR_REDACTED}"
    assert "@" not in json.dumps(logs, ensure_ascii=False, default=str)


def test_pipeline_logs_and_alerts_have_no_subscriber_address(monkeypatch, selection, tmp_path: Path):
    """운영자 주소가 is_owner 구독자이기도 한 런. 로그와 Discord 알림에 주소가 없다."""
    _configure(monkeypatch, EMAIL_TO="me@x.com")
    FakeTables(monkeypatch, [OWNER, ALICE, BOB])
    pipeline = Pipeline(monkeypatch, tmp_path)
    _record_resend(monkeypatch, fail_for=frozenset({"alice@x.com", "me@x.com"}))

    with structlog.testing.capture_logs() as logs:
        pipeline.run()

    dumped = json.dumps(logs, ensure_ascii=False, default=str)
    assert "@" not in dumped, [e for e in logs if "@" in json.dumps(e, default=str)]
    assert pipeline.alerts and all("@" not in alert for alert in pipeline.alerts)
    assert [e for e in logs if e["event"] == "email_send_failed"][0]["to"] == "(구독자)"


def test_db_subscriber_errors_are_scrubbed(monkeypatch):
    def _boom():
        raise RuntimeError('duplicate key value violates unique constraint (email)=(alice@x.com)')

    monkeypatch.setattr(db, "get_supabase_service", _boom)

    with structlog.testing.capture_logs() as logs:
        assert db.fetch_subscribers() is None
        assert db.update_subscriber(ALICE.id, {"status": "active"}) is False
        assert db.record_sent_items(ALICE.id, ["u1"], DATE, arms=["shared"]) is False
        assert db.has_sent_on_date(ALICE.id, DATE) is None

    assert len(logs) >= 5
    assert "@" not in json.dumps(logs, ensure_ascii=False, default=str)
