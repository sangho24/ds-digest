"""저장된 레코드로 이메일만 재발송한다.

    python -m app.jobs.resend_email --date 2026-09-04 [--subject-prefix "[재발송 테스트] "]

data/records/digest_{date}.json(구조화 정본)을 읽어 DigestItem 목록을 만들고
send_digest 에 그 날짜를 넘겨 머리말·subject·아카이브 링크가 원래 호와 같게
나가도록 한다. 이메일 템플릿을 바꾼 뒤 실제 메일 클라이언트에서 다시 보려는
용도다.

수집·분석은 하지 않고, seen_urls 기록·docs 커밋·Discord/Telegram 발송도 절대
하지 않는다. 이메일 한 채널만 건드린다.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

import structlog

from app.contract import today_kst
from app.models import DigestItem, DigestRecord
from app.newsletter import send_digest

logger = structlog.get_logger()

# app/jobs/resend_email.py 기준 리포 루트(app/ 의 부모). records.py 와 같은 규칙.
REPO_ROOT = Path(__file__).parent.parent.parent
RECORDS_DIR = REPO_ROOT / "data" / "records"


def load_record_items(date_iso: str, records_dir: Path = RECORDS_DIR) -> list[DigestItem]:
    """digest_{date}.json 을 읽어 DigestItem 목록을 돌려준다. 없으면 FileNotFoundError."""
    path = records_dir / f"digest_{date_iso}.json"
    if not path.exists():
        raise FileNotFoundError(f"레코드가 없다: {path}")
    record = DigestRecord.model_validate_json(path.read_text(encoding="utf-8"))
    return record.items


async def resend_digest_email(date_iso: str, subject_prefix: str = "") -> bool:
    """해당 날짜 레코드로 이메일을 재발송한다. 한 명 이상 성공하면 True."""
    items = load_record_items(date_iso)
    logger.info("resend_email_start", date=date_iso, items=len(items), subject_prefix=subject_prefix)
    ok = await send_digest(items, date_iso=date_iso, subject_prefix=subject_prefix)
    if ok:
        logger.info("resend_email_done", date=date_iso, items=len(items))
    else:
        logger.error("resend_email_failed", date=date_iso, msg="이메일 발송 전원 실패 또는 스킵")
    return ok


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 성공 0, 레코드 없음·발송 실패 1."""
    parser = argparse.ArgumentParser(description="저장된 레코드로 다이제스트 이메일만 재발송")
    parser.add_argument(
        "--date",
        default="",
        help="재발송할 다이제스트 날짜(YYYY-MM-DD). 비우면 KST 오늘",
    )
    parser.add_argument(
        "--subject-prefix",
        default="",
        help='subject 맨 앞에 붙일 문자열(예: "[재발송] ")',
    )
    args = parser.parse_args(argv)
    date_iso = args.date.strip() or today_kst()

    try:
        ok = asyncio.run(resend_digest_email(date_iso, args.subject_prefix))
    except FileNotFoundError as e:
        logger.error("resend_email_record_missing", date=date_iso, error=str(e))
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    # Windows 터미널이 cp949일 때 유니코드 로그 출력 실패 방지(daily_digest 와 동일)
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
