"""SMTP 로 메일 한 통을 보낸다. 표준 라이브러리만 쓴다.

왜 별도 모듈인가:
    app/mailer.py(다이제스트 발송)와 evals/alert.py(운영 알림)가 같은 전송 코드를 쓴다.
    alert 는 의존성 설치가 실패한 뒤에도 돌아야 해서 pydantic, structlog, resend 를
    가져오는 모듈을 import 할 수 없다. 그래서 이 파일은 표준 라이브러리 밖의 것을
    가져오지 않는다(이 규칙을 깨면 설치 실패 알림이 같이 죽는다).

포트 규칙:
    465 는 처음부터 TLS(SMTP_SSL), 그 밖(기본 587)은 평문으로 붙은 뒤 STARTTLS 로 올린다.
"""
from __future__ import annotations

import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

DEFAULT_SMTP_PORT = 587
SMTP_SSL_PORT = 465
SMTP_TIMEOUT = 30


def parse_port(value: object) -> int:
    """환경변수 값을 포트 번호로. 비었거나 숫자가 아니면 기본값(587)이다."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_SMTP_PORT


def build_message(
    sender: str,
    to: str,
    subject: str,
    html: str | None = None,
    text: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> EmailMessage:
    """메일 한 통을 만든다. html 과 text 를 둘 다 주면 multipart/alternative 가 된다."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid()
    for name, value in (headers or {}).items():
        message[name] = value
    if text is not None:
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype="html")
    else:
        message.set_content(html or "", subtype="html")
    return message


def send_smtp(
    host: str,
    port: int,
    user: str,
    password: str,
    sender: str,
    to: str,
    subject: str,
    html: str | None = None,
    text: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> None:
    """메일 한 통을 보낸다. 실패는 예외로 올린다(호출부가 결과 객체로 바꾼다)."""
    message = build_message(sender, to, subject, html=html, text=text, headers=headers)
    context = ssl.create_default_context()
    if port == SMTP_SSL_PORT:
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context) as client:
            if user:
                client.login(user, password)
            client.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as client:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        if user:
            client.login(user, password)
        client.send_message(message)
