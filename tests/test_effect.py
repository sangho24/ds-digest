"""개인화 효과 보고서(evals/effect.py) 검증.

계산부는 순수 함수라 합성 행만으로 본다. 수집부는 app.db 의 조회 함수를 가짜로 바꾼다
(conftest 가 service role 클라이언트 생성도 막는다). 실제 Supabase 호출은 없다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import app.db as db
import app.interest_model as interest_model
from app.config import get_settings
from evals import effect
from evals.effect import EffectData

ALICE = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BOB = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OWNER = "00000000-0000-4000-8000-000000000000"


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _subscriber(sid: str, name: str | None, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": sid,
        "name": name,
        "status": "active",
        "is_owner": False,
        "created_at": "2026-11-01T03:00:00+00:00",
        # 계산이 읽으면 안 되는 값. 보고서 어디에도 나오지 않아야 한다.
        "email": f"{sid[:4]}@secret.example",
        "token": f"tok-{sid[:8]}-SECRET",
    }
    row.update(extra)
    return row


def _sent(sid: str, n: int, arm: str | None = None, day: str = "2026-11-16") -> dict[str, Any]:
    return {
        "subscriber_id": sid,
        "item_url": f"https://example.com/{n}",
        "digest_date": day,
        "arm": arm,
        "position": 1,
    }


def _mark(sid: str, n: int, action: str, at: str = "2026-11-16T01:00:00+00:00") -> dict[str, Any]:
    return {
        "subscriber_id": sid,
        "item_url": f"https://example.com/{n}",
        "action": action,
        "created_at": at,
    }


def _click(sid: str, n: int, at: str = "2026-11-16T02:00:00+00:00") -> dict[str, Any]:
    return {"subscriber_id": sid, "item_url": f"https://example.com/{n}", "created_at": at}


def _two_arms(
    personal: tuple[int, int], baseline: tuple[int, int], clicks: tuple[int, int] = (0, 0)
) -> EffectData:
    """personal, baseline 을 (want 수, skip 수)로 준 데이터. clicks 는 비교군별 클릭 수."""
    sent: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    click_rows: list[dict[str, Any]] = []
    n = 0
    for arm, (want, skip), clicked in (
        ("personal", personal, clicks[0]),
        ("baseline", baseline, clicks[1]),
    ):
        for i in range(want + skip):
            n += 1
            sent.append(_sent(ALICE, n, arm))
            marks.append(_mark(ALICE, n, "want" if i < want else "skip"))
            if i < clicked:
                click_rows.append(_click(ALICE, n))
    return EffectData([_subscriber(ALICE, "앨리스")], sent, marks, click_rows)


# ── 구간 ────────────────────────────────────────────────────────────────


def test_wilson_interval_known_values():
    # Newcombe(1998)의 예: 81/263 → 0.2553 ~ 0.3662
    low, high = effect.wilson_interval(81, 263)
    assert low == pytest.approx(0.2553, abs=5e-5) and high == pytest.approx(0.3662, abs=5e-5)
    low, high = effect.wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=5e-5) and high == pytest.approx(0.7634, abs=5e-5)


def test_wilson_interval_edges():
    assert effect.wilson_interval(0, 0) is None
    low, high = effect.wilson_interval(0, 10)
    assert low == 0.0 and high == pytest.approx(0.2775, abs=5e-5)
    low, high = effect.wilson_interval(10, 10)
    assert high == 1.0 and low == pytest.approx(1 - 0.2775, abs=5e-5)


def test_newcombe_interval_known_value():
    # Newcombe(1998) method 10 의 예: 56/70 대 48/80 → 차이 0.2, 구간 0.0524 ~ 0.3339
    low, high = effect.newcombe_diff_interval(56, 70, 48, 80)
    assert low == pytest.approx(0.0524, abs=5e-5) and high == pytest.approx(0.3339, abs=5e-5)
    assert effect.newcombe_diff_interval(1, 0, 1, 5) is None


# ── 판정 네 경로 ────────────────────────────────────────────────────────


def test_verdict_is_held_when_either_arm_is_small():
    report = effect.compute_report(_two_arms(personal=(25, 4), baseline=(5, 35)))
    want = report["verdict"]["want_ratio"]
    assert want["verdict"] == "판단 보류(표본 부족: 필요 30, 현재 29)"
    # 차이와 구간은 보류여도 그대로 보여 준다.
    assert want["diff"] > 0 and want["ci95"][0] > 0
    assert report["verdict"]["headline"].endswith(want["verdict"])


def test_verdict_no_difference_when_interval_spans_zero():
    report = effect.compute_report(_two_arms(personal=(20, 20), baseline=(18, 22)))
    want = report["verdict"]["want_ratio"]
    assert want["ci95"][0] < 0 < want["ci95"][1]
    assert want["verdict"] == "차이 확인 안 됨"


def test_verdict_positive_when_interval_is_above_zero():
    report = effect.compute_report(_two_arms(personal=(35, 5), baseline=(10, 30)))
    want = report["verdict"]["want_ratio"]
    assert want["ci95"][0] > 0 and want["verdict"] == "개인화 효과 있음"
    assert want["personal"] == {"successes": 35, "total": 40, "rate": 0.875}


def test_verdict_negative_when_interval_is_below_zero():
    report = effect.compute_report(_two_arms(personal=(10, 30), baseline=(35, 5)))
    want = report["verdict"]["want_ratio"]
    assert want["ci95"][1] < 0 and want["verdict"] == "개인화가 오히려 나쁨"


def test_sample_threshold_is_inclusive_at_thirty():
    report = effect.compute_report(_two_arms(personal=(28, 2), baseline=(3, 27)))
    assert report["verdict"]["want_ratio"]["verdict"] == "개인화 효과 있음"


def test_click_verdict_uses_sent_counts_as_denominator():
    """클릭률의 분모는 발송 건이다. 표시가 없어도 클릭 판정은 낼 수 있다."""
    report = effect.compute_report(
        _two_arms(personal=(20, 20), baseline=(20, 20), clicks=(30, 5))
    )
    click = report["verdict"]["click_rate"]
    assert click["personal"]["total"] == 40 and click["personal"]["successes"] == 30
    assert click["verdict"] == "개인화 효과 있음"
    assert report["verdict"]["secondary"] == "참고(클릭률 기준): 개인화 효과 있음"


# ── 집계 규칙 ───────────────────────────────────────────────────────────


def test_latest_mark_wins():
    data = EffectData(
        [_subscriber(ALICE, "앨리스")],
        [_sent(ALICE, 1, "personal"), _sent(ALICE, 2, "personal")],
        [
            # 뒤에 온 행이 더 이르다. 순서가 아니라 시각으로 고른다.
            _mark(ALICE, 1, "skip", "2026-11-16T05:00:00+00:00"),
            _mark(ALICE, 1, "want", "2026-11-16T01:00:00+00:00"),
            _mark(ALICE, 2, "skip", "2026-11-16T01:00:00+00:00"),
            _mark(ALICE, 2, "want", "2026-11-16T09:00:00Z"),
        ],
    )
    summary = effect.compute_report(data)["summary"]
    assert (summary["want"], summary["skip"], summary["responded"]) == (1, 1, 2)
    assert effect.latest_feedback(data.feedback_events) == {
        (ALICE, "https://example.com/1"): "skip",
        (ALICE, "https://example.com/2"): "want",
    }


def test_repeated_clicks_count_once_per_subscriber_and_url():
    data = EffectData(
        [_subscriber(ALICE, "앨리스"), _subscriber(BOB, "밥")],
        [_sent(ALICE, 1), _sent(ALICE, 2), _sent(BOB, 1), _sent(BOB, 2)],
        [],
        [_click(ALICE, 1), _click(ALICE, 1), _click(ALICE, 1), _click(BOB, 1)],
    )
    summary = effect.compute_report(data)["summary"]
    assert summary["clicked"] == 2 and summary["click_rate"] == 0.5


def test_metric_definitions():
    data = EffectData(
        [_subscriber(ALICE, "앨리스")],
        [_sent(ALICE, n) for n in range(1, 11)],
        [_mark(ALICE, 1, "want"), _mark(ALICE, 2, "want"), _mark(ALICE, 3, "want"),
         _mark(ALICE, 4, "skip"), _mark(ALICE, 99, "want")],
        [_click(ALICE, 1), _click(ALICE, 5), _click(ALICE, 98)],
    )
    summary = effect.compute_report(data)["summary"]
    assert summary["sent"] == 10
    assert summary["response_rate"] == pytest.approx(4 / 10)
    assert summary["want_ratio"] == pytest.approx(3 / 4)
    assert summary["click_rate"] == pytest.approx(2 / 10)
    # 발송 기록에 없는 url 의 표시와 클릭은 지표에 넣지 않고 따로 센다.
    assert (summary["unmatched_feedback"], summary["unmatched_clicks"]) == (1, 1)


def test_arm_table_and_unassigned_rows():
    data = EffectData(
        [_subscriber(ALICE, "앨리스"), _subscriber(BOB, "밥")],
        [_sent(ALICE, 1, "personal"), _sent(ALICE, 2, "baseline"), _sent(ALICE, 3, "shared"),
         _sent(BOB, 1, "shared"), _sent(BOB, 2, None), _sent(BOB, 3, "typo")],
        [_mark(ALICE, 1, "want")],
    )
    arms = {row["arm"]: row for row in effect.compute_report(data)["arms"]}
    assert [arms[a]["sent"] for a in effect.ARM_ORDER] == [1, 1, 2, 2]
    assert arms["shared"]["subscribers"] == 2 and arms["unassigned"]["label"] == "미배정"
    assert arms["personal"]["want_ratio"] == 1.0
    assert arms["personal"]["want_ratio_ci95"] == pytest.approx(list(effect.wilson_interval(1, 1)))
    assert arms["baseline"]["want_ratio"] is None and arms["baseline"]["want_ratio_ci95"] is None
    assert arms["baseline"]["click_rate_ci95"] == pytest.approx(list(effect.wilson_interval(0, 1)))


def test_since_filters_by_digest_date_and_weeks_are_iso():
    data = EffectData(
        [_subscriber(ALICE, "앨리스")],
        [_sent(ALICE, 1, day="2026-11-08"), _sent(ALICE, 2, day="2026-11-09"),
         _sent(ALICE, 3, day="2026-11-15"), _sent(ALICE, 4, day="2026-11-16")],
        [_mark(ALICE, 1, "want"), _mark(ALICE, 4, "skip")],
    )
    full = effect.compute_report(data)
    # 11-08 은 일요일(45주), 11-09 ~ 11-15 는 46주, 11-16 은 47주다.
    assert [(w["week"], w["sent"]) for w in full["weekly"]] == [
        ("2026-W45", 1), ("2026-W46", 2), ("2026-W47", 1)
    ]
    recent = effect.compute_report(data, since="2026-11-15")
    assert recent["summary"]["sent"] == 2 and recent["summary"]["want"] == 0
    assert recent["summary"]["unmatched_feedback"] == 1


def test_subscriber_table_fields():
    marks = [_mark(BOB, n, "want", "2026-11-10T20:00:00+00:00") for n in range(1, 21)]
    data = EffectData(
        [_subscriber(ALICE, None), _subscriber(BOB, "밥", status="unsubscribed")],
        [_sent(ALICE, 1), _sent(BOB, 1), _sent(BOB, 2)],
        marks,
        [_click(ALICE, 1, "2026-11-16T16:30:00+00:00")],
    )
    report = effect.compute_report(data)
    alice, bob = report["subscribers"]
    assert alice["subscriber"] == ALICE[:8] and bob["subscriber"] == "밥"
    assert alice["joined"] == "2026-11-01" and bob["status"] == "unsubscribed"
    assert (alice["interest_model_active"], alice["interest_labels"]) == (False, 0)
    # 표시 20건(발송 기록에 없는 url 도 모델은 센다)이면 모델이 켜진다.
    assert (bob["interest_model_active"], bob["interest_labels"]) == (True, 20)
    assert bob["interest_labels_needed"] == interest_model.MIN_LABELS == 20
    assert (bob["sent"], bob["responded"]) == (2, 2)
    # 최근 반응일은 KST 날짜다(UTC 20:00 = 다음 날 05:00, UTC 16:30 = 다음 날 01:30).
    assert bob["last_response"] == "2026-11-11" and alice["last_response"] == "2026-11-17"
    assert report["summary"]["subscribers"] == {"total": 2, "active": 1, "unsubscribed": 1}


def test_owner_label_count_merges_local_labels():
    """is_owner 의 모델 활성 여부는 로컬 표시와 합쳐 센다(늦은 쪽 우선, unsure 는 빠진다)."""
    local = {
        f"https://example.com/{n}": {"human": "want", "labeled_at": "2026-10-01T00:00:00+00:00"}
        for n in range(1, 20)
    }
    local["https://example.com/50"] = {"human": "unsure", "labeled_at": "2026-12-01T00:00:00+00:00"}
    data = EffectData(
        [_subscriber(OWNER, "운영자", is_owner=True), _subscriber(ALICE, "앨리스")],
        [_sent(OWNER, 1)],
        [_mark(OWNER, 1, "skip"), _mark(OWNER, 50, "want"), _mark(OWNER, 60, "want")],
        owner_local_labels=local,
    )
    owner, alice = effect.compute_report(data)["subscribers"]
    # 로컬 19건(1번은 Supabase 표시와 겹침) + 60번. 50번은 나중에 unsure 로 고쳐 빠진다.
    assert owner["interest_labels"] == 20 and owner["interest_model_active"] is True
    assert alice["interest_labels"] == 0
    # 반응률에는 로컬 표시가 들어가지 않는다.
    assert (owner["responded"], owner["skip"]) == (1, 1)


def test_click_metrics_are_blank_when_clicks_are_unavailable():
    data = EffectData(
        [_subscriber(ALICE, "앨리스")],
        [_sent(ALICE, 1, "personal")],
        [_mark(ALICE, 1, "want")],
        clicks_available=False,
        notes=("click_events 를 읽지 못해 클릭 지표를 비웠다",),
    )
    report = effect.compute_report(data)
    assert report["summary"]["click_rate"] is None and report["summary"]["clicked"] is None
    assert report["verdict"]["click_rate"]["verdict"].startswith("측정 안 됨")
    text = effect.render_text(report)
    assert "수집 메모" in text and "측정 안 됨" in text


# ── 이메일 미노출 ───────────────────────────────────────────────────────


def test_report_never_shows_email_or_token():
    data = EffectData(
        [
            _subscriber(ALICE, "앨리스"),
            # 이름 칸에 주소를 적어 둔 경우에도 id 로 대신한다.
            _subscriber(BOB, "bob@secret.example"),
            _subscriber(OWNER, "  ", is_owner=True),
        ],
        [_sent(ALICE, 1, "personal"), _sent(BOB, 1, "baseline"), _sent(OWNER, 1, "shared")],
        [_mark(ALICE, 1, "want")],
        [_click(BOB, 1)],
    )
    report = effect.compute_report(data)
    text = effect.render_text(report)
    dumped = json.dumps(report, ensure_ascii=False)

    for output in (text, dumped):
        assert "@" not in output and "secret.example" not in output and "SECRET" not in output
    assert [r["subscriber"] for r in report["subscribers"]] == ["앨리스", BOB[:8], OWNER[:8]]



def test_anonymize_omits_the_subscriber_table():
    """공개 로그용 실행은 사람별 수치를 내지 않는다. 전체, 비교군, 주차 집계만 남는다."""
    data = EffectData(
        [_subscriber(ALICE, "앨리스"), _subscriber(BOB, None)],
        [_sent(ALICE, 1, "personal"), _sent(BOB, 1, "baseline")],
        [_mark(ALICE, 1, "want")],
    )
    report = effect.compute_report(data, anonymize=True)
    text = effect.render_text(report)

    assert report["subscribers"] == [] and report["subscribers_omitted"] is True
    assert "구독자별 표는 로컬에서 실행해 보라" in text
    for hidden in ("앨리스", ALICE[:8], BOB[:8], "가입일", "관심도 모델"):
        assert hidden not in text
    assert ALICE[:8] not in json.dumps(report, ensure_ascii=False)
    # 집계 구획은 그대로다.
    for heading in ("(a) 전체 요약", "(b) 비교군별", "(c) 핵심 판정", "(e) 주차별 추세"):
        assert heading in text
    assert report["summary"]["sent"] == 2 and len(report["weekly"]) == 1

    full = effect.compute_report(data)
    assert full["subscribers_omitted"] is False and len(full["subscribers"]) == 2


def _positioned(personal: list[int], baseline: list[int]) -> EffectData:
    sent = []
    for n, (arm, position) in enumerate(
        [("personal", p) for p in personal] + [("baseline", p) for p in baseline], start=1
    ):
        sent.append({**_sent(ALICE, n, arm), "position": position})
    return EffectData([_subscriber(ALICE, "앨리스")], sent)


def test_mean_position_is_reported_with_the_verdict():
    report = effect.compute_report(_positioned(personal=[4, 5, 3], baseline=[2, 3, 4]))
    position = report["verdict"]["position"]
    assert position["personal"] == pytest.approx(4.0) and position["baseline"] == pytest.approx(3.0)
    assert position["diff"] == pytest.approx(1.0)
    assert position["warning"] == "지면 위치가 달라 비교가 치우칠 수 있다"
    text = effect.render_text(report)
    assert "평균 지면 위치(1 이 맨 위): personal 4.0 / baseline 3.0" in text
    # 경고는 판정 줄 아래에 붙는다.
    assert text.index("판정(읽고 싶다 비율 기준)") < text.index("지면 위치가 달라 비교가 치우칠 수 있다")
    assert text.index("지면 위치가 달라 비교가 치우칠 수 있다") < text.index("(d) 구독자별")


def test_no_position_warning_when_the_gap_is_small_or_unknown():
    close = effect.compute_report(_positioned(personal=[3, 4], baseline=[3, 3]))
    assert close["verdict"]["position"]["diff"] == pytest.approx(0.5)
    assert close["verdict"]["position"]["warning"] is None
    assert "치우칠 수 있다" not in effect.render_text(close)

    # position 이 없는 행(컬럼 추가 전)과 한쪽 비교군이 빈 경우는 평균을 내지 않는다.
    data = _positioned(personal=[5], baseline=[])
    data.sent_items[0]["position"] = None
    unknown = effect.compute_report(data)
    assert unknown["verdict"]["position"] == {
        "personal": None, "baseline": None, "diff": None, "warning": None
    }
    assert "personal - / baseline -" in effect.render_text(unknown)


def test_baseline_higher_on_the_page_also_warns():
    report = effect.compute_report(_positioned(personal=[1, 1], baseline=[3, 4]))
    assert report["verdict"]["position"]["diff"] == pytest.approx(-2.5)
    assert report["verdict"]["position"]["warning"] is not None


def test_unassigned_rows_are_explained_in_the_report():
    text = effect.render_text(effect.compute_report(_two_arms((1, 1), (1, 1))))
    assert "미배정은 무작위 배정을 거치지 않은 발송 건이다" in text


def test_text_report_has_every_section_and_fixed_caveats():
    text = effect.render_text(effect.compute_report(_two_arms((35, 5), (10, 30))))
    for heading in ("(a) 전체 요약", "(b) 비교군별", "(c) 핵심 판정", "(d) 구독자별",
                    "(e) 주차별 추세", "(f) 해석 주의"):
        assert heading in text
    assert "판정(읽고 싶다 비율 기준): 개인화 효과 있음" in text
    tail = text[text.index("(f) 해석 주의"):]
    assert "사람 간 편차가 구간보다 클 수 있다" in tail and "보안 스캐너" in tail
    # 긴 하이픈(U+2014)은 출력에 쓰지 않는다.
    assert chr(0x2014) not in text


# ── 수집과 CLI ──────────────────────────────────────────────────────────


def _supabase(monkeypatch, url: str = "https://proj.supabase.co") -> None:
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_KEY", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key" if url else "")
    get_settings.cache_clear()


def _fake_tables(
    monkeypatch,
    subscribers: list[dict[str, Any]] | None,
    sent: list[dict[str, Any]] | None,
    marks: list[dict[str, Any]] | None = None,
    clicks: list[dict[str, Any]] | None = None,
) -> list[str | None]:
    since_seen: list[str | None] = []
    monkeypatch.setattr(db, "fetch_subscribers", lambda active_only=False: subscribers)
    monkeypatch.setattr(
        db, "fetch_sent_items_for_report", lambda since=None: since_seen.append(since) or sent
    )
    monkeypatch.setattr(db, "fetch_feedback_events_for_report", lambda: marks)
    monkeypatch.setattr(db, "fetch_click_events_for_report", lambda: clicks)
    return since_seen


def test_cli_exits_zero_without_supabase(monkeypatch, capsys):
    _supabase(monkeypatch, url="")
    monkeypatch.setattr(db, "fetch_subscribers", lambda *a, **k: pytest.fail("조회 금지"))

    assert effect.main(["report"]) == 0
    assert "Supabase 가 설정되지 않았다" in capsys.readouterr().out

    assert effect.main(["report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no_data"


@pytest.mark.parametrize(
    ("subscribers", "sent", "expected"),
    [
        (None, [], "subscribers 조회에 실패"),
        ([], [], "구독자 0명"),
        ([{"id": ALICE}], None, "sent_items 조회에 실패"),
        ([{"id": ALICE}], [], "발송 기록이 없다"),
    ],
)
def test_cli_names_what_is_missing_and_exits_zero(monkeypatch, capsys, subscribers, sent, expected):
    _supabase(monkeypatch)
    _fake_tables(monkeypatch, subscribers, sent)

    assert effect.main(["report"]) == 0
    assert expected in capsys.readouterr().out


def test_cli_exits_zero_when_fetch_is_blocked(monkeypatch, capsys):
    """conftest 가 클라이언트 생성을 막아 둔 상태 그대로(= 키가 틀렸거나 네트워크가 없는 CI)."""
    _supabase(monkeypatch)
    assert effect.main(["report"]) == 0
    assert "subscribers 조회에 실패" in capsys.readouterr().out


def test_cli_report_text_json_and_out(monkeypatch, capsys, tmp_path: Path):
    _supabase(monkeypatch)
    since_seen = _fake_tables(
        monkeypatch,
        [_subscriber(ALICE, "앨리스")],
        [_sent(ALICE, 1, "personal"), _sent(ALICE, 2, "baseline")],
        [_mark(ALICE, 1, "want")],
        None,
    )
    out = tmp_path / "nested" / "effect.json"

    assert effect.main(["report", "--json", "--since", "2026-11-01", "--out", str(out)]) == 0

    printed = capsys.readouterr().out
    payload = json.loads(printed)
    assert payload["status"] == "ok" and payload["since"] == "2026-11-01"
    assert since_seen == ["2026-11-01"]
    assert payload["summary"]["sent"] == 2 and payload["summary"]["click_rate"] is None
    assert any("click_events" in note for note in payload["notes"])
    assert out.read_text(encoding="utf-8") == printed
    assert "@" not in printed and "SECRET" not in printed

    assert effect.main(["report", "--anonymize"]) == 0
    text = capsys.readouterr().out
    assert "(c) 핵심 판정" in text and "앨리스" not in text and ALICE[:8] not in text
    assert "구독자별 표는 로컬에서 실행해 보라" in text

    assert effect.main(["report", "--anonymize", "--json"]) == 0
    anonymous = json.loads(capsys.readouterr().out)
    assert anonymous["subscribers"] == [] and anonymous["subscribers_omitted"] is True


def test_collect_drops_email_and_token(monkeypatch):
    _supabase(monkeypatch)
    _fake_tables(monkeypatch, [_subscriber(ALICE, "앨리스")], [_sent(ALICE, 1)], [], [])

    data = effect.collect()

    # 문체 요청은 유무(has_style_note)만 남고 내용은 넘어오지 않는다.
    assert set(data.subscribers[0]) == set(effect._SUBSCRIBER_FIELDS) | {"has_style_note"}
    assert "@" not in repr(data) and "SECRET" not in repr(data)
    assert any("arm 이 하나도 없다" in note for note in data.notes)


def test_cli_rejects_malformed_since(monkeypatch):
    with pytest.raises(SystemExit):
        effect.main(["report", "--since", "11/16"])


# ── 보고서용 전체 조회(app/db.py) ───────────────────────────────────────


class _PagedClient:
    """select 를 쪽 단위로 돌려주는 supabase 대역. 지정한 컬럼을 고르면 실패한다."""

    def __init__(self, rows: list[dict[str, Any]], missing_columns: frozenset[str] = frozenset()):
        self.rows = rows
        self.missing = missing_columns
        self.selects: list[str] = []
        self.filters: list[tuple[str, str]] = []
        self.ranges: list[tuple[int, int]] = []

    def table(self, name: str) -> "_PagedClient":
        self.name = name
        return self

    def select(self, columns: str) -> "_PagedClient":
        self.selects.append(columns)
        self._columns = columns.split(",")
        return self

    def gte(self, column: str, value: str) -> "_PagedClient":
        self.filters.append((column, value))
        return self

    def order(self, column: str) -> "_PagedClient":
        return self

    def range(self, start: int, end: int) -> "_PagedClient":
        self.ranges.append((start, end))
        self._range = (start, end)
        return self

    def execute(self) -> Any:
        if self.missing & set(self._columns):
            raise RuntimeError("column sent_items.arm does not exist")
        start, end = self._range

        class _Result:
            data = self.rows[start:end + 1]

        return _Result()


def test_report_fetch_reads_every_page(monkeypatch):
    rows = [{"subscriber_id": ALICE, "item_url": f"u{i}"} for i in range(2300)]
    client = _PagedClient(rows)
    monkeypatch.setattr(db, "get_supabase_service", lambda: client)

    fetched = db.fetch_sent_items_for_report("2026-11-01")

    assert fetched is not None and len(fetched) == 2300
    assert client.ranges == [(0, 999), (1000, 1999), (2000, 2999)]
    assert set(client.filters) == {("digest_date", "2026-11-01")}


def test_report_fetch_falls_back_without_arm_columns(monkeypatch):
    client = _PagedClient([{"subscriber_id": ALICE, "item_url": "u1"}], frozenset({"arm"}))
    monkeypatch.setattr(db, "get_supabase_service", lambda: client)

    assert db.fetch_sent_items_for_report() == [{"subscriber_id": ALICE, "item_url": "u1"}]
    assert client.selects == [
        "subscriber_id,item_url,digest_date,arm,position",
        "subscriber_id,item_url,digest_date",
    ]


def test_report_fetch_failures_are_none():
    # conftest 가 클라이언트 생성을 막아 두었다.
    assert db.fetch_sent_items_for_report() is None
    assert db.fetch_feedback_events_for_report() is None
    assert db.fetch_click_events_for_report() is None


# ── 문체 요청 열 (PROGRESS §59) ──────────────────────────────────────────


def test_subscriber_table_shows_only_whether_a_style_request_exists(monkeypatch):
    data = EffectData(
        [_subscriber(ALICE, "앨리스", has_style_note=True), _subscriber(BOB, "밥")],
        [_sent(ALICE, 1), _sent(BOB, 1)],
    )

    report = effect.compute_report(data)
    text = effect.render_text(report)

    assert [row["has_style_note"] for row in report["subscribers"]] == [True, False]
    header = next(line for line in text.splitlines() if "관심도 모델" in line)
    assert "문체 요청" in header
    alice_line = next(line for line in text.splitlines() if line.strip().startswith("앨리스"))
    bob_line = next(line for line in text.splitlines() if line.strip().startswith("밥"))
    assert "있음" in alice_line and "없음" in bob_line

    # 익명 모드에서는 지금처럼 표 전체가 생략된다.
    anonymous = effect.render_text(effect.compute_report(data, anonymize=True))
    assert "문체 요청" not in anonymous and "있음" not in anonymous


def test_collect_keeps_only_the_presence_of_the_style_note(monkeypatch):
    _supabase(monkeypatch)
    _fake_tables(
        monkeypatch,
        [
            _subscriber(ALICE, "앨리스", style_note="비밀스러운 문체 요청"),
            _subscriber(BOB, "밥", style_note="  "),
        ],
        [_sent(ALICE, 1)], [], [],
    )

    data = effect.collect()

    assert [row["has_style_note"] for row in data.subscribers] == [True, False]
    assert "비밀스러운" not in repr(data) and "style_note'" not in repr(data).replace("has_style_note'", "")
