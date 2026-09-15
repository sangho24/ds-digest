"""RSS/YouTube 수집기의 HTTP 계층 회귀 테스트.

2026-09 실측: 9개 소스 계열이 15일 내내 수집 0건이었는데 로그에는 실패가 한 줄도
없었다. 원인은 둘이었다.
- 기본 User-Agent(`python-httpx/x.y`)를 막는 서버가 있다. 우아한형제들 기술블로그는
  403 HTML 을 돌려주고, 식별형 UA 로는 200 RSS 를 돌려준다.
- 응답 상태를 보지 않아 403/404/406 이 예외 없이 "항목 0개"로 흘렀다. "가져오기
  실패"와 "새 글 없음"이 구분되지 않았다.

실제 네트워크는 쓰지 않는다. httpx.MockTransport 를 끼운 진짜 AsyncClient 를 써서
요청 헤더와 리다이렉트 처리까지 httpx 가 실제로 하는 그대로 검증한다.

실행: .venv/bin/python -m pytest tests/test_collector_http.py -v
"""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
import structlog

import app.collectors as collectors
from app.collectors import fetch_rss_recent, fetch_youtube_recent


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _install_transport(monkeypatch, handler):
    """collectors 가 만드는 AsyncClient 에 MockTransport 를 끼운다.

    생성 인자(timeout, headers 등)는 그대로 넘기므로 수집기가 설정한 UA 와
    리다이렉트 옵션이 실제 요청에 반영된다.
    """
    requests: list[httpx.Request] = []

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_recording_handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(collectors.httpx, "AsyncClient", _factory)
    return requests


def _is_identifying_ua(request: httpx.Request) -> bool:
    ua = request.headers.get("user-agent", "")
    return ua.startswith("ds-digest/") and "python-httpx" not in ua


def _rss_xml(titles: list[str]) -> bytes:
    """48시간 창 안에 드는 항목들로 RSS 2.0 을 만든다. 본문은 500자 이상이라
    링크 원문 fetch 가 일어나지 않는다."""
    published = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))
    body = "본문 " * 300
    items = "".join(
        f"<item><title>{t}</title><link>https://blog.example/{i}</link>"
        f"<pubDate>{published}</pubDate><description>{body}</description></item>"
        for i, t in enumerate(titles)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>테스트 블로그</title>{items}</channel></rss>"
    ).encode("utf-8")


_YT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
    'xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">'
    "<title>테스트 채널</title>"
    "<entry><id>yt:video:abc123</id><yt:videoId>abc123</yt:videoId>"
    "<title>영상 하나</title>"
    '<link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>'
    "<published>2026-09-14T00:00:00+00:00</published>"
    "<media:group><media:description>설명글</media:description></media:group>"
    "</entry></feed>"
).encode("utf-8")

_BLOCK_PAGE = b"<!DOCTYPE html><html><body><h1>403 Forbidden</h1></body></html>"


# ──────────────────────────────────────────────
# User-Agent
# ──────────────────────────────────────────────

def test_rss_request_carries_identifying_user_agent(monkeypatch):
    requests = _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, content=_rss_xml(["글"]),
                                   headers={"content-type": "application/rss+xml"}),
    )

    asyncio.run(fetch_rss_recent(["https://blog.example/feed/"], fetch_link_body=False))

    assert requests, "요청이 나가지 않았다"
    assert _is_identifying_ua(requests[0]), requests[0].headers.get("user-agent")


def test_youtube_request_carries_identifying_user_agent(monkeypatch):
    requests = _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, content=_YT_XML, headers={"content-type": "text/xml"}),
    )

    asyncio.run(fetch_youtube_recent(["UCtest"]))

    assert requests, "요청이 나가지 않았다"
    assert _is_identifying_ua(requests[0]), requests[0].headers.get("user-agent")


def test_rss_parses_feed_from_server_that_blocks_default_user_agent(monkeypatch):
    """우아한형제들 재현: 기본 UA 는 403 HTML, 식별형 UA 는 200 RSS.

    200 정상 경로의 파싱 결과(제목·링크·소스 키·본문)가 그대로인지도 함께 고정한다.
    """
    def handler(req):
        if not _is_identifying_ua(req):
            return httpx.Response(403, content=_BLOCK_PAGE, headers={"content-type": "text/html"})
        return httpx.Response(200, content=_rss_xml(["첫 글", "둘째 글"]),
                              headers={"content-type": "application/rss+xml; charset=UTF-8"})

    _install_transport(monkeypatch, handler)
    feed_url = "https://techblog.example/feed/"

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_rss_recent([feed_url], fetch_link_body=False))

    assert [i.title for i in items] == ["첫 글", "둘째 글"]
    assert [i.url for i in items] == ["https://blog.example/0", "https://blog.example/1"]
    assert all(i.source_key == feed_url and i.source_name == "테스트 블로그" for i in items)
    assert all(i.body.startswith("본문") for i in items)
    assert not [e for e in logs if e["event"] == "rss_fetch_failed"]


# ──────────────────────────────────────────────
# 응답 상태
# ──────────────────────────────────────────────

def test_rss_non_2xx_logs_failure_and_other_feeds_continue(monkeypatch):
    """403 소스는 실패 로그(url, status_code)를 남기고 빈 결과로 넘어간다.
    같은 실행의 다른 피드는 정상 수집돼야 한다."""
    bad, good = "https://dead.example/feed/", "https://alive.example/feed/"

    def handler(req):
        if str(req.url) == bad:
            return httpx.Response(403, content=_BLOCK_PAGE, headers={"content-type": "text/html"})
        return httpx.Response(200, content=_rss_xml(["살아있는 글"]),
                              headers={"content-type": "application/rss+xml"})

    _install_transport(monkeypatch, handler)

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_rss_recent([bad, good], fetch_link_body=False))

    assert [i.source_key for i in items] == [good]
    failures = [e for e in logs if e["event"] == "rss_fetch_failed"]
    assert len(failures) == 1, logs
    assert failures[0]["url"] == bad
    assert failures[0]["status_code"] == 403
    assert failures[0]["log_level"] == "error"


def test_youtube_non_2xx_logs_failure_and_returns_empty(monkeypatch):
    """삭제된 채널은 404 HTML 이다. 조용한 0건이 아니라 실패로 남아야 한다."""
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(404, content=b"<html>Not Found</html>",
                                   headers={"content-type": "text/html"}),
    )

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_youtube_recent(["UCgone"]))

    assert items == []
    failures = [e for e in logs if e["event"] == "youtube_fetch_failed"]
    assert len(failures) == 1, logs
    assert failures[0]["channel_id"] == "UCgone"
    assert failures[0]["status_code"] == 404
    assert "UCgone" in failures[0]["url"]


def test_youtube_follows_redirect(monkeypatch):
    """arXiv 는 http→https 301 을 따라가지 않아 본문이 비고 5개월간 0건이었다.
    YouTube 피드도 리다이렉트되면 따라가서 파싱해야 한다."""
    target = "https://www.youtube.com/relocated/videos.xml"

    def handler(req):
        # 채널 id 가 판별 문자열을 포함하지 않도록 목적지 URL 전체로 비교한다.
        if str(req.url) != target:
            return httpx.Response(301, headers={"location": target})
        return httpx.Response(200, content=_YT_XML, headers={"content-type": "text/xml"})

    requests = _install_transport(monkeypatch, handler)

    items = asyncio.run(fetch_youtube_recent(["UCredirect"]))

    assert [str(r.url) for r in requests][-1] == target, "리다이렉트를 따라가지 않았다"
    assert [i.url for i in items] == ["https://youtu.be/abc123"]


def test_rss_200_html_without_entries_warns(monkeypatch):
    """200 인데 HTML 이고 항목이 0개면 차단·안내 페이지일 가능성이 높다.
    실패로 세지는 않지만 경고를 남겨 "새 글 없음"과 구분한다."""
    url = "https://challenge.example/feed/"
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, content=b"<html><body>Just a moment...</body></html>",
                                   headers={"content-type": "text/html; charset=UTF-8"}),
    )

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_rss_recent([url], fetch_link_body=False))

    assert items == []
    assert not [e for e in logs if e["event"] == "rss_fetch_failed"], "200 은 실패로 세지 않는다"
    warnings = [e for e in logs if e["event"] == "rss_feed_html_response"]
    assert len(warnings) == 1, logs
    assert warnings[0]["url"] == url
    assert warnings[0]["log_level"] == "warning"


def test_rss_html_content_type_with_entries_does_not_warn(monkeypatch):
    """content-type 을 text/html 로 잘못 붙인 정상 피드는 항목이 파싱되므로 경고하지 않는다.
    조건을 `"html" in content_type` 하나로만 두면 이 경우가 오탐이 된다."""
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, content=_rss_xml(["잘못 표기된 피드의 글"]),
                                   headers={"content-type": "text/html; charset=UTF-8"}),
    )

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_rss_recent(["https://mislabeled.example/feed/"],
                                             fetch_link_body=False))

    assert [i.title for i in items] == ["잘못 표기된 피드의 글"]
    assert not [e for e in logs if e["event"] == "rss_feed_html_response"], logs


@pytest.mark.parametrize("case", ["empty_channel", "only_old_entries"])
def test_rss_xml_without_new_entries_does_not_warn(monkeypatch, case):
    """XML 피드에 새 글이 0건인 것은 정상("새 글 없음")이다. 경고하지 않는다.
    empty_channel 은 항목 자체가 0개라 조건을 `not feed.entries` 하나로만 두면 오탐이 된다."""
    if case == "empty_channel":
        items_xml = ""
    else:
        old = format_datetime(datetime.now(timezone.utc) - timedelta(days=30))
        items_xml = (f"<item><title>오래된 글</title><link>https://blog.example/old</link>"
                     f"<pubDate>{old}</pubDate><description>본문</description></item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
           f"<title>조용한 블로그</title>{items_xml}</channel></rss>").encode("utf-8")
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, content=xml,
                                   headers={"content-type": "application/rss+xml; charset=UTF-8"}),
    )

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_rss_recent(["https://quiet.example/feed/"], fetch_link_body=False))

    assert items == []
    assert not [e for e in logs if e["event"] == "rss_feed_html_response"], logs
    assert not [e for e in logs if e["event"] == "rss_fetch_failed"], logs


_NON_2XX_FINAL = [
    pytest.param(302, {}, id="302-without-location"),
    pytest.param(304, {}, id="304-not-modified"),
]


@pytest.mark.parametrize("status,headers", _NON_2XX_FINAL)
def test_rss_3xx_final_response_counts_as_failure(monkeypatch, status, headers):
    """성공은 2xx 만이다. 따라갈 Location 이 없는 302 나 304 가 최종 응답이면
    본문이 비어 0건이 되므로, 조용히 넘기지 말고 실패로 센다."""
    url = "https://moved.example/feed/"
    _install_transport(monkeypatch, lambda req: httpx.Response(status, headers=headers))

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_rss_recent([url], fetch_link_body=False))

    assert items == []
    failures = [e for e in logs if e["event"] == "rss_fetch_failed"]
    assert len(failures) == 1, logs
    assert failures[0]["url"] == url
    assert failures[0]["status_code"] == status


@pytest.mark.parametrize("status,headers", _NON_2XX_FINAL)
def test_youtube_3xx_final_response_counts_as_failure(monkeypatch, status, headers):
    _install_transport(monkeypatch, lambda req: httpx.Response(status, headers=headers))

    with structlog.testing.capture_logs() as logs:
        items = asyncio.run(fetch_youtube_recent(["UCnoloc"]))

    assert items == []
    failures = [e for e in logs if e["event"] == "youtube_fetch_failed"]
    assert len(failures) == 1, logs
    assert failures[0]["channel_id"] == "UCnoloc"
    assert failures[0]["status_code"] == status


def test_network_error_log_names_exception_type(monkeypatch):
    """ConnectError 는 str(e)가 비어 error= 만으로는 원인을 알 수 없었다.
    상태 코드 실패(HTTP 403)와 네트워크 문제(확인 불가)를 로그에서 구분해야 한다."""
    def handler(req):
        raise httpx.ConnectError("", request=req)

    _install_transport(monkeypatch, handler)

    with structlog.testing.capture_logs() as logs:
        rss = asyncio.run(fetch_rss_recent(["https://blocked.example/feed/"], fetch_link_body=False))
        yt = asyncio.run(fetch_youtube_recent(["UCblocked"]))

    assert rss == [] and yt == []
    rss_fail = [e for e in logs if e["event"] == "rss_fetch_failed"]
    yt_fail = [e for e in logs if e["event"] == "youtube_fetch_failed"]
    assert len(rss_fail) == 1 and len(yt_fail) == 1, logs
    assert rss_fail[0]["error"].startswith("ConnectError"), rss_fail[0]
    assert yt_fail[0]["error"].startswith("ConnectError"), yt_fail[0]
    assert "status_code" not in rss_fail[0]
