"""
콘텐츠 수집 모듈
YouTube 채널 최신 영상 + RSS 피드에서 새 아티클을 가져온다.
"""
import base64
import os
import re
import tempfile
import httpx
import feedparser
import structlog
from datetime import datetime, timedelta
from youtube_transcript_api import YouTubeTranscriptApi

from app.config import get_settings
from app.models import RawContent, SourceType
from app.transcript_gemini import fetch_transcript_via_gemini

logger = structlog.get_logger()


# ──────────────────────────────────────────────
# YouTube
# ──────────────────────────────────────────────

async def fetch_youtube_recent(channel_ids: list[str], fetch_per_channel: int = 10) -> list[RawContent]:
    """채널별 최신 영상 수집 (RSS 피드 기반, API key 불필요)
    시간 필터 없음 — dedup으로 중복 제거, 채널당 최대 fetch_per_channel개 반환.
    """
    items: list[RawContent] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for channel_id in channel_ids:
            feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            try:
                resp = await client.get(feed_url)
                feed = feedparser.parse(resp.text)
                channel_name = feed.feed.get("title", channel_id)

                for entry in feed.entries[:fetch_per_channel]:
                    published = datetime(*entry.published_parsed[:6]) if entry.get("published_parsed") else None
                    video_id = entry.yt_videoid
                    body = _extract_yt_description(entry)

                    transcript, transcript_source = await _resolve_transcript(video_id, entry, body)

                    items.append(RawContent(
                        source_type=SourceType.YOUTUBE,
                        source_name=channel_name,
                        title=entry.title,
                        url=f"https://youtu.be/{video_id}",
                        published_at=published,
                        transcript=transcript,
                        body=body,
                    ))
                    logger.info(
                        "youtube_collected",
                        title=entry.title,
                        has_transcript=bool(transcript),
                        has_body=bool(body),
                        transcript_source=transcript_source,
                    )

            except Exception as e:
                logger.error("youtube_fetch_failed", channel_id=channel_id, error=str(e))

    return items


async def _resolve_transcript(video_id: str, entry, body: str | None) -> tuple[str | None, str]:
    """YouTube 자막 확보 폴백 체인.

    1) youtube-transcript-api — 주거 IP에서는 최선. 유일하게 [MM:SS] 타임스탬프가 나온다.
    2) Gemini에 영상 URL 직접 전달 — datacenter IP 차단을 우회하지만 타임스탬프가 없다.
    3) 영상 설명글(body) — 자막이 아니므로 transcript로 승격하지 않고 출처만 기록한다.

    반환: (transcript, transcript_source)
      transcript_source ∈ {"api", "gemini", "description", "none"}
      — 어느 경로로 근거를 확보했는지 평가 하니스가 계측할 수 있도록 구분한다.
    """
    transcript = _get_transcript(video_id)
    if transcript:
        return transcript, "api"

    settings = get_settings()

    # DRY_RUN이면 외부 API를 호출하지 않는다(파이프라인 흐름 검증 전용).
    # GEMINI_API_KEY가 없으면 2단계를 건너뛴다.
    if settings.yt_gemini_transcript and settings.gemini_api_key and not settings.dry_run:
        # YouTube RSS 피드(videos.xml)에는 duration이 포함되지 않는 것이 일반적이라
        # 대부분 None이 넘어간다. 그 경우 사전 차단이 동작하지 않으며, 2시간 이상
        # 영상은 Gemini 쪽에서 400(토큰 초과)으로 실패한 뒤 None이 반환된다.
        duration_seconds = _extract_yt_duration_seconds(entry)
        # Gemini에는 youtu.be 단축 URL 대신 공식 문서 예제와 같은 watch?v= 형식을 넘긴다.
        # (RawContent.url은 기존 dedup·seen_urls 호환을 위해 youtu.be를 그대로 유지한다.)
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        transcript = await fetch_transcript_via_gemini(watch_url, duration_seconds)
        if transcript:
            return transcript, "gemini"

    # 3단계: 기존 description fallback. body는 호출부에서 이미 추출해 RawContent.body로
    # 들어가므로 여기서는 출처 라벨만 정한다(analyzer가 DESCRIPTION 근거 수준으로 처리).
    if body:
        return None, "description"
    return None, "none"


def _extract_yt_duration_seconds(entry) -> int | None:
    """feedparser YouTube entry에서 영상 길이(초)를 추출한다.

    YouTube의 videos.xml 피드는 보통 duration을 제공하지 않는다. 다른 피드나
    feedparser 버전에서 media:content@duration이 실릴 수 있어 방어적으로 시도하고,
    없으면 None을 반환한다(= duration 사전 차단 비활성).
    """
    try:
        media_contents = entry.get("media_content") or []
        for media in media_contents:
            raw = media.get("duration")
            if raw:
                seconds = int(float(raw))
                if seconds > 0:
                    return seconds
    except Exception as e:
        logger.warning("yt_duration_parse_failed", error=str(e))
    return None


def _extract_yt_description(entry) -> str | None:
    """feedparser YouTube entry에서 description 텍스트 추출.
    HTML 태그 제거 후 최대 3000자로 truncate. 비어 있으면 None 반환.
    """
    text = ""

    # 1순위: entry.summary
    if hasattr(entry, "summary") and entry.summary:
        text = entry.summary
    else:
        # 2순위: media_group > media:description
        media_group = entry.get("media_group", {})
        if media_group:
            text = media_group.get("media_description", "") or ""

    if not text:
        return None

    # HTML 태그 제거
    text = re.sub(r'<[^>]+>', '', text)
    # 앞뒤 공백 제거
    text = text.strip()

    if not text:
        return None

    # 3000자 truncate
    return text[:3000]


def _build_transcript_api() -> YouTubeTranscriptApi:
    """
    YouTubeTranscriptApi 인스턴스 생성.
    YOUTUBE_COOKIES 환경변수가 있으면 쿠키를 주입한 requests.Session을 http_client로 전달.
    - GitHub Actions IP 차단 우회용: YouTube 계정 쿠키를 Netscape 형식으로
      base64 인코딩하여 YOUTUBE_COOKIES secret에 저장하면 됨.
    - 쿠키 없으면 익명 요청 (로컬 개발 환경에서는 보통 통과).

    쿠키 준비 방법:
      1. youtube.com 로그인된 Chrome에서 "Get cookies.txt LOCALLY" 확장 설치
      2. youtube.com 접속 후 쿠키 파일 내보내기 (Netscape 형식)
      3. base64 인코딩: python -c "import base64; print(base64.b64encode(open('cookies.txt','rb').read()).decode())"
      4. 출력값을 GitHub Secrets > YOUTUBE_COOKIES 에 저장
    """
    import requests
    from http.cookiejar import MozillaCookieJar

    cookies_b64 = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_b64:
        try:
            cookies_bytes = base64.b64decode(cookies_b64)
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".txt", delete=False
            ) as f:
                f.write(cookies_bytes)
                cookie_path = f.name

            jar = MozillaCookieJar(cookie_path)
            jar.load(ignore_discard=True, ignore_expires=True)

            session = requests.Session()
            session.cookies = jar
            logger.info("transcript_api_with_cookies", cookie_count=len(list(jar)))
            return YouTubeTranscriptApi(http_client=session)
        except Exception as e:
            logger.warning("transcript_cookie_load_failed", error=str(e))
    return YouTubeTranscriptApi()


def _get_transcript(video_id: str) -> str | None:
    """YouTube 자막 추출 (한국어 우선, 없으면 영어) — youtube-transcript-api v1.x"""
    try:
        api = _build_transcript_api()
        transcript_list = api.list(video_id)

        # 한국어 우선, 영어 fallback
        for lang in ["ko", "en"]:
            try:
                t = transcript_list.find_transcript([lang])
                fetched = t.fetch()
                return "\n".join(
                    f"[{_format_time(s.start)}] {s.text}" for s in fetched.snippets
                )
            except Exception:
                continue

        # 자동 생성 자막
        try:
            generated = transcript_list.find_generated_transcript(["ko", "en"])
            fetched = generated.fetch()
            return "\n".join(
                f"[{_format_time(s.start)}] {s.text}" for s in fetched.snippets
            )
        except Exception:
            pass

    except Exception as e:
        logger.warning("transcript_unavailable", video_id=video_id, error=str(e))
    return None


def _format_time(seconds: float) -> str:
    """초 → MM:SS 형식"""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


# ──────────────────────────────────────────────
# RSS / Blog
# ──────────────────────────────────────────────

async def fetch_rss_recent(feed_urls: list[str], hours: int = 48) -> list[RawContent]:
    """RSS 피드에서 최신 아티클 수집"""
    items: list[RawContent] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for url in feed_urls:
            try:
                resp = await client.get(url, follow_redirects=True)
                # resp.text 대신 bytes를 전달 — feedparser가 XML 선언 / Content-Type에서
                # 인코딩을 직접 감지하므로 httpx의 잘못된 charset 추론을 우회함
                feed = feedparser.parse(resp.content)
                feed_name = feed.feed.get("title", url)

                for entry in feed.entries[:5]:
                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6])
                        if datetime.now() - published > timedelta(hours=hours):
                            continue

                    # 본문 추출 (content > summary)
                    body = ""
                    if hasattr(entry, "content"):
                        body = entry.content[0].get("value", "")
                    elif hasattr(entry, "summary"):
                        body = entry.summary

                    link = getattr(entry, "link", None) or getattr(entry, "id", None)
                    if not link:
                        continue

                    items.append(RawContent(
                        source_type=SourceType.RSS,
                        source_name=feed_name,
                        title=entry.title,
                        url=link,
                        published_at=published,
                        body=body[:5000],  # 토큰 절약: 5000자 제한
                    ))
                    logger.info("rss_collected", title=entry.title)

            except Exception as e:
                logger.error("rss_fetch_failed", url=url, error=str(e))

    return items


# ──────────────────────────────────────────────
# ArXiv
# ──────────────────────────────────────────────

async def fetch_arxiv_recent(categories: list[str], hours: int = 48) -> list[RawContent]:
    """ArXiv Atom API에서 카테고리별 최신 논문 수집"""
    items: list[RawContent] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for category in categories:
            try:
                url = (
                    f"http://export.arxiv.org/api/query"
                    f"?search_query=cat:{category}"
                    f"&sortBy=submittedDate&sortOrder=descending&max_results=10"
                )
                resp = await client.get(url)
                feed = feedparser.parse(resp.content)

                for entry in feed.entries:
                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6])
                        if datetime.now() - published > timedelta(hours=hours):
                            continue

                    link = getattr(entry, "link", None) or getattr(entry, "id", None)
                    if not link:
                        continue

                    items.append(RawContent(
                        source_type=SourceType.RSS,
                        source_name=f"arXiv:{category}",
                        title=entry.title,
                        url=link,
                        published_at=published,
                        body=entry.summary[:3000] if hasattr(entry, "summary") else "",
                    ))
                    logger.info("arxiv_collected", category=category, title=entry.title)

            except Exception as e:
                logger.error("arxiv_fetch_failed", category=category, error=str(e))

    return items


# ──────────────────────────────────────────────
# HackerNews
# ──────────────────────────────────────────────

async def fetch_hackernews_recent(
    keywords: list[str], hours: int = 24, min_score: int = 50
) -> list[RawContent]:
    """Algolia HN API에서 키워드별 최신 스토리 수집"""
    import time as _time

    items: list[RawContent] = []
    seen_urls: set[str] = set()
    timestamp = int(_time.time()) - hours * 3600

    async with httpx.AsyncClient(timeout=15) as client:
        for keyword in keywords:
            try:
                url = (
                    f"https://hn.algolia.com/api/v1/search"
                    f"?query={keyword}&tags=story"
                    f"&numericFilters=created_at_i>{timestamp},points>{min_score}"
                )
                resp = await client.get(url)
                data = resp.json()

                for hit in data.get("hits", []):
                    item_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"

                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)

                    items.append(RawContent(
                        source_type=SourceType.RSS,
                        source_name="HackerNews",
                        title=hit["title"],
                        url=item_url,
                        published_at=None,
                        body=hit.get("story_text", "")[:3000],
                    ))
                    logger.info("hn_collected", keyword=keyword, title=hit["title"])

            except Exception as e:
                logger.error("hn_fetch_failed", keyword=keyword, error=str(e))

    return items


# ──────────────────────────────────────────────
# Unified collector
# ──────────────────────────────────────────────

async def collect_all(
    youtube_channels: list[str],
    rss_feeds: list[str],
    hours: int = 48,
    fetch_per_channel: int = 10,
) -> tuple[list[RawContent], list[RawContent]]:
    """모든 소스에서 콘텐츠 수집.
    반환: (yt_items, rss_items) — YouTube와 RSS를 분리해서 반환 (전략이 다름).
    """
    yt_items = await fetch_youtube_recent(youtube_channels, fetch_per_channel)
    rss_items = await fetch_rss_recent(rss_feeds, hours)

    logger.info("youtube_rss_complete", youtube=len(yt_items), rss=len(rss_items), total=len(yt_items) + len(rss_items))
    return yt_items, rss_items
