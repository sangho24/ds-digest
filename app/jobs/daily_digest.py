"""
매일 아침 실행되는 다이제스트 잡
수집 → 중복 제거 → 필터링 → 분석 → 발송 파이프라인을 오케스트레이션한다.
"""
import asyncio
import httpx
import structlog
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from typing import Any

from app.config import get_settings
from app.contract import publish as publish_contract, today_kst
from app.collectors import collect_all
from app.analyzer import (
    filter_and_analyze,
    resolve_directives,
    resolve_youtube_transcripts,
)
from app.newsletter import TEMPLATE_DIR, send_digest, render_digest_web, kst_date_label
from app.feedback import load_profile
from app.models import DigestItem
from app.subscribers import Subscriber, scrub_error
from app.deliverers.telegram import send_telegram_digest
from app.deliverers.discord import send_discord_digest, send_discord_text

logger = structlog.get_logger()


async def _send_error_alert(message: str) -> None:
    """파이프라인 오류 알림. 실패해도 파이프라인에 영향 없음.

    Discord가 안 되면 Telegram으로 넘어간다. 이게 없으면 **가장 중요한 알림이
    가장 조용히 사라진다** — "모든 발송 채널 실패"를 그 실패한 채널로 보내려
    하기 때문이다. Discord 시크릿이 비어 있으면 다이제스트도 경보도 둘 다
    아무 데도 안 가고, 로그에만 남는다.
    """
    text = f"⚠️ **DS Digest 오류**\n{message}"
    if await send_discord_text(text):
        logger.info("error_alert_sent", channel="discord", message=message[:80])
        return

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id or settings.dry_run:
        logger.warning("error_alert_undeliverable", message=message[:120])
        return
    try:
        from app.deliverers.telegram import _send_message
        async with httpx.AsyncClient(timeout=15) as client:
            ok = await _send_message(
                client, settings.telegram_bot_token, settings.telegram_chat_id,
                f"⚠️ DS Digest 오류\n{message}",
            )
        logger.info("error_alert_sent" if ok else "error_alert_undeliverable",
                    channel="telegram", message=message[:80])
    except Exception as e:
        logger.warning("error_alert_failed", error=str(e), message=message[:120])


async def _process_pending_feedback() -> dict:
    """파이프라인 시작 전 미처리 피드백을 일괄 수거한다(배치 모드).

    **피드백 경로는 Discord로 한정한다.** 두 채널에서 동시에 받으면 같은 아이템에
    대한 신호가 갈리고 어느 쪽이 정본인지 판정할 근거가 없다. Telegram은 발송만
    남겨둘 수 있고(설정으로), 수거는 하지 않는다.
    """
    settings = get_settings()
    if not settings.discord_bot_token or not settings.discord_channel_id:
        logger.info("feedback_polling_skipped", reason="Discord 미설정")
        return {}
    try:
        from app.deliverers.discord_polling import poll_once
        async with httpx.AsyncClient(timeout=20) as client:
            summary = await poll_once(client)
        logger.info("pending_feedback_processed", **summary)
        return summary
    except Exception as e:
        logger.warning("pending_feedback_error", error=str(e))
        return {}


async def _send_feedback_summary(summary: dict) -> None:
    """전날 피드백 처리 결과를 알린다(피드백 경로와 같은 채널로)."""
    settings = get_settings()
    if not settings.discord_bot_token or not settings.discord_channel_id:
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
    if received := summary.get("directives"):
        # 무엇을 지시로 받아들였는지 원문 그대로 되돌려준다. 배치 폴링이라
        # 보낸 즉시 확인해줄 수 없으므로, 여기가 유일한 접수 확인 지점이다.
        quoted = " / ".join(f"\u201c{t}\u201d" for t in received[:3])
        parts.append(f"🗒 지시 {len(received)}건 접수 — {quoted}")
    if answered := summary.get("quiz_answers"):
        # 채점은 여기서 처음 사용자에게 보인다. 배치 폴링이라 버튼을 누른 시점엔
        # 콜백이 이미 만료돼 토스트를 띄울 수 없기 때문이다.
        correct = summary.get("quiz_correct", 0)
        parts.append(f"🧠 퀴즈 {correct}/{answered} 정답")

    if not parts:
        return

    await send_discord_text("📊 **어제 피드백 처리 완료**\n" + " · ".join(parts))

    # 문항별 채점을 되돌려준다. Poll은 정답을 보여주지 않으므로 여기가 사용자가
    # 자기 답을 확인하는 지점이다(사용자 요청 2026-09-03).
    if details := summary.get("quiz_details"):
        from app.quiz_results import format_quiz_recap
        for message in format_quiz_recap(details):
            await send_discord_text(message)


HELP_TEXT = """\
🧭 **DS Digest 사용법**

**큐레이션 바꾸기 — 그냥 한국말로 보내세요**
이 채널에 아무 말이나 보내면 지시로 접수되고 다음 날 아침 발송부터 반영됩니다.
· "논문보다 실무 사례 위주로"
· "쿠버네티스 얘기는 줄여줘"
· "arxiv 그만"
· "인과추론 더 보고 싶어"
되돌리려면 반대로 말하면 됩니다. 지시는 14일 후 자동 만료되고,
적용 중인 목록은 매일 아침 따로 알려드립니다.

**아이템 피드백**
각 아이템에 달린 👍 / 👎 를 누르세요 — 다음 큐레이션의 동점 순서를 가릅니다.

**퀴즈**
투표로 답을 고르면 기록됩니다. 반복해서 틀린 개념은 관련 콘텐츠가 우선 서빙됩니다.

**명령어**
`/keyword <주제>` — 특정 키워드를 분석 프롬프트에 직접 넣습니다
`/help` — 이 안내

-# 반영은 하루 1회 배치로 처리됩니다. 지금 보낸 것은 내일 아침 발송에 반영됩니다.
"""


async def _send_help() -> None:
    """사용법 안내. 자유 텍스트 지시 경로가 안내되지 않아 사실상 없는 기능이었다."""
    settings = get_settings()
    if not settings.discord_bot_token or not settings.discord_channel_id or settings.dry_run:
        return
    await send_discord_text(HELP_TEXT)


async def _send_directive_status(directive, note_in_mail: bool = False) -> None:
    """현재 적용 중인 지시를 Telegram으로 알린다.

    note_in_mail 은 글쓰기 지시가 이번 실행의 운영자 메일 문체로 적용되는가다(describe 참조).
    """
    settings = get_settings()
    if not settings.discord_bot_token or not settings.discord_channel_id or settings.dry_run:
        return

    text = (
        "🗒 <b>적용 중인 지시</b>\n"
        f"{directive.describe(note_in_mail)}\n"
        "<i>취소하려면 반대로 말씀하시면 됩니다 (지시는 14일 후 자동 만료)</i>"
    )
    await send_discord_text(text)


def _expected_source_keys(settings) -> list[str]:
    """설정상 존재해야 하는 소스 키. 수집기가 실제로 만드는 키와 형식을 맞춘다.

    형식이 어긋나면 같은 소스가 두 줄로 갈려 퍼널이 거짓말을 한다.
    (예: 수집기는 `arxiv:cs.LG`를 쓰는데 여기서 `cs.LG`를 넘기면 안 된다)
    """
    from urllib.parse import urlparse

    from app.collectors_newsletter import DEFAULT_NEWSLETTERS

    keys = [f"arxiv:{c}" for c in settings.arxiv_category_list]
    keys.append("hackernews")
    keys.extend(settings.youtube_channel_list)
    keys.extend(settings.rss_feed_list)
    for url in settings.newsletter_list or DEFAULT_NEWSLETTERS:
        host = urlparse(url if "://" in url else f"https://{url}").netloc
        if host:
            keys.append(host)
    return keys


def digest_items_raw(digest_items: list) -> list:
    """DigestItem 목록에서 RawContent만 꺼낸다(퍼널 기록은 소스 식별자만 본다)."""
    return [d.raw for d in digest_items]


def _delivery_channels(settings) -> list[str]:
    """DELIVERY_CHANNELS 를 소문자 목록으로. 발송 단계와 지시 표시가 같은 값을 본다."""
    return [c.strip().lower() for c in settings.delivery_channels.split(",") if c.strip()]


def _load_subscribers() -> list[Subscriber]:
    """활성 구독자 명단. 어떤 실패도 빈 목록으로 삼킨다(운영자 흐름을 막지 않는다).

    드라이런은 명단을 읽지 않는다. 빈 목록이면 뒤의 구독자 단계가 전부 꺼져 동작이
    구독자 기능이 없던 때와 완전히 같다(Supabase 미설정, 테이블 없음, 0명 모두 이 경우다).
    """
    try:
        settings = get_settings()
        if settings.dry_run or not settings.subscribers_enabled:
            return []
        from app.subscribers import list_active_subscribers
        return list_active_subscribers()
    except Exception as e:
        logger.warning("subscribers_load_failed", error=scrub_error(e))
        return []


async def _deliver_to_subscribers(
    subscribers: list[Subscriber],
    pool_entries: list[dict[str, Any]],
    digest_items: list[DigestItem],
    date_str: str,
    owner_delivered: set[str],
    owner_profile: Any = None,
    owner_directive: Any = None,
    operator_mail: Any = None,
) -> dict[str, int] | None:
    """운영자 흐름 뒤에 덧붙는 구독자 발송. 어떤 실패도 경고 로그로만 남긴다.

    여기서 예외가 올라가면 워크플로가 실패로 끝나 그날 docs 커밋까지 막힌다. 구독자
    쪽 문제로 운영자 산출물이 사라지면 안 되므로 통째로 감싼다.
    """
    try:
        from app.pool import candidates_from_entries
        from app.subscriber_delivery import OwnerContext, deliver_to_subscribers_async

        # 운영자 선정에 쓴 프로필과 지시. is_owner 의 비교군 표시(기준 선정 계산)와 문체
        # 요청(personal_note)에 쓴다. 운영자 메일 단계가 이미 다듬은 목록이 있으면 같이
        # 넘겨, is_owner 구독자가 같은 요청문으로 받을 때 LLM 을 다시 부르지 않게 한다.
        owner_context = (
            OwnerContext(
                profile=owner_profile,
                directive=owner_directive,
                restyled_items=getattr(operator_mail, "restyled", None),
                restyled_note=getattr(operator_mail, "note", ""),
            )
            if owner_profile is not None
            else None
        )
        summary = await deliver_to_subscribers_async(
            subscribers,
            candidates_from_entries(pool_entries),
            date_str,
            owner_items=digest_items,
            already_delivered=owner_delivered,
            owner_context=owner_context,
        )
        counts = summary.as_dict()
        if summary.failed:
            # 주소는 싣지 않는다. 운영 알림 채널에는 구독자 정보가 가지 않는다.
            await _send_error_alert(
                f"구독자 발송 실패 {summary.failed}/{counts['total']}명. 재발송: "
                f"python -m app.jobs.send_subscribers --date {date_str}"
            )
        return counts
    except Exception as e:
        logger.warning("subscriber_delivery_failed", error=scrub_error(e))
        return None


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
    if feedback_summary.get("help_requested"):
        await _send_help()

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
    arxiv_items = await fetch_arxiv_recent(
        settings.arxiv_category_list,
        max_items=settings.arxiv_max_items,
        request_delay=settings.arxiv_request_delay,
    )
    hn_items = await fetch_hackernews_recent(
        settings.hackernews_keyword_list,
        min_score=settings.hackernews_min_score,
        fetch_link_body=settings.fetch_link_body,
        max_items=settings.hackernews_max_items,
    )
    # 뉴스레터: 주간 발행이 많아 168시간(7일) lookback으로 수집한다.
    # (RSS는 48h, arXiv는 발표 일정 때문에 120h, HN은 기본 24h: 뉴스레터는 발행 주기가 길어 창을 넓힌다.)
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

    # 퍼널 기록용 원본 스냅샷. 아래 dedup이 리스트를 갈아치우므로 여기서 잡는다.
    collected_snapshot = yt_items + rss_items + arxiv_items + hn_items + newsletter_items

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
    # 글쓰기 지시(personal_note)가 오늘 운영자 메일의 문체로 적용되는지(표시용, §59).
    # 이메일 채널로 받거나 is_owner 구독자로 받을 때다. 뒤쪽을 알려면 명단이 필요해서,
    # 지시가 있고 이메일 채널이 꺼진 날에만 명단을 여기서 먼저 읽는다(아래 5단계가 재사용).
    # 글쓰기 지시가 없는 날은 명단을 읽는 시점과 횟수가 예전과 같다.
    early_subscribers: list[Subscriber] | None = None
    note_in_mail = False
    if getattr(directive, "personal_note", ""):
        from app.restyle import restyle_active
        if restyle_active():
            if "email" in _delivery_channels(settings) and settings.email_to.strip():
                note_in_mail = True
            else:
                early_subscribers = _load_subscribers()
                note_in_mail = any(s.is_owner for s in early_subscribers)
    if not directive.is_empty():
        logger.info("directive_active", summary=directive.describe(note_in_mail))
        # 적용 중인 지시를 매일 노출한다. 안 보이는 상태값이 조용히 큐레이션을
        # 끌고 가는 것이 이런 시스템에서 가장 위험하다 — 왜 arxiv가 안 오는지
        # 아무도 모르게 되면 지시가 아니라 버그처럼 보인다.
        await _send_directive_status(directive, note_in_mail)

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
    # pool_entries 에는 분석된 후보 전체(탈락 포함)의 스냅샷이 담긴다(7.6에서 저장).
    pool_entries: list[dict[str, Any]] = []
    digest_items = await filter_and_analyze(
        raw_items, profile, directive, pool_out=pool_entries
    )

    if not digest_items:
        # 상대 랭킹 전환 후 이 분기는 "모든 아이템이 바닥값 미만"인 드문 경우에만 걸린다.
        logger.warning("no_items_above_floor")
        await _send_error_alert(
            f"발송할 만한 아이템이 없습니다. "
            f"(수집 {len(raw_items)}건 전부 관련도 {settings.relevance_floor}점 미만)"
        )
        return {"status": "all_filtered", "collected": len(raw_items), "passed": 0}

    # 5. 발송 — 설정된 채널 모두 시도 (하나 실패해도 다른 쪽 계속)
    #
    # 소문자로 맞춘다. 예전엔 대소문자를 그대로 비교해서 DELIVERY_CHANNELS에
    # "Discord"가 들어 있으면 **어느 채널도 매치되지 않고 조용히 아무 데도
    # 안 나갔다**. 실측: 2026-09-01 실행이 수집·분석·선정을 다 끝내고
    # delivery={} 로 끝났다. 사람이 값을 넣는 자리라 대문자는 언제든 들어온다.
    KNOWN_CHANNELS = {"telegram", "discord", "email"}
    channels = _delivery_channels(settings)
    unknown = [c for c in channels if c not in KNOWN_CHANNELS]
    if unknown or not channels:
        # 값 자체는 시크릿이라 로그에서 마스킹된다. 그래서 "몇 개가 인식됐고
        # 몇 개가 아닌지"를 남겨야 진단이 된다.
        logger.error(
            "delivery_channels_unrecognized",
            recognized=[c for c in channels if c in KNOWN_CHANNELS],
            unrecognized_count=len(unknown),
            hint="DELIVERY_CHANNELS는 discord/telegram/email 중에서 쉼표로 구분",
        )
    sent_results: dict[str, bool] = {}

    if "telegram" in channels:
        sent_results["telegram"] = await send_telegram_digest(digest_items)

    if "discord" in channels:
        sent_results["discord"] = await send_discord_digest(digest_items)

    # 구독자 명단은 발송 전에 한 번 읽는다. is_owner 구독자의 주소가 EMAIL_TO 에 있으면
    # 그 한 통을 링크 든 구독자용 메일로 바꿔 보내야 같은 사람이 두 통을 받지 않는다.
    # 구독자가 없으면(미설정·0명·조회 실패) owners 가 비어 아래는 예전 호출 그대로다.
    subscribers = early_subscribers if early_subscribers is not None else _load_subscribers()
    owners = {s.email: s.token for s in subscribers if s.is_owner}
    owner_delivered: set[str] = set()

    # 운영자 메일 본문의 문체(§59). 글쓰기 지시(personal_note)나 is_owner 의 style_note 가
    # 있을 때만 LLM 을 1회 부르고, 없으면 digest_items 를 그대로 돌려준다(호출 없음).
    # 다듬은 목록은 메일 본문에만 쓴다. 위의 Discord, Telegram 과 아래의 아카이브, 정본,
    # 풀은 전부 digest_items(공용 요약)를 쓴다.
    operator_mail = None
    if "email" in channels:
        from app.subscriber_delivery import prepare_operator_mail
        operator_mail = await prepare_operator_mail(
            digest_items, directive, subscribers, settings.email_to
        )
        if owners:
            # owner_items 는 있을 때만 넘긴다(없는 날의 호출 모양은 예전 그대로다).
            extra = {"owner_items": operator_mail.owner_items} if operator_mail.owner_items else {}
            sent_results["email"] = await send_digest(
                operator_mail.items, owners=owners, owner_delivered=owner_delivered, **extra
            )
        else:
            sent_results["email"] = await send_digest(operator_mail.items)

    any_sent = any(sent_results.values())

    # 발송 실패 채널 알림
    failed_channels = [ch for ch, ok in sent_results.items() if not ok]
    if failed_channels and any_sent:
        # 일부 실패 (다른 채널은 성공)
        await _send_error_alert(f"발송 실패 채널: {', '.join(failed_channels)}")
    elif not any_sent:
        # 전체 실패
        await _send_error_alert(
            f"모든 발송 채널 실패: {', '.join(sent_results.keys())}"
            if sent_results else
            "발송 채널이 하나도 인식되지 않았습니다. "
            "DELIVERY_CHANNELS 값을 확인하세요 (discord/telegram/email)."
        )

    # 6. 발송 완료 URL 기록 (적어도 1개 채널 성공 시)
    if any_sent:
        _mark_sent(digest_items)

    # 7. HTML 저장 — data/archive (로컬 디버깅) + docs/ (GitHub Pages 공개)
    # 이메일(send_digest)은 테이블 레이아웃의 digest_email.html 을 쓰고, 여기
    # 아카이브는 웹용 digest.html 을 쓴다. 같은 HTML 을 둘 다에 쓰면 메일에서 깨진다.
    root = Path(__file__).parent.parent.parent
    # 날짜는 KST 고정 — 러너가 UTC라 `datetime.now()`를 쓰면 한국 시각 07:10
    # 발행분이 전날 날짜로 저장된다(아카이브가 실제로 하루씩 밀려 있었다).
    # 파일명뿐 아니라 머리말 날짜도 같은 값을 넘긴다.
    today_str = today_kst()
    html = render_digest_web(digest_items, date_str=kst_date_label(today_str))

    # 드라이런은 실제 산출물 경로를 건드리지 않는다.
    #
    # 실제로 사고가 났다: 드라이런이 `data/records/digest_{오늘}.json`과
    # `docs/{오늘}.html`을 mock 데이터로 만들었는데, 그날 산출물이 아직
    # 커밋되기 전이라 두 파일이 **untracked**였다. `git checkout -- data/ docs/`는
    # tracked 파일만 되돌리므로 mock이 살아남았고, 이어진 `git add -A`가 그걸
    # 그대로 커밋해 GitHub Pages에 "[DRY RUN]" 다이제스트가 공개됐다.
    #
    # 되돌리는 절차를 조심하는 것으로는 부족하다 — 애초에 안 쓰면 된다.
    dryrun_root = root / "data" / "dryrun"
    if settings.dry_run:
        archive_dir = dryrun_root / "archive"
        docs_dir = dryrun_root / "docs"
        logger.info("dry_run_output_redirected", path=str(dryrun_root))
    else:
        archive_dir = root / "data" / "archive"
        docs_dir = root / "docs"

    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"digest_{today_str}.html").write_text(html, encoding="utf-8")

    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / f"{today_str}.html").write_text(html, encoding="utf-8")
    _update_docs_index(docs_dir)

    # 7.1. 공개 JSON 계약 발행 — docs/latest.json · {date}.json · index.json
    # HTML은 사람이 읽는 표면이라 디자인이 바뀐다. 기계 소비자(Edith brief 등)가
    # HTML을 파싱하면 디자인 변경이 곧 파손이므로, 버전 박힌 JSON을 따로 낸다.
    publish_contract(digest_items, today_str, docs_dir)

    # 7.2. 구독자 메일의 피드백 페이지(docs/feedback.html)가 읽는 Edge Function 주소.
    # SUPABASE_URL 이 있을 때만 쓰고, 값은 주소뿐이다(키 금지). 실패해도 계속 간다.
    try:
        from app.subscriber_delivery import write_feedback_config
        write_feedback_config(docs_dir)
    except Exception as e:
        logger.warning("feedback_config_write_failed", error=str(e)[:200])

    # 7.5. 구조화 정본 저장 — 로컬 JSON(정본) + Supabase 미러(best-effort)
    # 발송 성공 여부와 무관하게 분석 결과가 남도록 HTML 저장과 같은 지점에 둔다.
    # (기존엔 HTML만 저장되고 구조화 분석 결과는 발송 후 폐기됐다.)
    # 7.4. 소스별 수집→후보→발송 퍼널 기록.
    # 이게 없으면 "수집은 되는데 한 번도 발송 안 되는 소스"가 지표에 안 보인다
    # (실측: arXiv가 40일간 발송 0건인데 source_reach는 그 존재조차 몰랐다).
    # 드라이런은 정본 파일에 쓰지 않는다 — mock 결과가 도달률 통계를 오염시킨다.
    # 그렇다고 건너뛰면 **발송 전에 퍼널을 검증할 방법이 없어진다**(수집 단계는
    # 드라이런에서도 실제 네트워크를 타므로 여기서 볼 수 있는 게 가장 많다).
    # 그래서 격리된 dryrun 경로에 남긴다.
    from app.source_stats import record as record_source_funnel, STATS_PATH
    record_source_funnel(
        collected_snapshot, raw_items, digest_items_raw(digest_items),
        date=today_str,
        # 설정상 있어야 할 소스를 함께 넘긴다. 이게 없으면 수집이 0건인 소스가
        # 퍼널에 아예 안 나타나 또 투명인간이 된다 — arXiv가 40일간 그랬다.
        expected=_expected_source_keys(settings),
        path=(dryrun_root / "data" / "source_stats.jsonl") if settings.dry_run else STATS_PATH,
    )

    from app.records import save_digest_records
    from app.db import save_digest_records_to_db
    # base_dir를 넘기면 그 아래 data/records/ 에 쓴다(기존 테스트 override 경로).
    record_path = save_digest_records(
        digest_items, today_str,
        base_dir=dryrun_root if settings.dry_run else None,
    )
    logger.info("records_saved", path=str(record_path), count=len(digest_items))
    await save_digest_records_to_db(digest_items, today_str)  # best-effort 미러

    # 7.6. 후보 풀 저장 — 탈락한 후보의 분석 결과까지 남겨 사용자별 재선정에 쓴다.
    # 정본이 아니므로 실패해도 경고만 남기고 계속 간다(save_pool 이 삼킨다).
    # 드라이런은 정본과 같은 규칙으로 data/dryrun/ 아래에 격리한다.
    from app.pool import save_pool
    pool_file = save_pool(
        pool_entries, today_str,
        base_dir=dryrun_root if settings.dry_run else None,
    )
    if pool_file is not None:
        logger.info("pool_saved", path=str(pool_file), count=len(pool_entries))

    # 8. 구독자별 선정과 발송. 운영자 흐름(발송, 정본, 풀, 아카이브)이 전부 끝난 뒤다.
    # 구독자가 없으면 아무것도 하지 않는다. 실패는 _deliver_to_subscribers 가 삼킨다.
    subscriber_counts: dict[str, int] | None = None
    if subscribers:
        subscriber_counts = await _deliver_to_subscribers(
            subscribers, pool_entries, digest_items, today_str, owner_delivered,
            owner_profile=profile, owner_directive=directive,
            operator_mail=operator_mail,
        )

    elapsed = (datetime.now() - start).total_seconds()
    result = {
        "status": "ok",
        "collected": len(raw_items),
        "analyzed": len(digest_items),
        "delivery": sent_results,
        "elapsed_seconds": round(elapsed, 1),
    }
    if subscriber_counts is not None:
        result["subscribers"] = subscriber_counts
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
    """docs/index.html — 날짜 목록 페이지 생성 (GitHub Pages 진입점).

    지면(digest.html)과 같은 토큰·활자를 쓰는 별도 템플릿을 쓴다. 예전에는 CSS를
    f-string 안에 넣었는데, 그러면 중괄호를 전부 이스케이프해야 해서 손대기가
    번거롭고 두 화면의 디자인이 조용히 갈린다.
    """
    files = sorted(docs_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].html"), reverse=True)
    dates = [f.stem for f in files]

    if not dates:
        return

    # 요일은 목록을 훑을 때 리듬을 준다 — 결번이 눈에 띈다.
    names = ["월", "화", "수", "목", "금", "토", "일"]
    weekdays = {}
    for value in dates:
        try:
            weekdays[value] = names[date.fromisoformat(value).weekday()]
        except ValueError:
            weekdays[value] = ""

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    html = env.get_template("archive_index.html").render(dates=dates, weekdays=weekdays)

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
