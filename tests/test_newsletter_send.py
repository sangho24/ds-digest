"""send_digest 의 수신자별 개별 발송 검증.

예전에는 EMAIL_TO 전원을 한 통의 `to` 에 담아 보냈다. 그러면 To 헤더로 서로의
주소가 노출되고, 한 주소가 거절되면 그 통 전체가 실패해 본인도 못 받았다.
여기서는 수신자마다 send 가 따로 불리는지, 한 명의 실패가 다음 발송을 막지
않는지, 반환값(bool) 계약이 유지되는지를 고정한다.
"""
import asyncio

import pytest
import resend
import structlog

import app.newsletter as newsletter
from app.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def stub_render(monkeypatch):
    """템플릿 렌더는 별도 테스트(test_email_template)가 맡는다. 여기서는 발송만 본다."""
    monkeypatch.setattr(newsletter, "render_digest_email", lambda *a, **k: "<html></html>")


def _configure(monkeypatch, email_to: str, api_key: str = "re_test", dry_run: bool = False) -> None:
    monkeypatch.setenv("RESEND_API_KEY", api_key)
    monkeypatch.setenv("EMAIL_TO", email_to)
    monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")
    get_settings.cache_clear()


def _record_sends(monkeypatch, fail_for: set[str] = frozenset()) -> list[dict]:
    """resend.Emails.send 를 호출 기록만 남기는 가짜로 바꾼다. fail_for 에 든
    주소로 보내면 예외를 던진다."""
    calls: list[dict] = []

    def _send(params):
        calls.append(params)
        to = params["to"]
        if any(addr in fail_for for addr in to):
            raise RuntimeError(f"rejected: {to}")
        return {"id": f"msg-{len(calls)}"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_send))
    return calls


def _run(coro):
    return asyncio.run(coro)


def test_each_recipient_gets_own_send(monkeypatch):
    """수신자 3명이면 send 3회, 각 호출의 to 는 한 명씩이고 서로 다르다."""
    _configure(monkeypatch, "a@x.com, b@x.com,c@x.com")
    calls = _record_sends(monkeypatch)

    assert _run(newsletter.send_digest([])) is True
    assert len(calls) == 3
    assert all(len(c["to"]) == 1 for c in calls)
    assert [c["to"][0] for c in calls] == ["a@x.com", "b@x.com", "c@x.com"]
    # 제목·본문은 모든 수신자에게 같다(한 번만 만든 것을 재사용).
    assert len({c["subject"] for c in calls}) == 1
    assert len({c["html"] for c in calls}) == 1


def test_one_failure_does_not_block_others(monkeypatch):
    """두 번째 수신자에서 예외가 나도 세 번째는 발송되고, 반환은 True,
    email_partial_failure 경고가 하나 남는다."""
    _configure(monkeypatch, "a@x.com,b@x.com,c@x.com")
    calls = _record_sends(monkeypatch, fail_for={"b@x.com"})

    with structlog.testing.capture_logs() as logs:
        result = _run(newsletter.send_digest([]))

    assert result is True
    assert [c["to"][0] for c in calls] == ["a@x.com", "b@x.com", "c@x.com"]

    partial = [e for e in logs if e["event"] == "email_partial_failure"]
    assert len(partial) == 1
    assert partial[0]["failed"] == ["b@x.com"]
    assert partial[0]["sent"] == "2/3"

    failed = [e for e in logs if e["event"] == "email_send_failed"]
    assert len(failed) == 1 and failed[0]["to"] == "b@x.com"
    sent = [e["to"] for e in logs if e["event"] == "email_sent"]
    assert sent == ["a@x.com", "c@x.com"]


def test_all_failures_return_false(monkeypatch):
    _configure(monkeypatch, "a@x.com,b@x.com")
    calls = _record_sends(monkeypatch, fail_for={"a@x.com", "b@x.com"})

    with structlog.testing.capture_logs() as logs:
        result = _run(newsletter.send_digest([]))

    assert result is False
    assert len(calls) == 2
    # 전원 실패는 부분 실패가 아니다.
    assert not [e for e in logs if e["event"] == "email_partial_failure"]


def test_no_recipients_skips_send(monkeypatch):
    _configure(monkeypatch, " , ")
    calls = _record_sends(monkeypatch)

    with structlog.testing.capture_logs() as logs:
        result = _run(newsletter.send_digest([]))

    assert result is False
    assert calls == []
    assert [e for e in logs if e["event"] == "email_recipients_empty"]


def test_dry_run_sends_nothing(monkeypatch):
    _configure(monkeypatch, "a@x.com,b@x.com", dry_run=True)
    calls = _record_sends(monkeypatch)

    assert _run(newsletter.send_digest([])) is True
    assert calls == []
