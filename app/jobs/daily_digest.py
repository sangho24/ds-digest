"""
매일 아침 실행되는 다이제스트 잡
수집 → 중복 제거 → 필터링 → 분석 → 발송 파이프라인을 오케스트레이션한다.
"""
import asyncio
import httpx
import structlog
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.contract import publish as publish_contract, today_kst
from app.collectors import collect_all
from app.analyzer import (
    filter_and_analyze,
    resolve_directives,
    resolve_youtube_transcripts,
)
from app.newsletter import send_digest, render_digest_email
from app.feedback import load_profile
from app.deliverers.telegram import send_telegram_digest

logger = structlog.get_logger()


async def _send_error_alert(message: str) -> None:
    """파이프라인 오류를 Telegram으로 알림. 실패해도 파이프라인에 영향 없음."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    if settings.dry_run:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": f"⚠️ <b>DS Digest 오류</b>\n{message}",
                    "parse_mode": "HTML",
                },
            )
        logger.info("error_alert_sent", message=message[:80])
    except Exception as e:
        logger.error("error_alert_failed", error=str(e))


async def _process_pending_feedback() -> dict:
    """
    파이프라인 시작 전 Telegram 미처리 콜백을 일괄 처리 (배치 모드).
    별도 서버 없이 하루 1회 피드백을 수집해 프로필에 반영.
    처리 결과 summary 반환: {"likes": N, "dislikes": N, "keywords": [...]}
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return {}
    try:
        from app.deliverers.polling import poll_once
        async with httpx.AsyncClient() as client:
            summary = await poll_once(client, settings.telegram_bot_token)
        logger.info("pending_feedback_processed", **summary)
        return summary
    except Exception as e:
        logger.warning("pending_feedback_error", error=str(e))
        return {}


async def _send_feedback_summary(summary: dict) -> None:
    """전날 피드백 처리 결과를 Telegram으로 알림."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    if settings.dry_run:
        return

    parts = []
    if summary.get("likes"):
        parts.append(f"👍 {summary['likes']}건")
    if summary.get("dislikes"):
        parts.append(f"👎 {summary['dislikes']}건")
    if summary.get("keywords"):
        parts.append(f"📝 키워드 등록: {', '.join(summary['keywords'])}")
    if summary.get("directives"):
        parts.append(f"🗒 지시 {len(summary['directives'])}건 접수")
    if answered := summary.get("quiz_answers"):
        # 채점은 여기서 처음 사용자에게 보인다. 배치 폴링이라 버튼을 누른 시점엔
        # 콜백이 이미 만료돼 토스트를 띄울 수 없기 때문이다.
        correct = summary.get("quiz_correct", 0)
        parts.append(f"🧠 퀴즈 {correct}/{answered} 정답")

    if not parts:
        return

    text = "📊 <b>어제 피드백 처리 완료</b>\n" + " · ".join(parts)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning("feedback_summary_failed", error=str(e))


async def _send_directive_status(directive) -> None:
    """현재 적용 중인 지시를 Telegram으로 알린다."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id or settings.dry_run:
        return

    text = (
        "🗒 <b>적용 중인 지시</b>\n"
        f"{directive.describe()}\n"
        "<i>취소하려면 반대로 말씀하시면 됩니다 (지시는 14일 후 자동 만료)</i>"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning("directive_status_failed", error=str(e))


async def run_daily_digest() -> dict:
    """메인 파이프라인 실행"""
    settings = get_settings()
    start = datetime.now()
    logger.info("digest_started", time=start.isoformat())

    # 0-a. 만료된 seen_urls 정리
    # 주의: 30일로 두면 31일째 되는 날 같은 콘텐츠가 재유입된다.
    # 실측(98일/247아이템) 결과 전체의 19.8%가 이 경로로 재발송되었고,
    # 재등장 간격 49건이 전부 30~37일이었다. 소스 풀이 작을수록 심해진다.
    if settings.supabase_url and settings.supabase_key:
        from app.db import cleanup_seen_urls
        cleanup_seen_urls(days=settings.seen_url_ttl_days)

    # 0-b. 어제 들어온 Telegram 피드백 먼저 처리 → 프로필 반영 → 요약 알림
    feedback_summary = await _process_pending_feedback()
    await _send_feedback_summary(feedback_summary)

    # 1. 사용자 프로필 로드
    profile = load_profile()
    logger.info("profile_loaded", topics=profile.preferred_topics, keywords=profile.keyword_requests)

    # 2. 콘텐츠 수집 (YouTube / RSS 분리)
    yt_items, rss_items = await collect_all(
        youtube_channels=settings.youtube_channel_list,
        rss_feeds=settings.rss_feed_list,
        hours=48,
        fetch_per_channel=settings.yt_fetch_per_channel,
        fetch_link_body=settings.fetch_link_body,
    )
    from app.collectors import fetch_arxiv_recent, fetch_hackernews_recent
    from app.collectors_newsletter import fetch_newsletters_recent, DEFAULT_NEWSLETTERS
    arxiv_items = await fetch_arxiv_recent(settings.arxiv_category_list)
    hn_items = await fetch_hackernews_recent(
        settings.hackernews_keyword_list,
        min_score=settings.hackernews_min_score,
        fetch_link_body=settings.fetch_link_body,
    )
    # 뉴스레터: 주간 발행이 많아 168시간(7일) lookback으로 수집한다.
    # (RSS/arXiv는 48h, HN은 기본 24h — 뉴스레터는 발행 주기가 길어 창을 넓힌다.)
    # 데일리 실행이 며칠 밀려도 그 주 발행호를 놓치지 않으며, 재수집돼도
    # URL dedup + seen_urls가 중복 발송을 막는다.
    # 실패는 수집기 내부의 per-site/per-issue try/except가 흡수해 빈 리스트를 돌려준다.
    newsletter_items = await fetch_newsletters_recent(
        settings.newsletter_list or DEFAULT_NEWSLETTERS, hours=168
    )
    logger.info(
        "collection_complete",
        youtube=len(yt_items),
        rss=len(rss_items),
        arxiv=len(arxiv_items),
        hn=len(hn_items),
        newsletter=len(newsletter_items),
    )

    if not yt_items and not rss_items and not arxiv_items and not hn_items and not newsletter_items:
        logger.warning("no_items_collected")
        await _send_error_alert("수집된 콘텐츠가 없습니다. YouTube / RSS 소스를 확인하세요.")
        return {"status": "no_items", "collected": 0}

    # 3. 중복 발송 방지
    # RSS: 시간 필터로 이미 걸러졌지만 dedup도 적용
    rss_items = _deduplicate(rss_items + arxiv_items + hn_items + newsletter_items)
    # YouTube: dedup 후 채널당 new_per_channel개로 제한
    yt_items = _deduplicate(yt_items)
    yt_items = _cap_per_channel(yt_items, settings.yt_new_per_channel)

    # 3.4. 자연어 지시 해석 — 런당 한 번만 하고 두 소비처(Stage 1 랭킹·최종 선정)에
    # 같은 값을 내려보낸다. 두 번 해석하면 LLM 호출도 두 배고, 두 결과가 갈리면
    # 같은 런 안에서 서로 다른 지시가 적용된다.
    # 아는 출처 목록을 넘겨야 drop_sources가 실재하는 값에만 걸리므로, dedup을
    # 마친 전체 후보를 넘긴다.
    directive = await resolve_directives(yt_items + rss_items)
    if not directive.is_empty():
        logger.info("directive_active", summary=directive.describe())
        # 적용 중인 지시를 매일 노출한다. 안 보이는 상태값이 조용히 큐레이션을
        # 끌고 가는 것이 이런 시스템에서 가장 위험하다 — 왜 arxiv가 안 오는지
        # 아무도 모르게 되면 지시가 아니라 버그처럼 보인다.
        await _send_directive_status(directive)

    # 3.5. Stage 1 메타 랭킹 → Stage 2 상위 N건만 Gemini 전사 (rate-limit 보호)
    yt_items = await resolve_youtube_transcripts(
        yt_items, profile, settings.yt_transcript_budget, directive
    )

    raw_items = yt_items + rss_items
    logger.info("after_dedup", yt=len(yt_items), rss=len(rss_items), remaining=len(raw_items))

    if not raw_items:
        logger.info("all_items_already_seen")
        return {"status": "all_seen", "collected": 0}

    # 4. 필터링 + 분석
    digest_items = await filter_and_analyze(raw_items, profile, directive)

    if not digest_items:
        # 상대 랭킹 전환 후 이 분기는 "모든 아이템이 바닥값 미만"인 드문 경우에만 걸린다.
        logger.warning("no_items_above_floor")
        await _send_error_alert(
            f"발송할 만한 아이템이 없습니다. "
            f"(수집 {len(raw_items)}건 전부 관련도 {settings.relevance_floor}점 미만)"
        )
        return {"status": "all_filtered", "collected": len(raw_items), "passed": 0}

    # 5. 발송 — 설정된 채널 모두 시도 (하나 실패해도 다른 쪽 계속)
    channels = [c.strip() for c in settings.delivery_channels.split(",")]
    sent_results: dict[str, bool] = {}

    if "telegram" in channels:
        sent_results["telegram"] = await send_telegram_digest(digest_items)

    if "email" in channels:
        sent_results["email"] = await send_digest(digest_items)

    any_sent = any(sent_results.values())

    # 발송 실패 채널 알림
    failed_channels = [ch for ch, ok in sent_results.items() if not ok]
    if failed_channels and any_sent:
        # 일부 실패 (다른 채널은 성공)
        await _send_error_alert(f"발송 실패 채널: {', '.join(failed_channels)}")
    elif not any_sent:
        # 전체 실패
        await _send_error_alert(
            f"모든 발송 채널 실패: {', '.join(sent_results.keys()) or '채널 미설정'}"
        )

    # 6. 발송 완료 URL 기록 (적어도 1개 채널 성공 시)
    if any_sent:
        _mark_sent(digest_items)

    # 7. HTML 저장 — data/archive (로컬 디버깅) + docs/ (GitHub Pages 공개)
    root = Path(__file__).parent.parent.parent
    html = render_digest_email(digest_items)
    # 날짜는 KST 고정 — 러너가 UTC라 `datetime.now()`를 쓰면 한국 시각 07:10
    # 발행분이 전날 날짜로 저장된다(아카이브가 실제로 하루씩 밀려 있었다).
    today_str = today_kst()

    archive_dir = root / "data" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"digest_{today_str}.html").write_text(html, encoding="utf-8")

    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / f"{today_str}.html").write_text(html, encoding="utf-8")
    _update_docs_index(docs_dir)

    # 7.1. 공개 JSON 계약 발행 — docs/latest.json · {date}.json · index.json
    # HTML은 사람이 읽는 표면이라 디자인이 바뀐다. 기계 소비자(Edith brief 등)가
    # HTML을 파싱하면 디자인 변경이 곧 파손이므로, 버전 박힌 JSON을 따로 낸다.
    publish_contract(digest_items, today_str, docs_dir)

    # 7.5. 구조화 정본 저장 — 로컬 JSON(정본) + Supabase 미러(best-effort)
    # 발송 성공 여부와 무관하게 분석 결과가 남도록 HTML 저장과 같은 지점에 둔다.
    # (기존엔 HTML만 저장되고 구조화 분석 결과는 발송 후 폐기됐다.)
    from app.records import save_digest_records
    from app.db import save_digest_records_to_db
    record_path = save_digest_records(digest_items, today_str)
    logger.info("records_saved", path=str(record_path), count=len(digest_items))
    await save_digest_records_to_db(digest_items, today_str)  # best-effort 미러

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
    - 한 번의 bulk 쿼리로 조회하여 N+1 문제 및 부분 실패 최소화.
    - Supabase 미설정 또는 조회 실패 시 이번 실행 내 중복만 제거.
    """
    from app.config import get_settings
    settings = get_settings()

    use_db = bool(settings.supabase_url and settings.supabase_key)

    # 이번 실행 내 URL 중복 먼저 제거
    seen_in_run: set[str] = set()
    candidates = []
    for item in raw_items:
        if item.url not in seen_in_run:
            seen_in_run.add(item.url)
            candidates.append(item)

    if not use_db:
        return candidates

    # 한 번의 bulk 쿼리로 이미 발송된 URL 조회
    from app.db import fetch_seen_urls
    already_seen = fetch_seen_urls([item.url for item in candidates])
    logger.info("dedup_bulk_check", total=len(candidates), already_seen=len(already_seen))

    fresh = []
    for item in candidates:
        from app.db import _normalize_url
        if _normalize_url(item.url) in already_seen:
            logger.debug("skipping_seen_url", url=item.url[:80])
        else:
            fresh.append(item)

    return fresh


def _cap_per_channel(items, max_per_channel: int):
    """채널(source_name)별로 최대 max_per_channel개만 남김."""
    from collections import defaultdict
    counts: dict[str, int] = defaultdict(int)
    result = []
    for item in items:
        key = item.source_key or item.source_name
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


def _update_docs_index(docs_dir: Path) -> None:
    """docs/index.html — 날짜 목록 페이지 생성 (GitHub Pages 진입점)"""
    files = sorted(docs_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].html"), reverse=True)
    dates = [f.stem for f in files]

    if not dates:
        return

    items_html = "\n".join(
        f'<li><a href="{d}.html">📄 {d}</a></li>'
        for d in dates
    )

    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DS Digest — 아카이브</title>
<style>
  body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f0;}}
  .wrap{{max-width:640px;margin:48px auto;padding:0 20px;}}
  h1{{font-size:22px;font-weight:700;margin-bottom:6px;}}
  .sub{{color:#888;font-size:13px;margin-bottom:28px;}}
  ul{{list-style:none;padding:0;margin:0;}}
  li{{margin-bottom:8px;}}
  a{{display:block;padding:14px 20px;background:#fff;border-radius:10px;
     text-decoration:none;color:#1a1a1a;font-size:15px;
     border:1px solid #e8e8e0;}}
  a:hover{{box-shadow:0 2px 8px rgba(0,0,0,.1);}}
</style>
</head>
<body>
<div class="wrap">
  <h1>📚 DS Digest 아카이브</h1>
  <p class="sub">총 {len(dates)}개 다이제스트</p>
  <ul>{items_html}</ul>
</div>
</body>
</html>"""

    (docs_dir / "index.html").write_text(html, encoding="utf-8")
    logger.info("docs_index_updated", count=len(dates))


# CLI로 직접 실행 가능
if __name__ == "__main__":
    # Windows 터미널이 cp949일 때 유니코드 로그 출력 실패 방지
    import sys, io
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    asyncio.run(run_daily_digest())
