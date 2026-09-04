"""resend_email 잡 검증: 저장된 레코드로 이메일만 재발송한다.

resend.Emails.send 를 가짜로 바꿔 (a) 오늘 레코드로 실행하면 수신자 수만큼
발송되고 subject 에 prefix 와 레코드 날짜가, HTML 에 첫 항목 제목이 들어 있는지,
(b) 레코드가 없는 날짜면 exit code 1 인지를 고정한다.
설정 주입 방식은 test_newsletter_send.py 를 따른다.
"""
import json
from pathlib import Path

import pytest
import resend

from app.config import get_settings
from app.jobs import resend_email

RECORD_PATH = Path(__file__).parent.parent / "data" / "records" / "digest_2026-09-04.json"


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch, email_to: str) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("EMAIL_FROM", "digest@test.local")
    monkeypatch.setenv("EMAIL_TO", email_to)
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()


def _record_sends(monkeypatch) -> list[dict]:
    """resend.Emails.send 를 호출 기록만 남기는 가짜로 바꾼다."""
    calls: list[dict] = []

    def _send(params):
        calls.append(params)
        return {"id": f"msg-{len(calls)}"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_send))
    return calls


def test_resends_record_to_each_recipient(monkeypatch):
    """(a) --date 2026-09-04: 수신자 2명이면 send 2회, subject 에 prefix 와 09/04,
    HTML 에 레코드 첫 항목 제목이 들어 있다."""
    _configure(monkeypatch, "a@x.com,b@x.com")
    calls = _record_sends(monkeypatch)

    code = resend_email.main(["--date", "2026-09-04", "--subject-prefix", "[재발송 테스트] "])

    assert code == 0
    assert len(calls) == 2
    assert [c["to"][0] for c in calls] == ["a@x.com", "b@x.com"]

    first_title = json.loads(RECORD_PATH.read_text(encoding="utf-8"))["items"][0]["raw"]["title"]
    for c in calls:
        assert c["subject"].startswith("[재발송 테스트] ")
        assert "09/04" in c["subject"]
        assert first_title in c["html"]
        # 아카이브 링크도 레코드 날짜를 가리켜야 한다(오늘 날짜가 아니라).
        assert "2026-09-04.html" in c["html"]


def test_missing_record_exits_1(monkeypatch):
    """(b) 레코드가 없는 날짜면 exit code 1 이고 발송은 한 번도 없다."""
    _configure(monkeypatch, "a@x.com")
    calls = _record_sends(monkeypatch)

    code = resend_email.main(["--date", "1999-01-01"])

    assert code == 1
    assert calls == []
