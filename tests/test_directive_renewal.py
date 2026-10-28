"""곧 만료될 계기판 권고는 "이미 있음"이 아니라 갱신 대상이다.

TTL 7일과 주간 실행 간격 7일이 같은 길이라, 이번 주 실행이 지난주보다 몇 분만
빨라도 지난주 권고가 아직 살아 있다. 그 상태에서 중복으로 건너뛰면 권고는 곧바로
만료되고 한 주 통째로 되먹임이 빠진다. 실측 2026-10-26: 10-19 12:17:15 KST 권고가
10-26 12:14:43 KST 실행에서 2분 32초 차이로 건너뛰어졌다. 이 파일은 그 경계를 고정한다.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.directives import KST, DASHBOARD_PREFIX, capture, interpret
from evals.directive import RENEWAL_MARGIN, TTL_DAYS, build_advice, classify_advice, new_advice

# 10-26 실행의 권고 스텝 시각. 리포트에는 요약 길이 WARN 이 있었다.
NOW = datetime(2026, 10, 26, 12, 14, 43, tzinfo=KST)
REPORT = {"violations": [{"metric": "summary_stats.one_line_summary.mean", "operator": ">",
                          "threshold": 30, "actual": 30.257, "status": "WARN"}]}


def _row(text: str, expires_at: datetime | str | None, received_at: datetime | None = None) -> dict:
    """capture() 가 남기는 행 모양. expires_at 은 datetime 이면 ISO 로 바꾸고, 문자열이면 그대로 둔다."""
    row = {"text": text, "received_at": (received_at or NOW - timedelta(days=TTL_DAYS)).isoformat()}
    if expires_at is not None:
        row["expires_at"] = expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
    return row


@pytest.fixture()
def advice_text() -> str:
    texts = build_advice(REPORT)
    assert len(texts) == 1
    return texts[0]


def test_renewal_margin_is_shorter_than_ttl_but_covers_cron_jitter():
    """발화 시각 흔들림(수 시간)은 덮고, 같은 날 재실행(~7일 남음)은 못 넘는다."""
    assert timedelta(hours=6) < RENEWAL_MARGIN < timedelta(days=TTL_DAYS)


def test_advice_left_by_a_wednesday_rerun_is_renewed_next_monday(advice_text):
    """월요일 잡이 실패해 수요일에 수동 재실행하면 다음 월요일엔 2일 남는다. 그래도 갱신해야
    수요일 만료부터 그다음 월요일까지 5일 공백이 안 생긴다."""
    existing = [_row(advice_text, NOW + timedelta(days=2), received_at=NOW - timedelta(days=5))]

    assert classify_advice(REPORT, existing, now=NOW) == [(advice_text, True)]


def test_naive_now_is_read_as_kst(advice_text):
    """호출자가 tz 없는 now 를 넘겨도 KST 로 맞춰 TypeError 없이 판정한다."""
    existing = [_row(advice_text, NOW + timedelta(minutes=2))]

    assert classify_advice(REPORT, existing, now=NOW.replace(tzinfo=None)) == [(advice_text, True)]


def test_advice_expiring_in_two_minutes_is_renewed(advice_text):
    """10-26 재현: 옛 권고가 2분 32초 뒤 만료 → 건너뛰지 않고 갱신으로 다시 쓴다."""
    existing = [_row(advice_text, NOW + timedelta(minutes=2, seconds=32))]

    assert classify_advice(REPORT, existing, now=NOW) == [(advice_text, True)]
    assert new_advice(REPORT, existing, now=NOW) == [advice_text]


def test_advice_with_six_days_left_is_still_a_duplicate(advice_text):
    """같은 날 수동 재실행: 남은 수명이 길면 여전히 중복이다. 같은 줄이 쌓이면 안 된다."""
    existing = [_row(advice_text, NOW + timedelta(days=6))]

    assert classify_advice(REPORT, existing, now=NOW) == []
    assert new_advice(REPORT, existing, now=NOW) == []


def test_renewal_boundary_is_inclusive_at_the_margin(advice_text):
    """남은 수명이 정확히 RENEWAL_MARGIN 이면 건너뛰고(>=), 1초 모자라면 갱신한다."""
    exactly = [_row(advice_text, NOW + RENEWAL_MARGIN)]
    one_short = [_row(advice_text, NOW + RENEWAL_MARGIN - timedelta(seconds=1))]

    assert classify_advice(REPORT, exactly, now=NOW) == []
    assert classify_advice(REPORT, one_short, now=NOW) == [(advice_text, True)]


def test_unreadable_expiry_counts_as_alive(advice_text):
    """expires_at 이 깨졌거나 없는 행은 load_raw 와 같은 기준으로 살아 있다고 본다."""
    broken = [_row(advice_text, "언제인지-모름")]
    missing = [_row(advice_text, None)]
    wrong_type = [{"text": advice_text, "expires_at": 12345}]

    for existing in (broken, missing, wrong_type):
        assert classify_advice(REPORT, existing, now=NOW) == [], existing


def test_fresh_advice_is_not_marked_as_renewal(advice_text):
    """살아 있는 같은 권고가 없으면 새 권고(False)다. 사람 지시만 있어도 마찬가지."""
    assert classify_advice(REPORT, [], now=NOW) == [(advice_text, False)]
    human_only = [_row("사람이 쓴 다른 지시", NOW + timedelta(days=10))]
    assert classify_advice(REPORT, human_only, now=NOW) == [(advice_text, False)]


def test_renewed_advice_is_not_renewed_again_while_both_rows_live(advice_text):
    """갱신 직후 옛 행(곧 만료)과 새 행(~7일)이 같이 살아 있을 때, 옛 행만 보고 또 쓰면 안 된다."""
    existing = [_row(advice_text, NOW + timedelta(minutes=2)),
                _row(advice_text, NOW + timedelta(days=TTL_DAYS), received_at=NOW)]
    assert classify_advice(REPORT, existing, now=NOW) == []
    # 순서를 바꿔도 같다.
    assert classify_advice(REPORT, list(reversed(existing)), now=NOW) == []


def test_naive_expiry_is_read_as_kst(advice_text):
    """손으로 넣은 tz 없는 expires_at 과 tz 있는 now 를 비교해도 죽지 않는다."""
    naive = NOW.replace(tzinfo=None) + timedelta(minutes=2)
    existing = [_row(advice_text, naive)]
    assert classify_advice(REPORT, existing, now=NOW) == [(advice_text, True)]


def test_default_now_is_kst_wall_clock(advice_text):
    """now 를 안 주면 KST 현재 시각 기준이다. 갓 쓴 권고(7일 남음)는 중복으로 걸러진다."""
    existing = [_row(advice_text, datetime.now(KST) + timedelta(days=TTL_DAYS))]
    assert new_advice(REPORT, existing) == []


def test_standing_note_carries_each_dashboard_sentence_once(tmp_path):
    """갱신으로 두 행이 겹치는 하루 동안 standing_note 에 같은 문장이 두 번 붙으면 안 된다."""
    raw_path = tmp_path / "directives.jsonl"
    sentence = "한 줄 요약이 길어지고 있다. 30자 이내로 더 압축하라."
    old = NOW - timedelta(days=TTL_DAYS, minutes=-2)  # 2분 뒤 만료
    capture(f"{DASHBOARD_PREFIX} {sentence}", ttl_days=TTL_DAYS, path=raw_path, now=old)
    capture(f"{DASHBOARD_PREFIX} {sentence}", ttl_days=TTL_DAYS, path=raw_path, now=NOW)

    async def _boom(*args, **kwargs):
        raise AssertionError("계기판 줄만 있는데 LLM 을 불렀다")

    directive = asyncio.run(interpret(_boom, "", [], path=raw_path, now=NOW))

    assert directive.standing_note.count("더 압축하라") == 1, directive.standing_note


def test_main_prints_renewal_separately_from_new_advice(tmp_path, monkeypatch, capsys):
    """10-26 상황을 파일로 재현: 2분 뒤 만료될 권고가 있으면 `권고 갱신:` 으로 다시 쓴다."""
    import app.directives as directives_module
    from evals import directive

    raw_path = tmp_path / "directives.jsonl"
    monkeypatch.setattr(directives_module, "DIRECTIVES_PATH", raw_path)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(REPORT, ensure_ascii=False), encoding="utf-8")

    text = build_advice(REPORT)[0]
    old = NOW - timedelta(days=TTL_DAYS, minutes=-2, seconds=-32)
    capture(text, ttl_days=TTL_DAYS, path=raw_path, now=old)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(directive, "datetime", _Clock)

    def _printed() -> list[str]:
        # structlog 가 stdout 에 directive_captured 줄을 섞으므로 main() 의 출력만 고른다.
        return [line for line in capsys.readouterr().out.splitlines()
                if line.startswith(("권고 ", "새로 "))]

    assert directive.main(["evals.directive", str(report_path)]) == 0
    assert _printed() == [f"권고 갱신: {text}"]

    rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert [r["text"] for r in rows] == [text, text]
    assert datetime.fromisoformat(rows[-1]["expires_at"]) == NOW + timedelta(days=TTL_DAYS)

    # 같은 시각에 한 번 더 돌리면 새 행(7일 남음)이 보여 건너뛴다.
    assert directive.main(["evals.directive", str(report_path)]) == 0
    assert _printed() == ["새로 남길 권고가 없습니다."]

    # 살아 있는 권고가 하나도 없으면 `권고 기록:` 으로 찍힌다.
    raw_path.unlink()
    assert directive.main(["evals.directive", str(report_path)]) == 0
    assert _printed() == [f"권고 기록: {text}"]
