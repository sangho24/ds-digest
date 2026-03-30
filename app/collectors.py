"""
콘텐츠 수집 모듈
YouTube 채널 최신 영상 + RSS 피드에서 새 아티클을 가져온다.
"""
import base64
import os
import tempfile
import httpx
import feedparser
import structlog
from datetime import datetime, timedelta
from youtube_transcript_api import YouTubeTranscriptApi

from app.models import RawContent, SourceType

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
                    transcript = _get_transcript(video_id)

                    items.append(RawContent(
                        source_type=SourceType.YOUTUBE,
                        source_name=channel_name,
                        title=entry.title,
                        url=f"https://youtu.be/{video_id}",
                        published_at=published,
                        transcript=transcript,
                    ))
                    logger.info("youtube_collected", title=entry.title, has_transcript=bool(transcript))

            except Exception as e:
                logger.error("youtube_fetch_failed", channel_id=channel_id, error=str(e))

    return items


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

    logger.info("collection_complete", youtube=len(yt_items), rss=len(rss_items), total=len(yt_items) + len(rss_items))
    return yt_items, rss_items
