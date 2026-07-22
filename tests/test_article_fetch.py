"""_fetch_article_body 단위 테스트.

링크 기사 본문 fetch 헬퍼가 성공/비200/비HTML/비http/예외 상황에서
모두 raise 없이 안전하게 동작하는지 고정한다. 실제 네트워크는 쓰지 않고
httpx.AsyncClient 대역으로 응답을 주입한다.

실행: pytest tests/test_article_fetch.py -v
"""
import asyncio

from app.collectors import _fetch_article_body


class _FakeResponse:
    """httpx.Response 중 헬퍼가 실제로 쓰는 부분만 흉내낸다."""

    def __init__(self, status_code=200, text="", content_type="text/html; charset=utf-8"):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


class _FakeClient:
    """지정한 응답을 돌려주거나 예외를 던지는 httpx.AsyncClient 대역."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls: list[str] = []

    async def get(self, url, follow_redirects=False):
        self.calls.append(url)
        if self._error is not None:
            raise self._error
        return self._response


def _run(client, url):
    return asyncio.run(_fetch_article_body(client, url))


def test_success_extracts_article_body_as_markdown():
    """<article> 본문만 Markdown으로 뽑고 footer 등 비본문은 제거한다."""
    html = (
        "<html><body>"
        "<article><h1>제목</h1><p>본문 내용</p></article>"
        "<footer>구독 취소 안내</footer>"
        "</body></html>"
    )
    client = _FakeClient(_FakeResponse(text=html))

    body = _run(client, "https://example.com/a")

    assert "제목" in body
    assert "본문 내용" in body
    assert "구독 취소" not in body  # footer 제거 확인


def test_non_200_returns_empty():
    """200이 아니면 본문이 있어도 ""를 반환한다."""
    client = _FakeClient(_FakeResponse(status_code=404, text="<html>없음</html>"))
    assert _run(client, "https://example.com/404") == ""


def test_non_html_content_type_returns_empty():
    """content-type이 html이 아니면 ""를 반환한다."""
    client = _FakeClient(_FakeResponse(text="{}", content_type="application/json"))
    assert _run(client, "https://example.com/api.json") == ""


def test_non_http_url_returns_empty_without_request():
    """http(s)가 아니면 요청조차 하지 않고 ""를 반환한다."""
    client = _FakeClient(_FakeResponse(text="<html></html>"))

    result = _run(client, "ftp://example.com/file")

    assert result == ""
    assert client.calls == []


def test_exception_is_swallowed_and_returns_empty():
    """네트워크 예외 등 어떤 예외도 삼키고 ""를 반환한다(파이프라인 보호)."""
    client = _FakeClient(error=RuntimeError("network down"))
    assert _run(client, "https://example.com/boom") == ""


def test_body_is_capped_at_5000_chars():
    """추출 본문은 5000자로 캡한다."""
    html = "<html><body><article>" + "가" * 8000 + "</article></body></html>"
    client = _FakeClient(_FakeResponse(text=html))

    body = _run(client, "https://example.com/long")

    assert len(body) <= 5000
