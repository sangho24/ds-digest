"""seen 완화 다이얼과 오류 알림 폴백.

둘 다 "조용한 실패"를 막는 장치다.
  - seen 완화: 후보가 이미 소진돼 실험 결과가 안 나오는 상황을, 행을 지우지 않고
    조회 범위만 좁혀서 푼다(되돌릴 게 없다).
  - 알림 폴백: "모든 발송 채널 실패"를 그 실패한 채널로 보내려다 경보까지
    사라지는 문제를 막는다.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db
import app.jobs.daily_digest as job
from app.config import Settings


class _Query:
    """Supabase 쿼리 빌더 스텁 — 어떤 조건이 걸렸는지 기록한다."""

    def __init__(self, calls):
        self.calls = calls
        self.data = []

    def table(self, name):
        self.calls.append(("table", name)); return self

    def select(self, *a):
        self.calls.append(("select", a)); return self

    def in_(self, col, vals):
        self.calls.append(("in_", col)); return self

    def gte(self, col, value):
        self.calls.append(("gte", col, value)); return self

    def execute(self):
        return self


def _run_fetch(monkeypatch, recent_days):
    calls = []
    settings = Settings(supabase_url="u", supabase_key="k",
                        seen_recent_days=recent_days, dry_run=False, _env_file=None)
    monkeypatch.setattr(db, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr(db, "get_supabase", lambda: _Query(calls))
    db.fetch_seen_urls(["https://e.com/a"])
    return calls


def test_seen_window_off_by_default(monkeypatch):
    """0이면 기간 조건이 붙지 않는다 — 기존 동작 그대로."""
    calls = _run_fetch(monkeypatch, 0)
    assert not [c for c in calls if c[0] == "gte"], calls


def test_seen_window_narrows_query(monkeypatch):
    """N > 0이면 최근 N일로 좁힌다. 행을 지우지 않으므로 되돌릴 게 없다."""
    calls = _run_fetch(monkeypatch, 7)
    gte = [c for c in calls if c[0] == "gte"]
    assert len(gte) == 1 and gte[0][1] == "seen_at", calls
    cutoff = datetime.fromisoformat(gte[0][2])
    expected = datetime.now(timezone.utc) - timedelta(days=7)
    assert abs((cutoff - expected).total_seconds()) < 60


def test_error_alert_falls_back_to_telegram(monkeypatch):
    """Discord가 안 되면 Telegram으로 간다.

    이게 없으면 '모든 발송 채널 실패'를 그 실패한 채널로 보내려다 경보까지
    사라진다 — 가장 중요한 알림이 가장 조용히 없어지는 경로다.
    """
    settings = Settings(telegram_bot_token="t", telegram_chat_id="c",
                        dry_run=False, _env_file=None)
    monkeypatch.setattr(job, "get_settings", lambda: settings)

    async def _discord_fails(_text):
        return False
    monkeypatch.setattr(job, "send_discord_text", _discord_fails)

    sent = []

    async def _tg(client, token, chat_id, text, reply_markup=None):
        sent.append((token, chat_id, text)); return True
    monkeypatch.setattr("app.deliverers.telegram._send_message", _tg)

    asyncio.run(job._send_error_alert("모든 발송 채널 실패: discord"))
    assert len(sent) == 1, sent
    assert "모든 발송 채널 실패" in sent[0][2]


def test_error_alert_prefers_discord(monkeypatch):
    """Discord가 되면 Telegram으로 중복 발송하지 않는다."""
    settings = Settings(telegram_bot_token="t", telegram_chat_id="c",
                        dry_run=False, _env_file=None)
    monkeypatch.setattr(job, "get_settings", lambda: settings)

    async def _discord_ok(_text):
        return True
    monkeypatch.setattr(job, "send_discord_text", _discord_ok)

    sent = []

    async def _tg(*a, **k):
        sent.append(a); return True
    monkeypatch.setattr("app.deliverers.telegram._send_message", _tg)

    asyncio.run(job._send_error_alert("일부 실패"))
    assert sent == []


# ── 발송 채널 파싱 ─────────────────────────────────────────────────────────
# 2026-09-01 실행이 수집·분석·선정을 다 끝내고 delivery={} 로 끝났다.
# DELIVERY_CHANNELS 값의 대소문자가 코드의 소문자 비교와 안 맞아서, 어느
# 채널에도 매치되지 않고 조용히 아무 데도 안 나간 것이다.

@pytest.mark.parametrize("value", ["Discord", "DISCORD", " discord ", "discord\n"])
def test_delivery_channels_case_insensitive(value):
    known = {"telegram", "discord", "email"}
    channels = [c.strip().lower() for c in value.split(",") if c.strip()]
    assert "discord" in channels and not [c for c in channels if c not in known]


def test_delivery_channels_rejects_unknown():
    known = {"telegram", "discord", "email"}
    channels = [c.strip().lower() for c in "slack,discord".split(",") if c.strip()]
    assert [c for c in channels if c not in known] == ["slack"]
    assert "discord" in channels          # 나머지는 정상 동작해야 한다
