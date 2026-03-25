"""
매일 아침 실행되는 다이제스트 잡
수집 → 중복 제거 → 필터링 → 분석 → 발송 파이프라인을 오케스트레이션한다.
"""
import asyncio
import httpx
import structlog
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.collectors import collect_all
from app.analyzer import filter_and_analyze
from app.newsletter import send_digest, render_digest_email
from app.feedback import load_profile
from app.deliverers.telegram import send_telegram_digest

logger = structlog.get_logger()


async def _process_pending_feedback() -> None:
    """
    파이프라인 시작 전 Telegram 미처리 콜백을 일괄 처리 (Method B).
    별도 서버 없이 하루 1회 피드백을 수집해 프로필에 반영.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return
    try:
        from app.deliverers.polling import poll_once
        async with httpx.AsyncClient() as client:
            await poll_once(client, settings.telegram_bot_token)
        logger.info("pending_feedback_processed")
    except Exception as e:
        logger.warning("pending_feedback_error", error=str(e))


async def run_daily_digest() -> dict:
    """메인 파이프라인 실행"""
    settings = get_settings()
    start = datetime.now()
    logger.info("digest_started", time=start.isoformat())

    # 0. 어제 들어온 Telegram 피드백 먼저 처리 → 프로필 반영
    await _process_pending_feedback()

    # 1. 사용자 프로필 로드
    profile = load_profile()
    logger.info("profile_loaded", topics=profile.preferred_topics, keywords=profile.keyword_requests)

    # 2. 콘텐츠 수집 (YouTube / RSS 분리)
    yt_items, rss_items = await collect_all(
        youtube_channels=settings.youtube_channel_list,
        rss_feeds=settings.rss_feed_list,
        hours=48,
        fetch_per_channel=settings.yt_fetch_per_channel,
    )

    if not yt_items and not rss_items:
        logger.warning("no_items_collected")
        return {"status": "no_items", "collected": 0}

    # 3. 중복 발송 방지
    # RSS: 시간 필터로 이미 걸러졌지만 dedup도 적용
    rss_items = _deduplicate(rss_items)
    # YouTube: dedup 후 채널당 new_per_channel개로 제한
    yt_items = _deduplicate(yt_items)
    yt_items = _cap_per_channel(yt_items, settings.yt_new_per_channel)

    raw_items = yt_items + rss_items
    logger.info("after_dedup", yt=len(yt_items), rss=len(rss_items), remaining=len(raw_items))

    if not raw_items:
        logger.info("all_items_already_seen")
        return {"status": "all_seen", "collected": 0}

    # 4. 필터링 + 분석
    digest_items = await filter_and_analyze(raw_items, profile)

    if not digest_items:
        logger.warning("no_items_passed_filter")
        return {"status": "all_filtered", "collected": len(raw_items), "passed": 0}

    # 5. 발송 — 설정된 채널 모두 시도 (하나 실패해도 다른 쪽 계속)
    channels = [c.strip() for c in settings.delivery_channels.split(",")]
    sent_results: dict[str, bool] = {}

    if "telegram" in channels:
        sent_results["telegram"] = await send_telegram_digest(digest_items)

    if "email" in channels:
        sent_results["email"] = await send_digest(digest_items)

    any_sent = any(sent_results.values())

    # 6. 발송 완료 URL 기록 (적어도 1개 채널 성공 시)
    if any_sent:
        _mark_sent(digest_items)

    # 7. HTML 로컬 저장 (디버깅 + 아카이브)
    output_dir = Path(__file__).parent.parent.parent / "data" / "archive"
    output_dir.mkdir(parents=True, exist_ok=True)
    html = render_digest_email(digest_items)
    today_str = datetime.now().strftime("%Y-%m-%d")
    (output_dir / f"digest_{today_str}.html").write_text(html, encoding="utf-8")

    elapsed = (datetime.now() - start).total_seconds()
    result = {
        "status": "ok",
        "collected": len(raw_items),
        "analyzed": len(digest_items),
        "delivery": sent_results,
        "elapsed_seconds": round(elapsed, 1),
    }
    logger.info("digest_complete", **result)
    return result


def _deduplicate(raw_items):
    """
    Supabase seen_urls를 이용해 이미 발송된 URL을 제거.
    Supabase 미설정 시 이번 실행 내 중복만 제거.
    """
    from app.config import get_settings
    settings = get_settings()

    use_db = bool(settings.supabase_url and settings.supabase_key)
    seen_in_run: set[str] = set()
    fresh = []

    for item in raw_items:
        if item.url in seen_in_run:
            continue
        if use_db:
            from app.db import is_seen
            if is_seen(item.url):
                logger.debug("skipping_seen_url", url=item.url[:80])
                continue
        seen_in_run.add(item.url)
        fresh.append(item)

    return fresh


def _cap_per_channel(items, max_per_channel: int):
    """채널(source_name)별로 최대 max_per_channel개만 남김."""
    from collections import defaultdict
    counts: dict[str, int] = defaultdict(int)
    result = []
    for item in items:
        key = item.source_name
        if counts[key] < max_per_channel:
            result.append(item)
            counts[key] += 1
    return result


def _mark_sent(digest_items) -> None:
    """발송 완료된 아이템의 URL을 Supabase seen_urls에 기록."""
    from app.config import get_settings
    settings = get_settings()

    if not (settings.supabase_url and settings.supabase_key):
        return

    from app.db import mark_seen
    for item in digest_items:
        mark_seen(item.raw.url)


# CLI로 직접 실행 가능
if __name__ == "__main__":
    # Windows 터미널이 cp949일 때 유니코드 로그 출력 실패 방지
    import sys, io
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    asyncio.run(run_daily_digest())
