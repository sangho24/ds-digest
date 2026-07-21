"""Stibee 뉴스레터 발행호 수집기.

Stibee는 RSS 대신 ``sitemap.xml``에 발행호 URL과 발행일을 제공한다.
각 발행호의 실제 이메일 문서에서 본문 블록만 골라 Markdown 텍스트로 변환한다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import unquote, unquote_plus, urljoin, urlparse, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup, Tag
import httpx
from markdownify import markdownify
import structlog

from app.models import RawContent, SourceType


logger = structlog.get_logger()


DEFAULT_NEWSLETTERS: list[str] = [
    "https://dflick.stibee.com/",
    "https://deepdaiv.stibee.com/",
    "https://hai-hancom.stibee.com/",
    "https://innoforest.stibee.com/",
    "https://trendlite.stibee.com/",
]

NEWSLETTER_NAMES: dict[str, str] = {
    "dflick.stibee.com": "디플릭",
    "deepdaiv.stibee.com": "Weekly deep daiv.",
    "hai-hancom.stibee.com": "한컴 AI",
    "innoforest.stibee.com": "혁신의 숲",
    "trendlite.stibee.com": "트렌드라이트",
}

FOOTER_HINTS = (
    "구독 취소",
    "구독취소",
    "수신거부",
    "unsubscribe",
    "정보 변경",
    "All Rights Reserved",
)
FOOTER_HREF = (
    "page.stibee.com/subscribers/auth",
    "page.stibee.com/subscriptions",
)

REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
TABLE_TAGS = ["table", "thead", "tbody", "tfoot", "tr", "td", "th"]
MIN_PROSE_DENSITY = 0.5
TRACKING_QUERY_PREFIXES = (
    "utm_",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ck_subscriber_id",
    "_hsenc",
    "_hsmi",
)


@dataclass(frozen=True)
class _SitemapIssue:
    url: str
    published_at: datetime


@dataclass(frozen=True)
class _ParsedIssue:
    title: str
    source_name: str | None
    body: str
    prose_density: float


async def fetch_newsletters_recent(
    newsletters: list[str], hours: int = 168
) -> list[RawContent]:
    """Stibee 뉴스레터에서 최근 ``hours`` 이내 발행호를 수집한다.

    ``DEFAULT_NEWSLETTERS``를 기본 대상 목록으로 제공하며, 호출자가 전달한
    ``newsletters`` 목록으로 수집 대상을 정할 수 있다. 한 사이트나 발행호의
    실패는 다른 대상의 수집을 중단시키지 않는다.
    """
    items: list[RawContent] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "ds-digest-newsletter-collector/1.0"},
    ) as client:
        for newsletter_url in newsletters:
            try:
                base_url = _normalize_base_url(newsletter_url)
                sitemap_url = urljoin(base_url, "sitemap.xml")
                response = await client.get(sitemap_url)
                response.raise_for_status()
                issues = _parse_sitemap(response.content)

                if not issues:
                    logger.warning(
                        "newsletter_sitemap_empty",
                        newsletter=base_url,
                        sitemap_url=sitemap_url,
                    )
                    continue

                recent_issues = [
                    issue
                    for issue in issues
                    if _is_within_hours(issue.published_at, hours)
                ]
                configured_name = NEWSLETTER_NAMES.get(urlparse(base_url).netloc.lower())

                for issue in recent_issues:
                    if issue.url in seen_urls:
                        continue

                    try:
                        issue_response = await client.get(issue.url)
                        issue_response.raise_for_status()
                        parsed = _parse_issue_page(issue_response.text, issue.url)
                        if parsed is None:
                            continue

                        source_name = (
                            configured_name
                            or parsed.source_name
                            or urlparse(base_url).netloc
                        )
                        items.append(
                            RawContent(
                                source_type=SourceType.NEWSLETTER,
                                source_name=source_name,
                                # netloc은 표시명이 바뀌어도 안정적인 식별자다.
                                source_key=urlparse(base_url).netloc,
                                source_label=source_name,
                                title=parsed.title,
                                url=issue.url,
                                published_at=issue.published_at,
                                body=parsed.body,
                            )
                        )
                        seen_urls.add(issue.url)
                        logger.info(
                            "newsletter_collected",
                            newsletter=source_name,
                            title=parsed.title,
                            url=issue.url,
                            published_at=issue.published_at.isoformat(),
                            prose_density=round(parsed.prose_density, 4),
                        )
                    except Exception as exc:
                        logger.error(
                            "newsletter_issue_fetch_failed",
                            newsletter=base_url,
                            url=issue.url,
                            error=str(exc),
                        )

            except Exception as exc:
                logger.error(
                    "newsletter_fetch_failed",
                    newsletter=newsletter_url,
                    error=str(exc),
                )

    return items


def _normalize_base_url(url: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid newsletter URL: {url!r}")
    return normalized.rstrip("/") + "/"


def _parse_sitemap(xml: bytes | str) -> list[_SitemapIssue]:
    """sitemap에서 ``/p/{id}`` 발행호 URL과 발행일을 추출한다."""
    root = ElementTree.fromstring(xml)
    issues: list[_SitemapIssue] = []

    for url_node in root.iter():
        if _local_name(url_node.tag) != "url":
            continue

        loc = _child_text(url_node, "loc")
        lastmod = _child_text(url_node, "lastmod")
        if not loc or not lastmod:
            logger.warning(
                "newsletter_sitemap_entry_incomplete",
                url=loc,
                has_lastmod=bool(lastmod),
            )
            continue

        path = urlparse(loc).path
        if re.fullmatch(r"/p/[^/]+/?", path) is None:
            continue

        try:
            published_at = _parse_datetime(lastmod)
        except ValueError as exc:
            logger.warning(
                "newsletter_sitemap_date_invalid",
                url=loc,
                lastmod=lastmod,
                error=str(exc),
            )
            continue

        issues.append(_SitemapIssue(url=loc.rstrip("/"), published_at=published_at))

    return sorted(issues, key=lambda issue: issue.published_at, reverse=True)


def _parse_issue_page(html: str, url: str) -> _ParsedIssue | None:
    """발행호 HTML에서 실제 이메일 문서의 제목과 본문을 추출한다.

    Stibee 아카이브 페이지는 Next.js RSC 중복본 때문에 ``html`` 요소가 세 개
    중첩된다. 마지막 요소가 실제 이메일 문서이므로 그 안의 본문 블록만 쓴다.
    """
    soup = BeautifulSoup(html, "html.parser")
    html_documents = soup.find_all("html")
    document: BeautifulSoup | Tag = html_documents[-1] if html_documents else soup
    blocks = list(document.select(".stb-block-outer"))

    if not blocks:
        logger.warning(
            "newsletter_issue_empty",
            url=url,
            reason="no_stb_blocks",
            html_document_count=len(html_documents),
        )
        return None

    content_blocks: list[Tag] = []
    for block in blocks:
        if _is_footer_block(block):
            break
        content_blocks.append(block)

    if not content_blocks:
        logger.warning(
            "newsletter_issue_empty",
            url=url,
            reason="footer_at_first_block",
            html_document_count=len(html_documents),
        )
        return None

    body_html = "\n".join(str(block) for block in content_blocks)
    body = markdownify(
        body_html,
        heading_style="ATX",
        bullets="-",
        wrap=False,
        strip=TABLE_TAGS,
    )
    body = re.sub(
        r"(\]\()([^\s)]+)(\))",
        lambda match: f"{match.group(1)}{_tidy_url(match.group(2))}{match.group(3)}",
        body,
    )
    body = _clean_markdown(body)

    if not body:
        logger.warning(
            "newsletter_issue_empty",
            url=url,
            reason="markdown_empty",
            block_count=len(content_blocks),
        )
        return None

    prose_chars = re.sub(r"[|\-\s]", "", body)
    prose_density = len(prose_chars) / len(body)
    if prose_density < MIN_PROSE_DENSITY:
        logger.warning(
            "newsletter_prose_density_low",
            url=url,
            prose_density=round(prose_density, 4),
            minimum_prose_density=MIN_PROSE_DENSITY,
            body_chars=len(body),
            prose_chars=len(prose_chars),
        )

    title = (
        _meta_content(soup, "property", "og:title")
        or _meta_content(soup, "name", "twitter:title")
        or _tag_text(soup.find("title"))
        or _tag_text(document.find(["h1", "h2"]))
        or f"뉴스레터 {urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]}"
    )
    source_name = _meta_content(soup, "property", "og:site_name")

    return _ParsedIssue(
        title=title,
        source_name=source_name,
        body=body,
        prose_density=prose_density,
    )


def _is_footer_block(block: Tag) -> bool:
    text = block.get_text(" ", strip=True).casefold()
    has_footer_text = any(hint.casefold() in text for hint in FOOTER_HINTS)
    if has_footer_text:
        return True

    # 상단의 "구독하기" 버튼도 subscriptions 경로를 사용한다. 명시적인
    # 구독 버튼은 푸터가 아니며, 실제 푸터 문구는 위의 텍스트 힌트로 잡힌다.
    if "구독하기" in text:
        return False

    has_footer_href = any(
        hint.casefold() in str(link.get("href") or "").casefold()
        for link in block.find_all("a")
        for hint in FOOTER_HREF
    )
    return has_footer_href


def _clean_markdown(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _tidy_url(url: str) -> str:
    """링크 URL의 추적 정보와 불필요한 인코딩을 안전하게 정리한다."""
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", url):
            return url

        parsed = urlsplit(url)
        kept_query_parts: list[str] = []
        for part in parsed.query.split("&") if parsed.query else []:
            raw_key = part.partition("=")[0]
            key = unquote_plus(raw_key, encoding="utf-8", errors="strict").casefold()
            if not key.startswith(TRACKING_QUERY_PREFIXES):
                kept_query_parts.append(part)

        tidied = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "&".join(kept_query_parts),
                "",
            )
        )
        return unquote(tidied, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return url


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)


def _is_within_hours(published_at: datetime, hours: int) -> bool:
    if published_at.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(timezone.utc).astimezone(published_at.tzinfo)
    age = now - published_at
    return timedelta(0) <= age <= timedelta(hours=hours)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ElementTree.Element, name: str) -> str | None:
    for child in node:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _meta_content(soup: BeautifulSoup, attribute: str, value: str) -> str | None:
    meta = soup.find("meta", attrs={attribute: value})
    if meta is None:
        return None
    content = str(meta.get("content") or "").strip()
    return content or None


def _tag_text(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    text = tag.get_text(" ", strip=True)
    return text or None
