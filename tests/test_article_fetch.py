"""_fetch_article_body 단위 테스트.

링크 기사 본문 fetch 헬퍼가 성공/비200/비HTML/비http/예외 상황에서
모두 raise 없이 안전하게 동작하는지 고정한다. 실제 네트워크는 쓰지 않고
httpx.AsyncClient 대역으로 응답을 주입한다.

실행: pytest tests/test_article_fetch.py -v
"""
import asyncio

import app.collectors as collectors
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


# ──────────────────────────────────────────────
# HackerNews 후보 상한 (수집 편중 회귀)
# ──────────────────────────────────────────────
#
# 상한이 없을 때 키워드 4개 × Algolia 기본 20건 = 최대 80건이 후보 풀에 들어왔다.
# RSS 블로그 하나가 48시간에 1~3건, YouTube 채널이 3건으로 캡되는 것과 자릿수가
# 다르다. 실측 40일 165건에서 HN이 32.3%를 차지한 원인이다 — 더 좋아서가 아니라
# 후보를 훨씬 많이 냈기 때문이다.

from app.collectors import fetch_hackernews_recent  # noqa: E402


class _HNClient:
    """Algolia 응답과 본문 fetch를 함께 대역하는 AsyncClient."""

    body_fetches: list[str] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        if "hn.algolia.com" in url:
            hits = [
                {"objectID": str(i), "title": f"story{i}", "points": i,
                 "url": f"https://ext.example/{i}", "story_text": ""}
                for i in range(20)
            ]
            return _Resp(200, {"hits": hits})
        _HNClient.body_fetches.append(url)
        return _Resp(200, {}, text="본문 " * 300)


class _Resp:
    def __init__(self, code, payload, text=""):
        self.status_code, self._p, self.text = code, payload, text
        self.headers = {"content-type": "text/html"}

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def test_hackernews_respects_max_items(monkeypatch):
    monkeypatch.setattr(collectors.httpx, "AsyncClient", _HNClient)

    items = asyncio.run(fetch_hackernews_recent(["a", "b"], max_items=5, fetch_link_body=False))

    assert len(items) == 5


def test_hackernews_keeps_highest_points(monkeypatch):
    """상한에 걸리면 점수 높은 것부터 남는다."""
    monkeypatch.setattr(collectors.httpx, "AsyncClient", _HNClient)

    items = asyncio.run(fetch_hackernews_recent(["a"], max_items=3, fetch_link_body=False))

    assert [i.title for i in items] == ["story19", "story18", "story17"]


def test_hackernews_does_not_fetch_bodies_for_dropped_items(monkeypatch):
    """버릴 아이템의 원문을 가져오면 순수한 낭비다 — 이 수집기에서 가장 비싼 작업이다."""
    _HNClient.body_fetches = []
    monkeypatch.setattr(collectors.httpx, "AsyncClient", _HNClient)

    asyncio.run(fetch_hackernews_recent(["a"], max_items=3, fetch_link_body=True))

    assert len(_HNClient.body_fetches) == 3, f"버린 아이템까지 fetch했다: {_HNClient.body_fetches}"
