"""발송부 추상화(app/mailer.py)와 설정 파싱, 운영 알림 수신자 분리 검증.

실제 Resend·SMTP 서버로는 아무것도 나가지 않는다. resend.Emails.send 와
smtplib.SMTP / SMTP_SSL 을 호출 기록만 남기는 가짜로 바꾼다.
"""
from __future__ import annotations

import asyncio
from email.message import EmailMessage
from typing import Any

import pytest
import resend

import app.newsletter as newsletter
import app.smtp_transport as smtp_transport
from app import mailer
from app.config import Settings, get_settings
from evals import alert


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch, provider: str = "", **env: str) -> None:
    """발송에 영향을 주는 env 를 전부 명시한다(개발자 .env 값이 새지 않게)."""
    values = {
        "EMAIL_PROVIDER": provider,
        "RESEND_API_KEY": "re_test",
        "EMAIL_FROM": "digest@test.local",
        "EMAIL_TO": "a@x.com",
        "DRY_RUN": "false",
        "SMTP_HOST": "",
        "SMTP_PORT": "",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "SUPABASE_URL": "",
        "SUPABASE_KEY": "",
        "SUPABASE_SERVICE_KEY": "",
    }
    values.update(env)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _record_resend(monkeypatch, error: Exception | None = None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _send(params: dict[str, Any]) -> dict[str, str]:
        calls.append(params)
        if error is not None:
            raise error
        return {"id": f"msg-{len(calls)}"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_send))
    return calls


class _FakeSMTP:
    """smtplib.SMTP / SMTP_SSL 대역. 호출 순서와 보낸 메시지를 기록한다."""

    instances: list["_FakeSMTP"] = []
    fail_on_send: Exception | None = None

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.messages: list[EmailMessage] = []
        self.login_args: tuple[str, str] | None = None
        type(self).instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: object = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        if type(self).fail_on_send is not None:
            raise type(self).fail_on_send
        self.calls.append("send")
        self.messages.append(message)


class _FakeSMTPSSL(_FakeSMTP):
    instances: list["_FakeSMTP"] = []


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances, _FakeSMTPSSL.instances = [], []
    _FakeSMTP.fail_on_send = _FakeSMTPSSL.fail_on_send = None
    monkeypatch.setattr(smtp_transport.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtp_transport.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    return _FakeSMTP, _FakeSMTPSSL


# ── 설정 파싱: 워크플로가 내려보내는 빈 문자열 ─────────────────────────────


def test_empty_env_values_fall_back_to_defaults(monkeypatch):
    """시크릿이 없으면 빈 문자열이 온다. SMTP_PORT 가 int 파싱에서 죽으면 다이제스트 전체가 죽는다."""
    for name in ("EMAIL_PROVIDER", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                 "SUPABASE_SERVICE_KEY", "ALERT_EMAIL_TO"):
        monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)
    assert settings.email_provider == "resend"
    assert settings.smtp_port == 587
    assert settings.alert_email_to == ""


def test_provider_is_normalized_and_port_is_parsed(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", " SMTP ")
    monkeypatch.setenv("SMTP_PORT", "465")
    settings = Settings(_env_file=None)
    assert settings.email_provider == "smtp"
    assert settings.smtp_port == 465


def test_service_key_falls_back_to_supabase_key():
    plain = Settings(supabase_url="https://p.supabase.co", supabase_key="anon", _env_file=None)
    assert plain.supabase_service_key_effective == "anon"
    assert plain.subscribers_enabled is True

    both = Settings(supabase_url="https://p.supabase.co", supabase_key="anon",
                    supabase_service_key="service", _env_file=None)
    assert both.supabase_service_key_effective == "service"

    assert Settings(supabase_url="", supabase_key="", supabase_service_key="",
                    _env_file=None).subscribers_enabled is False


# ── resend 경로 ─────────────────────────────────────────────────────────


def test_resend_request_without_headers_is_unchanged(monkeypatch):
    """헤더가 없으면 요청 본문은 추상화 이전과 키까지 같다."""
    _configure(monkeypatch)
    calls = _record_resend(monkeypatch)

    result = mailer.send("a@x.com", "제목", "<p>본문</p>")

    assert result.ok and result.provider == "resend" and result.message_id == "msg-1"
    assert calls == [{
        "from": "digest@test.local", "to": ["a@x.com"], "subject": "제목", "html": "<p>본문</p>",
    }]
    assert resend.api_key == "re_test"


def test_resend_passes_headers(monkeypatch):
    _configure(monkeypatch)
    calls = _record_resend(monkeypatch)

    mailer.send("a@x.com", "제목", "<p>본문</p>", {"List-Unsubscribe": "<https://e/x>"})

    assert calls[0]["headers"] == {"List-Unsubscribe": "<https://e/x>"}


def test_resend_failure_is_a_result_not_an_exception(monkeypatch):
    _configure(monkeypatch)
    _record_resend(monkeypatch, error=RuntimeError("rejected"))

    result = mailer.send("a@x.com", "제목", "<p>본문</p>")

    assert result.ok is False and "rejected" in result.error


def test_resend_without_key_does_not_call_the_sdk(monkeypatch):
    _configure(monkeypatch, RESEND_API_KEY="")
    calls = _record_resend(monkeypatch)

    result = mailer.send("a@x.com", "제목", "<p>본문</p>")

    assert result.ok is False and result.error == "resend_api_key_missing"
    assert calls == []


def test_unknown_provider_fails_without_sending(monkeypatch):
    _configure(monkeypatch, provider="sendgrid")
    calls = _record_resend(monkeypatch)

    result = mailer.send("a@x.com", "제목", "<p>본문</p>")

    assert result.ok is False and result.error == "email_provider_unknown"
    assert calls == []


# ── smtp 경로 ───────────────────────────────────────────────────────────


def test_smtp_587_uses_starttls_and_login(monkeypatch, fake_smtp):
    plain, ssl_class = fake_smtp
    _configure(monkeypatch, provider="smtp", SMTP_HOST="smtp.test.local",
               SMTP_USER="user", SMTP_PASSWORD="pw")
    resend_calls = _record_resend(monkeypatch)

    result = mailer.send(
        "b@x.com", "한글 제목", "<p>본문</p>",
        {"List-Unsubscribe": "<https://e/x?t=tok&a=unsubscribe>",
         "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    )

    assert result.ok and result.provider == "smtp"
    assert resend_calls == [] and ssl_class.instances == []
    client = plain.instances[0]
    assert (client.host, client.port) == ("smtp.test.local", 587)
    assert client.calls == ["ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert client.login_args == ("user", "pw")
    message = client.messages[0]
    assert message["To"] == "b@x.com" and message["From"] == "digest@test.local"
    assert message["Subject"] == "한글 제목"
    assert message["List-Unsubscribe"] == "<https://e/x?t=tok&a=unsubscribe>"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert message.get_content_type() == "text/html"
    assert "<p>본문</p>" in message.get_content()


def test_smtp_465_uses_ssl_without_starttls(monkeypatch, fake_smtp):
    plain, ssl_class = fake_smtp
    _configure(monkeypatch, provider="smtp", SMTP_HOST="smtp.test.local", SMTP_PORT="465",
               SMTP_USER="user", SMTP_PASSWORD="pw")

    assert mailer.send("b@x.com", "제목", "<p>본문</p>").ok

    assert plain.instances == []
    assert ssl_class.instances[0].port == 465
    assert ssl_class.instances[0].calls == ["login", "send", "quit"]


def test_smtp_failure_is_a_result_not_an_exception(monkeypatch, fake_smtp):
    plain, _ = fake_smtp
    plain.fail_on_send = RuntimeError("550 mailbox unavailable")
    _configure(monkeypatch, provider="smtp", SMTP_HOST="smtp.test.local")

    result = mailer.send("b@x.com", "제목", "<p>본문</p>")

    assert result.ok is False and "550" in result.error


def test_smtp_without_host_does_not_connect(monkeypatch, fake_smtp):
    plain, ssl_class = fake_smtp
    _configure(monkeypatch, provider="smtp")

    result = mailer.send("b@x.com", "제목", "<p>본문</p>")

    assert result.ok is False and result.error == "smtp_host_missing"
    assert plain.instances == [] and ssl_class.instances == []


def test_send_digest_goes_through_smtp_when_configured(monkeypatch, fake_smtp):
    """send_digest 가 발송부 추상화를 쓴다. EMAIL_TO 각 주소에 한 통씩, 헤더 없이."""
    plain, _ = fake_smtp
    _configure(monkeypatch, provider="smtp", SMTP_HOST="smtp.test.local", EMAIL_TO="a@x.com,b@x.com")
    monkeypatch.setattr(newsletter, "render_digest_email", lambda *a, **k: "<html></html>")

    assert asyncio.run(newsletter.send_digest([])) is True

    assert [c.messages[0]["To"] for c in plain.instances] == ["a@x.com", "b@x.com"]
    assert all(c.messages[0]["List-Unsubscribe"] is None for c in plain.instances)


# ── 운영 알림 수신자 분리 (evals/alert.py) ─────────────────────────────────


def _capture_alert_post(monkeypatch) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    def _fake_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        sent.update(payload)
        return 200, ""

    monkeypatch.setattr(alert, "_post", _fake_post)
    return sent


def test_alert_uses_alert_email_to_when_set(monkeypatch):
    sent = _capture_alert_post(monkeypatch)
    env = {"RESEND_API_KEY": "k", "EMAIL_FROM": "a@b.c",
           "EMAIL_TO": "digest-reader@x.com", "ALERT_EMAIL_TO": "ops@x.com, ops2@x.com"}

    assert alert.send_email("본문", env)[1] is True
    assert sent["to"] == ["ops@x.com", "ops2@x.com"]


@pytest.mark.parametrize("alert_to", [None, "", "  "])
def test_alert_falls_back_to_email_to(monkeypatch, alert_to):
    """ALERT_EMAIL_TO 가 없거나(시크릿 미등록 시 빈 문자열) 비면 EMAIL_TO 로 간다."""
    sent = _capture_alert_post(monkeypatch)
    env = {"RESEND_API_KEY": "k", "EMAIL_FROM": "a@b.c", "EMAIL_TO": "d@e.f"}
    if alert_to is not None:
        env["ALERT_EMAIL_TO"] = alert_to

    assert alert.send_email("본문", env)[1] is True
    assert sent["to"] == ["d@e.f"]


def test_alert_goes_through_smtp_when_provider_is_smtp(monkeypatch, fake_smtp):
    plain, _ = fake_smtp
    sent = _capture_alert_post(monkeypatch)
    env = {"EMAIL_PROVIDER": "smtp", "SMTP_HOST": "smtp.test.local", "SMTP_PORT": "",
           "SMTP_USER": "u", "SMTP_PASSWORD": "p", "EMAIL_FROM": "a@b.c",
           "EMAIL_TO": "d@e.f", "ALERT_EMAIL_TO": "ops@x.com"}

    channel, ok, _detail = alert.send_email("알림 본문", env, subject="[DS Digest] 실패")

    assert (channel, ok) == ("email", True)
    assert sent == {}, "smtp 경로에서는 Resend API 를 부르지 않는다"
    message = plain.instances[0].messages[0]
    assert plain.instances[0].port == 587
    assert message["To"] == "ops@x.com" and message["Subject"] == "[DS Digest] 실패"
    assert "알림 본문" in message.get_content()


def test_alert_module_stays_stdlib_only():
    """알림은 의존성 설치가 실패한 뒤에도 돌아야 한다. SMTP 전송 모듈도 표준 라이브러리만 쓴다."""
    source = smtp_transport.__file__
    text = open(source, encoding="utf-8").read()
    imports = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    allowed = ("__future__", "smtplib", "ssl", "collections.abc", "email.")
    assert all(any(name in line for name in allowed) for line in imports), imports
