"""지인 초대제 구독자 명단.

    python -m app.subscribers list
    python -m app.subscribers add EMAIL [--name N] [--owner]
    python -m app.subscribers remove EMAIL
    python -m app.subscribers purge EMAIL --yes

명단은 Supabase subscribers 테이블에 있다(스키마와 RLS 는 supabase/ 가 정본).
가입 화면은 없고 운영자가 이 CLI 로 직접 넣는다. token 은 메일 안의 피드백 링크와
수신거부 링크에 실려 구독자를 식별하므로, 로그와 list 출력에는 앞 4자만 보인다.

user_id 규칙:
    is_owner 인 구독자는 기존 단일 사용자와 같은 사람이라 user_id "default" 를 쓴다
    (프로필, 관심도 라벨, 퀴즈 기록이 전부 그 키에 쌓여 있다). 나머지는 subscriber.id 다.
"""
from __future__ import annotations

import argparse
import io
import re
import secrets
import sys
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel

from app.config import get_settings

logger = structlog.get_logger()

STATUS_ACTIVE = "active"
STATUS_UNSUBSCRIBED = "unsubscribed"
OWNER_USER_ID = "default"
_TOKEN_VISIBLE = 4


class Subscriber(BaseModel):
    """subscribers 테이블 한 행. 선정과 발송에 쓰는 컬럼만 담는다."""

    id: str
    email: str
    name: str | None = None
    status: str = STATUS_ACTIVE
    token: str
    is_owner: bool = False

    @property
    def user_id(self) -> str:
        """선정과 관심도 모델에 쓰는 사용자 키."""
        return OWNER_USER_ID if self.is_owner else self.id

    @property
    def short_id(self) -> str:
        """로그용 식별자(id 앞 8자)."""
        return self.id[:8]


def normalize_email(email: str) -> str:
    return email.strip().lower()


def mask_email(email: str) -> str:
    """로그와 화면에 남길 때 쓰는 가린 주소. 첫 글자와 도메인만 남긴다."""
    local, sep, domain = email.strip().partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"


# 주소가 섞인 오류 문구 대신 로그에 남기는 문구.
ERROR_REDACTED = "(주소 포함 오류, 생략)"
_ERROR_LIMIT = 200


def scrub_error(error: object, email: str | None = None) -> str:
    """로그와 알림에 실어도 되는 오류 문구.

    저장소가 공개라 워크플로 로그는 누구에게나 보인다. 발송 라이브러리와 PostgREST 의
    오류 문구에는 수신 주소가 섞여 나올 수 있으므로, 아는 주소(email)를 지우고 200자로
    자른 뒤에도 '@' 가 남아 있으면 문구를 통째로 버린다. 가린 주소(a***@domain)도 남기지
    않는다.
    """
    text = str(error)
    if email:
        text = re.sub(re.escape(email), "(수신자)", text, flags=re.IGNORECASE)
    text = text[:_ERROR_LIMIT]
    return ERROR_REDACTED if "@" in text else text


def mask_token(token: str) -> str:
    """token 은 앞 4자만 보인다."""
    return f"{token[:_TOKEN_VISIBLE]}…" if token else ""


def _to_subscriber(row: dict[str, Any]) -> Subscriber | None:
    """행 하나를 모델로. 필수 컬럼이 빠진 행은 건너뛴다(한 행이 전체를 막지 않는다)."""
    try:
        return Subscriber(
            id=str(row["id"]),
            email=normalize_email(str(row["email"])),
            name=row.get("name"),
            status=str(row.get("status") or STATUS_ACTIVE),
            token=str(row["token"]),
            is_owner=bool(row.get("is_owner")),
        )
    except Exception as e:
        logger.warning("subscriber_row_invalid", error=type(e).__name__)
        return None


def _list(active_only: bool) -> list[Subscriber] | None:
    """명단 조회. Supabase 미설정이면 빈 목록, 조회 실패면 None."""
    if not get_settings().subscribers_enabled:
        return []
    from app.db import fetch_subscribers

    rows = fetch_subscribers(active_only=active_only)
    if rows is None:
        return None
    return [s for s in (_to_subscriber(row) for row in rows) if s is not None]


def list_active_subscribers() -> list[Subscriber]:
    """발송 대상(status=active). 미설정이거나 조회에 실패하면 빈 목록이다(fail-open).

    빈 목록이면 파이프라인은 구독자 단계를 통째로 건너뛰어 기존 동작과 같아진다.
    """
    return [s for s in (_list(active_only=True) or []) if s.status == STATUS_ACTIVE]


def list_all_subscribers() -> list[Subscriber] | None:
    """상태와 무관한 전체 명단. 조회 실패는 None(CLI 가 0명과 구분해 알린다)."""
    return _list(active_only=False)


def find_subscriber(email: str) -> Subscriber | None:
    target = normalize_email(email)
    for subscriber in list_all_subscribers() or []:
        if subscriber.email == target:
            return subscriber
    return None


def set_status(email: str, status: str) -> bool:
    """구독 상태를 바꾼다. 대상이 없거나 저장에 실패하면 False."""
    if status not in (STATUS_ACTIVE, STATUS_UNSUBSCRIBED):
        raise ValueError(f"알 수 없는 상태: {status}")
    subscriber = find_subscriber(email)
    if subscriber is None:
        return False
    from app.db import update_subscriber

    fields: dict[str, Any] = {
        "status": status,
        "unsubscribed_at": (
            datetime.now(timezone.utc).isoformat() if status == STATUS_UNSUBSCRIBED else None
        ),
    }
    return update_subscriber(subscriber.id, fields)


def add_subscriber(
    email: str, name: str | None = None, is_owner: bool = False
) -> Subscriber | None:
    """구독자를 추가한다. 이미 있는 주소면 새로 만들지 않고 다시 active 로 돌린다.

    다시 켤 때 token 은 그대로 둔다. 예전에 받은 메일의 링크가 계속 같은 사람을 가리킨다.
    저장에 실패하면 None.
    """
    address = normalize_email(email)
    if "@" not in address:
        raise ValueError("이메일 주소 형식이 아니다")

    existing = find_subscriber(address)
    if existing is not None:
        if existing.status != STATUS_ACTIVE and not set_status(address, STATUS_ACTIVE):
            return None
        return existing.model_copy(update={"status": STATUS_ACTIVE})

    from app.db import insert_subscriber

    row = insert_subscriber(
        {
            "email": address,
            "name": name,
            "status": STATUS_ACTIVE,
            "token": secrets.token_urlsafe(24),
            "is_owner": is_owner,
        }
    )
    return _to_subscriber(row) if row else None


def purge_subscriber(email: str) -> Subscriber | None:
    """구독자 행을 지운다. 연결 기록(sent_items, feedback_events, click_events)은
    외래키 on delete cascade 로 함께 지워진다. 되돌릴 수 없다.

    remove(수신거부)와 다르다. remove 는 행을 남겨 예전 메일의 링크가 계속 같은 사람을
    가리키게 하고, purge 는 "내 기록을 지워 달라"는 요청에 쓴다. 지운 구독자를 돌려주고,
    대상이 없거나 삭제에 실패하면 None 이다.
    """
    subscriber = find_subscriber(email)
    if subscriber is None:
        return None
    from app.db import delete_subscriber

    return subscriber if delete_subscriber(subscriber.id) else None


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def _format_list(subscribers: list[Subscriber]) -> str:
    lines = [f"{'상태':<14}{'소유자':<6}{'token':<8}{'이름':<14}이메일", "-" * 70]
    for s in subscribers:
        lines.append(
            f"{s.status:<14}{'예' if s.is_owner else '':<6}{mask_token(s.token):<8}"
            f"{(s.name or ''):<14}{s.email}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 성공 0, 미설정·대상 없음·저장 실패 1."""
    parser = argparse.ArgumentParser(description="지인 초대제 구독자 명단 관리")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="전체 명단(token 은 앞 4자만)")
    add = sub.add_parser("add", help="구독자 추가(이미 있으면 다시 켠다)")
    add.add_argument("email")
    add.add_argument("--name", default=None)
    add.add_argument("--owner", action="store_true", help="운영자 본인 주소(user_id default)")
    remove = sub.add_parser("remove", help="수신거부 처리(행은 지우지 않는다)")
    remove.add_argument("email")
    purge = sub.add_parser(
        "purge", help="구독자와 연결 기록(발송 이력, 표시, 클릭)을 지운다. 되돌릴 수 없다"
    )
    purge.add_argument("email")
    purge.add_argument("--yes", action="store_true", help="확인 없이 지운다(이 플래그가 없으면 지우지 않는다)")
    args = parser.parse_args(argv)

    if not get_settings().subscribers_enabled:
        print("Supabase 가 설정되지 않았다(SUPABASE_URL, SUPABASE_SERVICE_KEY).", file=sys.stderr)
        return 1

    if args.command == "list":
        subscribers = list_all_subscribers()
        if subscribers is None:
            print("명단 조회에 실패했다. 키와 테이블(RLS)을 확인하라.", file=sys.stderr)
            return 1
        print(_format_list(subscribers))
        print(f"합계 {len(subscribers)}명")
        return 0

    if args.command == "add":
        try:
            subscriber = add_subscriber(args.email, name=args.name, is_owner=args.owner)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        if subscriber is None:
            print("추가에 실패했다. 키가 service role 인지, 테이블이 있는지 확인하라.", file=sys.stderr)
            return 1
        print(
            f"구독자 {subscriber.email} (token {mask_token(subscriber.token)}, "
            f"user_id {subscriber.user_id if subscriber.is_owner else subscriber.short_id}) 활성"
        )
        return 0

    if args.command == "purge":
        target = mask_email(normalize_email(args.email))
        if not args.yes:
            print(
                f"{target} 의 구독자 행과 연결 기록(sent_items, feedback_events, click_events)을 "
                "지운다. 되돌릴 수 없다. 실행하려면 --yes 를 붙여라. 아무것도 지우지 않았다.",
                file=sys.stderr,
            )
            return 1
        purged = purge_subscriber(args.email)
        if purged is None:
            print("대상이 없거나 삭제에 실패했다.", file=sys.stderr)
            return 1
        print(f"지웠다: {target} (구독자 {purged.short_id}, 연결 기록 포함)")
        return 0

    if set_status(args.email, STATUS_UNSUBSCRIBED):
        print(f"수신거부 처리했다: {normalize_email(args.email)}")
        return 0
    print("대상이 없거나 저장에 실패했다.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    # Windows 터미널이 cp949일 때 유니코드 출력 실패 방지(daily_digest 와 동일)
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
