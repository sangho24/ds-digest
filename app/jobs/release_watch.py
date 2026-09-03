"""릴리스 감시 잡 - 수집 -> 전이 판정 -> 기록 -> 알림 -> 보드 발행.

`daily_digest` 와 별도 잡이다. 두 트랙을 한 프로세스에 묶으면 한쪽 실패가
다른 쪽을 끌어내리고, 개인 다이제스트의 저작권 있는 원문이 공개 표면으로
샐 경계가 흐려진다. 파일 경로부터 알림까지 전부 분리한다.

실행:  python -m app.jobs.release_watch
드라이런(DRY_RUN=true): 실제 상태를 기준으로 비교하되 기록·알림은 하지 않고
산출물을 data/dryrun/ 아래에 쓴다.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

from app.collectors_release import collect_releases
from app.config import get_settings
from app.contract import ARCHIVE_BASE
from app.deliverers.discord import send_discord_text
from app.releases import (
    EVENTS_PATH,
    STATES_PATH,
    append_events,
    build_tracker,
    detect_announcements,
    detect_transitions,
    format_alert,
    load_events,
    load_states,
    load_watchlist,
    save_states,
)

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent.parent
TRACKER_URL = f"{ARCHIVE_BASE}/tracker.html"


async def _notify(text: str) -> bool:
    """Discord 우선, 실패하면 Telegram. 드라이런은 보내지 않는다.

    `daily_digest._send_error_alert` 와 같은 모양이지만 그 모듈을 import 하면
    수집기·분석기까지 딸려 온다. 열다섯 줄 중복이 결합보다 싸다.
    """
    settings = get_settings()
    if settings.dry_run:
        logger.info("release_notify_skipped_dry_run", preview=text[:200])
        return False
    if await send_discord_text(text):
        return True
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("release_notify_undeliverable")
        return False
    try:
        from app.deliverers.telegram import _send_message
        async with httpx.AsyncClient(timeout=15) as client:
            # Discord 마크다운을 Telegram HTML 로 옮기지 않는다. 폴백은 도달이 목적이다.
            plain = text.replace("**", "").replace("__", "").replace("<", "").replace(">", "")
            return await _send_message(client, settings.telegram_bot_token,
                                       settings.telegram_chat_id, plain)
    except Exception as e:  # noqa: BLE001
        logger.warning("release_notify_failed", error=str(e))
        return False


async def run_release_watch() -> dict:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    logger.info("release_watch_started", time=now.isoformat(), dry_run=settings.dry_run)

    watchlist = load_watchlist()

    # 비교 기준은 언제나 실제 상태다. 드라이런이 빈 기준으로 돌면 매번
    # bootstrap 처럼 보여 "무엇이 새로운가"를 검증할 수 없다.
    states = load_states(STATES_PATH)
    prior_events = load_events(EVENTS_PATH)
    known_urls = {e.repo_id for e in prior_events if e.transition == "announced"}
    bootstrap = not states

    if settings.dry_run:
        out_root = ROOT / "data" / "dryrun"
        states_path = out_root / "data" / "release_states.json"
        events_path = out_root / "data" / "releases.jsonl"
        docs_dir = out_root / "docs"
        logger.info("release_watch_dry_run_redirected", path=str(out_root))
    else:
        states_path, events_path, docs_dir = STATES_PATH, EVENTS_PATH, ROOT / "docs"

    observations, posts, errors = await collect_releases(watchlist)
    logger.info("release_collected", hf=len(observations), blog=len(posts), errors=len(errors))

    if not observations and not posts:
        await _notify("⚠️ **릴리스 감시** 수집 결과가 없습니다. HF API 접근을 확인하세요.\n"
                      + "\n".join(errors[:5]))
        return {"status": "no_observations", "errors": errors}

    events, new_states = detect_transitions(observations, states, now=now)
    events += detect_announcements(posts, watchlist, known_urls, now=now)

    # 드라이런 산출물은 격리 경로에 쓴다. 이전 드라이런 잔여물이 append 되지
    # 않도록 이벤트 파일은 새로 쓴다.
    if settings.dry_run and events_path.exists():
        events_path.unlink()
    append_events(events, events_path)
    save_states(new_states, states_path)

    tracker = build_tracker(watchlist, new_states, prior_events + events, generated_at=now)
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "tracker.json").write_text(
        json.dumps(tracker, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    live = [e for e in events if not e.bootstrap]
    if bootstrap:
        text = (f"🔔 **릴리스 감시 시작** · {len(watchlist.orgs)}개 조직, "
                f"{len(new_states)}개 리포 적재. 이제부터 새 전이만 알립니다.\n"
                f"보드: <{TRACKER_URL}>")
    else:
        text = format_alert(events, watchlist, TRACKER_URL)

    sent = await _notify(text) if text else False
    if errors:
        await _notify("⚠️ **릴리스 감시** 일부 소스 실패 " + f"{len(errors)}건\n" + "\n".join(errors[:6]))

    result = {
        "status": "ok",
        "bootstrap": bootstrap,
        "observations": len(observations),
        "posts": len(posts),
        "events": len(events),
        "alerted": len(live),
        "sent": sent,
        "errors": len(errors),
        "tracked": len(new_states),
    }
    logger.info("release_watch_complete", **result)
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    asyncio.run(run_release_watch())
