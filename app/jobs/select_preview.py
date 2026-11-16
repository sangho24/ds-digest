"""저장된 후보 풀로 한 사용자의 선정 결과를 미리 본다.

    python -m app.jobs.select_preview --date 2026-11-16 [--user-id default]

data/pool/pool_{date}.json 을 읽어 select_for_user 에 넣고 결과를 표로 출력한다.
수집·분석·발송은 하지 않고 LLM 도 부르지 않는다. 파일을 쓰지도 않는다.

자연어 지시(directive)는 LLM 해석을 거쳐야 얻을 수 있어 여기서는 None 으로 둔다.
그래서 지시가 걸린 날의 실제 발송 선정과 순서가 다를 수 있다.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import structlog

from app.analyzer import SelectionResult, select_for_user
from app.config import get_settings
from app.contract import today_kst
from app.feedback import PROFILE_PATH
from app.models import UserProfile
from app.pool import ScoredCandidate, load_pool

logger = structlog.get_logger()

_TITLE_WIDTH = 60


def load_profile_readonly(user_id: str) -> tuple[UserProfile, str]:
    """프로필을 읽기만 한다. (프로필, 출처 설명)을 돌려준다.

    app.feedback.load_profile 은 프로필이 없으면 새로 만들어 저장한다. 미리보기가
    상태를 바꾸면 안 되므로 조회만 하고, 없으면 빈 프로필로 계산한다.
    """
    settings = get_settings()
    if settings.supabase_url and settings.supabase_key:
        from app.db import load_profile_from_db

        profile = load_profile_from_db(user_id)
        if profile is not None:
            return profile, "Supabase"
    try:
        if PROFILE_PATH.exists():
            local = UserProfile(**json.loads(PROFILE_PATH.read_text(encoding="utf-8")))
            if local.user_id == user_id:
                return local, f"로컬 {PROFILE_PATH.name}"
    except Exception as e:
        logger.warning("preview_profile_load_failed", user_id=user_id, error=str(e))
    return UserProfile(user_id=user_id), "없음(빈 프로필)"


def _status(candidate: ScoredCandidate, selected_ids: set[int], floor: int) -> str:
    if id(candidate.item) in selected_ids:
        return "선정"
    if candidate.absolute_score < floor:
        return "바닥미달"
    return "탈락"


def format_preview(
    scored: list[ScoredCandidate], result: SelectionResult, floor: int
) -> str:
    """후보 전체를 선정, 탈락, 바닥 미달 순으로 정렬한 표를 문자열로 만든다."""
    selected_ids = {id(d) for d in result.selected}
    order = {"선정": 0, "탈락": 1, "바닥미달": 2}

    rows: list[tuple[str, int, float, str]] = []
    for c in scored:
        adjustment = result.adjustments.get(id(c.item))
        rows.append(
            (
                _status(c, selected_ids, floor),
                c.item.analysis.relevance_score,
                adjustment[0] if adjustment else 0.0,
                c.item.raw.title,
            )
        )
    rows.sort(key=lambda r: (order[r[0]], -(r[1] + r[2])))

    lines = [f"{'결과':<6}{'점수':>4}  {'관심도':>6}  제목", "-" * 80]
    for status, score, adjust, title in rows:
        short = title if len(title) <= _TITLE_WIDTH else title[: _TITLE_WIDTH - 1] + "…"
        lines.append(f"{status:<6}{score:>4}  {adjust:>+6.2f}  {short}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, base_dir: Path | None = None) -> int:
    """CLI 진입점. 성공 0, 풀 파일 없음 1."""
    parser = argparse.ArgumentParser(description="저장된 후보 풀로 사용자별 선정 미리보기")
    parser.add_argument(
        "--date", default="", help="풀 날짜(YYYY-MM-DD). 비우면 KST 오늘"
    )
    parser.add_argument("--user-id", default="default", help="선정 기준 사용자")
    args = parser.parse_args(argv)
    date_iso = args.date.strip() or today_kst()

    try:
        scored = load_pool(date_iso, base_dir=base_dir)
    except FileNotFoundError as e:
        print(
            f"{e}\n"
            "풀은 다이제스트 실행 때 data/pool/ 에 저장된다. 날짜를 확인하거나, "
            "드라이런 산출물이면 data/dryrun/data/pool/ 을 보라.",
            file=sys.stderr,
        )
        return 1

    profile, profile_source = load_profile_readonly(args.user_id)
    result = select_for_user(scored, profile, directive=None, user_id=args.user_id)
    floor = get_settings().relevance_floor

    print(f"날짜 {date_iso} / 사용자 {args.user_id} / 프로필 {profile_source}")
    print("지시(directive)는 LLM 해석이 필요해 적용하지 않았다(None). 실제 발송과 다를 수 있다.")
    print(
        f"후보 {len(scored)}건, 바닥값({floor}) 이상 {result.above_floor}건, "
        f"선정 {len(result.selected)}건, 관심도 조정 {len(result.adjustments)}건"
    )
    print(format_preview(scored, result, floor))

    stored = {c.item.raw.url for c in scored if c.selected_default}
    now = {d.raw.url for d in result.selected}
    if stored == now:
        print("저장 당시 default 선정과 같다.")
    else:
        print(
            f"저장 당시 default 선정과 다르다: 빠짐 {len(stored - now)}건, "
            f"새로 들어옴 {len(now - stored)}건"
        )
    return 0


if __name__ == "__main__":
    # Windows 터미널이 cp949일 때 유니코드 출력 실패 방지(daily_digest 와 동일)
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
