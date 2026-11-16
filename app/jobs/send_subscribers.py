"""저장된 후보 풀로 구독자 발송 단계만 다시 돈다.

    python -m app.jobs.send_subscribers --date 2026-11-16 [--dry-run] [--only EMAIL]

크론이 풀 저장까지 끝내고 구독자 발송에서 실패한 날의 재발송용이다. data/pool/
pool_{date}.json 을 읽어 daily_digest 의 구독자 단계(app/subscriber_delivery.py)와
같은 선정, 렌더, 발송, sent_items 기록을 한다.

수집·분석은 하지 않고 LLM 도 부르지 않는다. 운영자 채널(Discord, Telegram, EMAIL_TO),
seen_urls, 정본, 아카이브는 건드리지 않는다.

--dry-run 은 메일을 보내지 않고 DB 에 쓰지도 않는다. 명단과 sent_items 는 읽고,
수신자별 HTML 을 data/dryrun/subscribers/{date}/ 아래에 쓴다(파일명은 구독자 id 앞 8자).

그 날짜 호를 이미 받은 구독자는 건너뛴다(sent_items 의 digest_date 로 판단). 그래서
일부만 실패한 날 그대로 다시 돌려도 받은 사람에게 두 번 가지 않는다.

is_owner 구독자는 그날 운영자에게 실제로 나간 목록(data/records/digest_{date}.json)을
받는다. 정본이 없으면 풀의 selected_default 표시로 대신한다.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import structlog

from app.config import get_settings
from app.contract import today_kst
from app.models import DigestItem, DigestRecord
from app.pool import ScoredCandidate, load_pool
from app.subscriber_delivery import DeliverySummary, deliver_to_subscribers
from app.subscribers import list_active_subscribers, normalize_email

logger = structlog.get_logger()

REPO_ROOT = Path(__file__).parent.parent.parent


def owner_items_for(
    date_iso: str, scored: list[ScoredCandidate], base_dir: Path | None = None
) -> list[DigestItem]:
    """운영자에게 그날 나간 목록. 정본을 우선하고, 없으면 풀의 selected_default 다."""
    root = base_dir if base_dir is not None else REPO_ROOT
    path = root / "data" / "records" / f"digest_{date_iso}.json"
    try:
        if path.exists():
            return DigestRecord.model_validate_json(path.read_text(encoding="utf-8")).items
    except Exception as e:
        logger.warning("owner_record_unreadable", date=date_iso, error=str(e)[:200])
    return [c.item for c in scored if c.selected_default]


def run(
    date_iso: str,
    dry_run: bool = False,
    only: str | None = None,
    base_dir: Path | None = None,
) -> DeliverySummary | None:
    """구독자 단계를 돈다. 풀 파일이 없으면 FileNotFoundError, 대상이 없으면 None."""
    scored = load_pool(date_iso, base_dir=base_dir)
    subscribers = list_active_subscribers()
    if only:
        target = normalize_email(only)
        subscribers = [s for s in subscribers if s.email == target]
    if not subscribers:
        return None

    root = base_dir if base_dir is not None else REPO_ROOT
    dry_run_dir = root / "data" / "dryrun" / "subscribers" / date_iso if dry_run else None
    return deliver_to_subscribers(
        subscribers,
        scored,
        date_iso,
        owner_items=owner_items_for(date_iso, scored, base_dir),
        dry_run_dir=dry_run_dir,
    )


def main(argv: list[str] | None = None, base_dir: Path | None = None) -> int:
    """CLI 진입점. 성공 0, 풀 없음·대상 없음·발송 실패 1."""
    parser = argparse.ArgumentParser(description="저장된 후보 풀로 구독자 발송만 다시 돈다")
    parser.add_argument("--date", default="", help="풀 날짜(YYYY-MM-DD). 비우면 KST 오늘")
    parser.add_argument(
        "--dry-run", action="store_true", help="발송·DB 기록 없이 수신자별 HTML 만 쓴다"
    )
    parser.add_argument("--only", default="", help="이 주소의 구독자 한 명만")
    args = parser.parse_args(argv)
    date_iso = args.date.strip() or today_kst()
    # 환경의 DRY_RUN=true 도 드라이런으로 본다. 실수로 실발송되는 쪽보다 안전하다.
    dry_run = args.dry_run or get_settings().dry_run

    try:
        summary = run(date_iso, dry_run=dry_run, only=args.only.strip() or None, base_dir=base_dir)
    except FileNotFoundError as e:
        print(f"{e}\n풀은 다이제스트 실행 때 data/pool/ 에 저장된다.", file=sys.stderr)
        return 1
    if summary is None:
        print(
            "발송 대상 구독자가 없다(명단이 비었거나, Supabase 미설정이거나, 조회 실패).",
            file=sys.stderr,
        )
        return 1

    counts = summary.as_dict()
    print(
        f"날짜 {date_iso} / 대상 {counts['total']}명 / 발송 {counts['sent']} / "
        f"실패 {counts['failed']} / 이미 받음 {counts['already_sent']} / "
        f"아이템 없음 {counts['no_items']} / 드라이런 {counts['dry_run']}"
    )
    for outcome in summary.outcomes:
        detail = f" {outcome.error}" if outcome.error else ""
        print(f"  {outcome.subscriber}  {outcome.status}  {outcome.items}건{detail}")
    return 1 if summary.failed else 0


if __name__ == "__main__":
    # Windows 터미널이 cp949일 때 유니코드 출력 실패 방지(daily_digest 와 동일)
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
