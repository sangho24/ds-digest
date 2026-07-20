"""Stibee 뉴스레터 파서의 네트워크 비의존 회귀 테스트."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import app.collectors_newsletter as collector
from app.collectors_newsletter import _parse_issue_page, _tidy_url, fetch_newsletters_recent
from app.models import SourceType


def _triple_html(body_blocks: str, *, title: str = "이번 주 뉴스") -> str:
    return rf"""
    <html>
      <head>
        <meta property="og:title" content="{title}">
        <meta property="og:site_name" content="테스트 뉴스레터">
      </head>
      <body>
        <script>"\u003chtml\u003e\u003cdiv class=\"stb-block-outer\"\u003eRSC 중복본 1\u003c/div\u003e"</script>
        <html><body>
          <script>"\u003chtml\u003e\u003cdiv class=\"stb-block-outer\"\u003eRSC 중복본 2\u003c/div\u003e"</script>
          <html><body>
            {body_blocks}
          </body></html>
        </body></html>
      </body>
    </html>
    """


def test_uses_third_html_document_instead_of_rsc_duplicates():
    html = _triple_html(
        """
        <div class="stb-block-outer"><h1>실제 본문 제목</h1></div>
        <div class="stb-block-outer"><p>실제 본문 내용입니다.</p></div>
        """
    )

    parsed = _parse_issue_page(html, "https://example.stibee.com/p/1")

    assert parsed is not None
    assert parsed.title == "이번 주 뉴스"
    assert "실제 본문 제목" in parsed.body
    assert "실제 본문 내용입니다." in parsed.body
    assert "RSC 중복본" not in parsed.body


@pytest.mark.parametrize("footer_text", ["구독 취소", "수신거부"])
def test_removes_footer_and_every_block_after_it(footer_text: str):
    html = _triple_html(
        f"""
        <div class="stb-block-outer"><p>남아야 하는 본문</p></div>
        <div class="stb-block-outer"><a href="">{footer_text}</a></div>
        <div class="stb-block-outer"><p>제거되어야 하는 푸터 뒷부분</p></div>
        """
    )

    parsed = _parse_issue_page(html, "https://example.stibee.com/p/2")

    assert parsed is not None
    assert "남아야 하는 본문" in parsed.body
    assert footer_text not in parsed.body
    assert "제거되어야 하는 푸터 뒷부분" not in parsed.body


def test_removes_footer_by_href_when_text_has_no_footer_hint():
    html = _triple_html(
        """
        <div class="stb-block-outer"><p>본문</p></div>
        <div class="stb-block-outer">
          <a href="https://page.stibee.com/subscribers/auth/123">설정</a>
        </div>
        <div class="stb-block-outer"><p>회사 주소</p></div>
        """
    )

    parsed = _parse_issue_page(html, "https://example.stibee.com/p/3")

    assert parsed is not None
    assert parsed.body == "본문"


def test_subscription_button_at_top_is_not_mistaken_for_footer():
    html = _triple_html(
        """
        <div class="stb-block-outer">
          <a href="https://page.stibee.com/subscriptions/123">구독하기</a>
        </div>
        <div class="stb-block-outer"><p>실제 본문</p></div>
        <div class="stb-block-outer"><a href="">구독 취소</a></div>
        """
    )

    parsed = _parse_issue_page(html, "https://example.stibee.com/p/4")

    assert parsed is not None
    assert "구독하기" in parsed.body
    assert "실제 본문" in parsed.body
    assert "구독 취소" not in parsed.body


def test_strips_nested_layout_tables_without_losing_body_text():
    html = _triple_html(
        """
        <div class="stb-block-outer">
          <table><tbody><tr><td>
            <table><tbody><tr><td>중첩 레이아웃 안의 본문</td></tr></tbody></table>
          </td></tr></tbody></table>
        </div>
        """
    )

    parsed = _parse_issue_page(html, "https://example.stibee.com/p/table")

    assert parsed is not None
    assert "중첩 레이아웃 안의 본문" in parsed.body
    assert "|" not in parsed.body


def test_warns_when_prose_density_is_below_threshold():
    html = _triple_html(
        """
        <div class="stb-block-outer">
          <p>본문</p>
          <p>------------------------------------------------------------</p>
        </div>
        """
    )

    with patch("app.collectors_newsletter.logger.warning") as warning:
        parsed = _parse_issue_page(html, "https://example.stibee.com/p/low-density")

    assert parsed is not None
    assert parsed.prose_density < 0.5
    warning.assert_called_once_with(
        "newsletter_prose_density_low",
        url="https://example.stibee.com/p/low-density",
        prose_density=round(parsed.prose_density, 4),
        minimum_prose_density=0.5,
        body_chars=len(parsed.body),
        prose_chars=2,
    )


def test_preserves_links_as_markdown():
    html = _triple_html(
        """
        <div class="stb-block-outer">
          <p><a href="https://example.com/article">원문 읽기</a></p>
        </div>
        """
    )

    parsed = _parse_issue_page(html, "https://example.stibee.com/p/link")

    assert parsed is not None
    assert "[원문 읽기](https://example.com/article)" in parsed.body


def test_tidy_url_removes_tracking_parameters_and_fragment():
    url = "https://example.com/article?id=123&utm_source=email&page=2&fbclid=abc#part"

    assert _tidy_url(url) == "https://example.com/article?id=123&page=2"


def test_tidy_url_decodes_percent_encoded_korean():
    url = "https://example.com/%ED%98%81%EC%8B%A0%EC%9D%98-%EC%88%B2"

    assert _tidy_url(url) == "https://example.com/혁신의-숲"


def test_tidy_url_preserves_normal_query_parameters():
    url = "https://example.com/article?id=123&page=2"

    assert _tidy_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/%FF",
        "https://example.com/%ZZ#fragment",
        "https://[invalid.example.com/article",
    ],
)
def test_tidy_url_preserves_invalid_url_without_raising(url: str):
    assert _tidy_url(url) == url


def test_link_cleanup_does_not_change_link_text():
    html = _triple_html(
        """
        <div class="stb-block-outer">
          <a href="https://example.com/%EC%9D%98?utm_campaign=test">%EC%9D%98 원문</a>
        </div>
        """
    )

    parsed = _parse_issue_page(html, "https://example.stibee.com/p/tidy-link")

    assert parsed is not None
    assert "[%EC%9D%98 원문](https://example.com/의)" in parsed.body


def test_empty_parse_warns_and_returns_none_instead_of_raising():
    html = _triple_html("<div class='not-a-content-block'>내용 없음</div>")

    with patch("app.collectors_newsletter.logger.warning") as warning:
        parsed = _parse_issue_page(html, "https://example.stibee.com/p/empty")

    assert parsed is None
    warning.assert_called_once()
    assert warning.call_args.args[0] == "newsletter_issue_empty"


def test_fetch_continues_after_site_failure_and_skips_empty_issue(monkeypatch):
    published = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")

    def sitemap(issue_url: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><lastmod>{published}</lastmod><loc>{issue_url}</loc></url>
        </urlset>"""

    responses = {
        "https://empty.stibee.com/sitemap.xml": sitemap(
            "https://empty.stibee.com/p/1"
        ),
        "https://empty.stibee.com/p/1": _triple_html(
            "<div class='not-a-content-block'>비어 있음</div>"
        ),
        "https://good.stibee.com/sitemap.xml": sitemap(
            "https://good.stibee.com/p/2"
        ),
        "https://good.stibee.com/p/2": _triple_html(
            '<div class="stb-block-outer"><p>수집된 본문</p></div>',
            title="정상 발행호",
        ),
    }

    class FakeResponse:
        def __init__(self, content: str):
            self.content = content.encode("utf-8")
            self.text = content

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.options = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, url: str):
            if url == "https://broken.stibee.com/sitemap.xml":
                raise OSError("network down")
            return FakeResponse(responses[url])

    monkeypatch.setattr(collector.httpx, "AsyncClient", FakeAsyncClient)

    with (
        patch("app.collectors_newsletter.logger.warning") as warning,
        patch("app.collectors_newsletter.logger.error") as error,
    ):
        items = asyncio.run(
            fetch_newsletters_recent(
                [
                    "https://broken.stibee.com/",
                    "https://empty.stibee.com/",
                    "https://good.stibee.com/",
                ]
            )
        )

    assert len(items) == 1
    assert items[0].source_type is SourceType.NEWSLETTER
    assert items[0].source_name == "테스트 뉴스레터"
    assert items[0].title == "정상 발행호"
    assert items[0].body == "수집된 본문"
    assert any(call.args[0] == "newsletter_issue_empty" for call in warning.call_args_list)
    assert error.call_args.args[0] == "newsletter_fetch_failed"
