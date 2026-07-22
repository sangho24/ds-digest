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
from bs4 import BeautifulSoup
from markdownify import markdownify
from datetime import datetime, timedelta
from youtube_transcript_api import YouTubeTranscriptApi

from app.models import RawContent, SourceType

logger = structlog.get_logger()


async def _fetch_article_body(client: httpx.AsyncClient, url: str) -> str:
    """링크 기사 본문을 fetch해 Markdown 텍스트로 반환한다.

    외부 링크 스토리(HN)나 요약만 제공하는 RSS 피드의 전문 확보에 쓴다.
    trafilatura는 실패 시 조용히 None을 반환해 데이터가 유실되므로 쓰지 않고,
    BeautifulSoup + markdownify로 직접 파싱한다.

    어떤 예외에도 raise하지 않는다 — 실패 시 ""를 반환해 파이프라인/테스트를
    절대 깨뜨리지 않는다. 상위 호출부는 빈 문자열을 "본문 없음"으로 다루면 된다.
    """
    if not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return ""
        content_type = resp.headers.get("content-type", "").lower()
        if "html" not in content_type:
            return ""

        # 응답 크기 상한 — HN 외부 링크는 임의 URL이라 대용량 응답(바이너리 등)이
        # 통째로 메모리에 올라올 수 있다. content-length 선검사 + 파싱 전 캡으로 방어.
        max_bytes = 5_000_000  # 5MB
        content_length = resp.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > max_bytes:
            logger.warning("article_too_large", url=url, content_length=content_length)
            return ""
        html_text = resp.text
        if len(html_text) > max_bytes:  # content-length 미제공 응답 대비
            html_text = html_text[:max_bytes]

        soup = BeautifulSoup(html_text, "html.parser")
        # 비본문 요소 제거(스크립트·스타일·내비게이션·푸터)
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        # 본문 영역 우선순위: <article> → <main> → <body> → 문서 전체
        container = soup.find("article") or soup.find("main") or soup.body or soup
        text = markdownify(str(container), heading_style="ATX", bullets="-", wrap=False)
        text = text.strip()
        return text[:5000]  # 토큰 절약: 5000자 캡
    except Exception as e:
        logger.warning("article_fetch_failed", url=url, error=str(e))
        return ""


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

                    # 전사는 수집 시점이 아니라 dedup·채널 캡 이후 상위 N건만 Gemini로
                    # 확보한다(analyzer.resolve_youtube_transcripts). 수집 단계에서 전량
                    # 전사하면 dedup 이전에 20여 건의 Gemini 호출이 터져 무료 티어 429
                    # 폭풍을 일으킨다. 여기서는 설명글(body)만 담고 transcript=None으로 둔다.
                    items.append(RawContent(
                        source_type=SourceType.YOUTUBE,
                        source_name=channel_name,
                        source_key=channel_id,      # 채널 표시명이 바뀌어도 안정적인 id
                        source_label=channel_name,
                        title=entry.title,
                        url=f"https://youtu.be/{video_id}",
                        published_at=published,
                        transcript=None,
                        body=body,
                    ))
                    logger.info(
                        "youtube_collected",
                        title=entry.title,
                        has_transcript=False,  # 전사는 이후 Stage 2에서 확보
                        has_body=bool(body),
                    )

            except Exception as e:
                logger.error("youtube_fetch_failed", channel_id=channel_id, error=str(e))

    return items


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

async def fetch_rss_recent(
    feed_urls: list[str], hours: int = 48, fetch_link_body: bool = True
) -> list[RawContent]:
    """RSS 피드에서 최신 아티클 수집.

    fetch_link_body=True면 피드 본문(content/summary)이 짧을 때 링크 원문을
    추가로 가져와 더 긴 쪽을 쓴다.
    """
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

                    # 피드가 요약만 제공(500자 미만)하면 링크 원문을 가져와 더 긴 쪽을 쓴다.
                    # 이미 충분한 본문이 있으면 불필요한 네트워크 요청을 하지 않는다.
                    if (
                        fetch_link_body
                        and len(body.strip()) < 500
                        and str(link).lower().startswith(("http://", "https://"))
                    ):
                        fetched = await _fetch_article_body(client, link)
                        if len(fetched) > len(body):
                            body = fetched

                    items.append(RawContent(
                        source_type=SourceType.RSS,
                        source_name=feed_name,
                        source_key=url,           # 피드 URL은 표시명보다 안정적
                        source_label=feed_name,
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
                        source_key=f"arxiv:{category}",
                        source_label=f"arXiv:{category}",
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
    keywords: list[str], hours: int = 24, min_score: int = 50, fetch_link_body: bool = True
) -> list[RawContent]:
    """Algolia HN API에서 키워드별 최신 스토리 수집.

    fetch_link_body=True면 story_text가 빈 외부 링크 스토리에 한해 원문 본문을
    가져온다(HN 스토리 대부분은 외부 링크라 story_text가 비어 있다).
    """
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

                    body = (hit.get("story_text") or "")[:3000]
                    # story_text가 빈 외부 링크 스토리만 원문 본문을 보강한다.
                    # (HN 자체 스레드로 폴백된 URL은 hit["url"]이 None이라 건너뛴다)
                    external_url = hit.get("url")
                    if (
                        fetch_link_body
                        and not body
                        and external_url
                        and str(external_url).lower().startswith(("http://", "https://"))
                    ):
                        body = await _fetch_article_body(client, external_url)

                    items.append(RawContent(
                        source_type=SourceType.RSS,
                        source_name="HackerNews",
                        source_key="hackernews",     # 도메인 분리 없이 단일 버킷 유지
                        source_label="HackerNews",
                        title=hit["title"],
                        url=item_url,
                        published_at=None,
                        body=body,
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
    fetch_link_body: bool = True,
) -> tuple[list[RawContent], list[RawContent]]:
    """모든 소스에서 콘텐츠 수집.
    반환: (yt_items, rss_items) — YouTube와 RSS를 분리해서 반환 (전략이 다름).
    """
    yt_items = await fetch_youtube_recent(youtube_channels, fetch_per_channel)
    rss_items = await fetch_rss_recent(rss_feeds, hours, fetch_link_body)

    logger.info("youtube_rss_complete", youtube=len(yt_items), rss=len(rss_items), total=len(yt_items) + len(rss_items))
    return yt_items, rss_items
