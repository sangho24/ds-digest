"""메일 발송부 추상화. 한 통 단위로 보내고 실패는 예외 대신 결과 객체로 돌려준다.

설정 EMAIL_PROVIDER 로 경로를 고른다:
    "resend" (기본)  Resend SDK. 기존 동작 그대로다.
    "smtp"           표준 라이브러리 smtplib (app/smtp_transport.py).

왜 필요한가:
    Resend 무료 플랜의 onboarding@resend.dev 발신은 가입자 본인 주소로만 나간다.
    지인에게 보내려면 도메인 인증을 하거나 다른 발송 경로(SMTP)를 써야 하는데,
    발송 코드가 Resend SDK 에 직접 묶여 있으면 경로를 바꿀 때마다 호출부를 고쳐야 한다.

렌더, 수신자 목록, 드라이런 판단은 호출부(app/newsletter.py, app/subscriber_delivery.py)의
일이다. 이 모듈은 주어진 한 통을 보내기만 한다.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import resend

from app.config import Settings, get_settings
from app.smtp_transport import send_smtp

PROVIDER_RESEND = "resend"
PROVIDER_SMTP = "smtp"
_ERROR_LIMIT = 200


@dataclass(frozen=True)
class SendResult:
    """발송 한 통의 결과. ok 가 False 면 error 에 이유가 있다."""

    ok: bool
    provider: str
    error: str = ""
    message_id: str = ""


def configuration_problem(settings: Settings | None = None) -> str | None:
    """지금 설정으로 발송할 수 없으면 이유(로그 이벤트 이름)를, 가능하면 None 을 돌려준다."""
    settings = settings or get_settings()
    provider = settings.email_provider
    if provider == PROVIDER_RESEND:
        return None if settings.resend_api_key else "resend_api_key_missing"
    if provider == PROVIDER_SMTP:
        return None if settings.smtp_host else "smtp_host_missing"
    return "email_provider_unknown"


def send(
    to: str,
    subject: str,
    html: str,
    headers: Mapping[str, str] | None = None,
) -> SendResult:
    """메일 한 통을 보낸다. 예외를 올리지 않는다.

    headers 는 List-Unsubscribe 같은 추가 헤더다. 비어 있으면 Resend 요청에 headers
    키 자체를 넣지 않는다(헤더 없는 기존 발송의 요청 본문이 그대로 유지된다).
    """
    settings = get_settings()
    provider = settings.email_provider
    problem = configuration_problem(settings)
    if problem is not None:
        return SendResult(ok=False, provider=provider, error=problem)

    try:
        if provider == PROVIDER_SMTP:
            send_smtp(
                settings.smtp_host,
                settings.smtp_port,
                settings.smtp_user,
                settings.smtp_password,
                settings.email_from,
                to,
                subject,
                html=html,
                headers=headers,
            )
            return SendResult(ok=True, provider=provider)

        resend.api_key = settings.resend_api_key
        params: dict[str, object] = {
            "from": settings.email_from,
            "to": [to],
            "subject": subject,
            "html": html,
        }
        if headers:
            params["headers"] = dict(headers)
        response = resend.Emails.send(params)
        message_id = str(response.get("id") or "") if isinstance(response, Mapping) else ""
        return SendResult(ok=True, provider=provider, message_id=message_id)
    except Exception as e:
        return SendResult(ok=False, provider=provider, error=str(e)[:_ERROR_LIMIT])
