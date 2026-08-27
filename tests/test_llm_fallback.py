"""
LLM 제공자 폴백 회귀 테스트

2026-07-17경 Groq 모델이 404(폐기 추정)를 내기 시작하자 다이제스트가
3일간 발행되지 않았다. 원인은 모델 폐기 자체가 아니라 **폴백이 없었던 것**이다.

이전 구현:
    if settings.groq_api_key:
        data = await _call_groq(prompt)     # 실패해도 여기서 끝
    else:
        data = await _call_gemini(prompt)

즉 폴백이 "키가 없을 때"만 동작하고 "호출이 실패했을 때"는 동작하지 않았다.
그 결과 수집 6건 전부 analysis_failed -> relevance 0 -> 전량 탈락했다.

실행: pytest tests/test_llm_fallback.py -v
"""
import asyncio
from unittest.mock import patch

import httpx
import pytest

import app.analyzer as analyzer
from app.config import get_settings


GROQ_404 = "Client error '404 Not Found' for url 'https://api.groq.com/openai/v1/chat/completions'"
VALID_RESPONSE = {"actionability": 8, "depth": 7, "one_line_summary": "요약"}


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_keys(monkeypatch, groq: str, gemini: str) -> None:
    monkeypatch.setenv("GROQ_API_KEY", groq)
    monkeypatch.setenv("GEMINI_API_KEY", gemini)
    get_settings.cache_clear()


async def _fail(_prompt, message="boom", **_kwargs):
    raise Exception(message)


async def _succeed(_prompt, **_kwargs):
    return VALID_RESPONSE


def test_groq_failure_falls_back_to_gemini(monkeypatch):
    """제공자 하나가 죽어도 파이프라인은 살아야 한다 — 이 테스트가 이번 장애의 핵심이다."""
    _set_keys(monkeypatch, "groq-key", "gemini-key")

    async def groq_404(_prompt, **_kwargs):
        raise Exception(GROQ_404)

    with patch.object(analyzer, "_call_groq", groq_404), \
         patch.object(analyzer, "_call_gemini", _succeed):
        result = asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))

    assert result == VALID_RESPONSE


def test_groq_success_does_not_call_gemini(monkeypatch):
    """Groq가 정상이면 Gemini를 부르지 않는다 (불필요한 비용·지연 방지)."""
    _set_keys(monkeypatch, "groq-key", "gemini-key")
    gemini_called = False

    async def gemini_spy(_prompt, **_kwargs):
        nonlocal gemini_called
        gemini_called = True
        return VALID_RESPONSE

    with patch.object(analyzer, "_call_groq", _succeed), \
         patch.object(analyzer, "_call_gemini", gemini_spy):
        result = asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))

    assert result == VALID_RESPONSE
    assert gemini_called is False


def test_both_providers_fail_raises(monkeypatch):
    """둘 다 실패하면 예외를 올려 보내 기존 스킵 처리를 따른다."""
    _set_keys(monkeypatch, "groq-key", "gemini-key")

    async def groq_404(_prompt, **_kwargs):
        raise Exception(GROQ_404)

    async def gemini_down(_prompt, **_kwargs):
        raise Exception("gemini down")

    with patch.object(analyzer, "_call_groq", groq_404), \
         patch.object(analyzer, "_call_gemini", gemini_down):
        with pytest.raises(Exception, match="gemini down"):
            asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))


def test_no_groq_key_uses_gemini_directly(monkeypatch):
    """Groq 키가 없으면 처음부터 Gemini를 쓴다 (기존 동작 유지)."""
    _set_keys(monkeypatch, "", "gemini-key")
    groq_called = False

    async def groq_spy(_prompt, **_kwargs):
        nonlocal groq_called
        groq_called = True
        return VALID_RESPONSE

    with patch.object(analyzer, "_call_groq", groq_spy), \
         patch.object(analyzer, "_call_gemini", _succeed):
        result = asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))

    assert result == VALID_RESPONSE
    assert groq_called is False


def test_groq_fails_and_no_gemini_key_raises_original_error(monkeypatch):
    """Gemini 키가 없으면 Groq의 원래 오류를 그대로 올려 원인 추적이 가능해야 한다."""
    _set_keys(monkeypatch, "groq-key", "")

    async def groq_404(_prompt, **_kwargs):
        raise Exception(GROQ_404)

    with patch.object(analyzer, "_call_groq", groq_404):
        with pytest.raises(Exception, match="404 Not Found"):
            asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))


def test_no_provider_configured_raises_clear_error(monkeypatch):
    """키가 하나도 없으면 원인이 분명한 오류를 낸다."""
    _set_keys(monkeypatch, "", "")

    with pytest.raises(RuntimeError, match="LLM 제공자"):
        asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))


# ──────────────────────────────────────────────
# strict 구조화 출력 (2026-08-16 llama 셧다운 대응)
# ──────────────────────────────────────────────
#
# 랭킹은 이제 모델 교체가 아니라 constrained decoding으로 출력을 강제한다.
# 다만 Groq의 json_schema 지원은 모델별로 들쭉날쭉하고(커뮤니티 실측), 지원이
# 빠지면 400이 온다. 그때 파이프라인이 멈추면 안 되므로 json_object로 강등한다.

class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers: dict = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


def _content(body: str) -> dict:
    return {"choices": [{"message": {"content": body}}]}


def _record_posts(monkeypatch, responses):
    """AsyncClient.post 대역. 보낸 payload를 순서대로 캡처한다."""
    sent: list[dict] = []
    queue = list(responses)

    async def _post(_self, _url, headers=None, json=None, **_kwargs):
        sent.append(json)
        if not queue:
            raise AssertionError("mock 응답보다 많은 호출이 발생했습니다")
        return queue.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return sent


SCHEMA = {"name": "t", "strict": True, "schema": {"type": "object"}}


def test_groq_sends_json_schema_when_given(monkeypatch):
    """json_schema를 주면 strict 구조화 출력으로 요청해야 한다."""
    _set_keys(monkeypatch, "groq-key", "")
    sent = _record_posts(monkeypatch, [_Resp(200, _content('{"top_indices": [1, 0]}'))])

    result = asyncio.run(analyzer._call_groq("prompt", json_schema=SCHEMA))

    assert result == {"top_indices": [1, 0]}
    assert sent[0]["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}


def test_groq_json_schema_rejected_falls_back_to_json_object(monkeypatch):
    """모델이 스키마를 거부(400)하면 json_object로 내려가 계속 진행한다.

    구조화 출력 하나 때문에 랭킹이 죽으면 first_n 폴백으로 떨어져 큐레이션이
    통째로 사라진다. 파싱은 _parse_top_indices가 방어하므로 강등이 안전하다.
    """
    _set_keys(monkeypatch, "groq-key", "")
    sent = _record_posts(monkeypatch, [
        _Resp(400, text="response_format json_schema not supported"),
        _Resp(200, _content('{"top_indices": [2]}')),
    ])

    result = asyncio.run(analyzer._call_groq("prompt", json_schema=SCHEMA))

    assert result == {"top_indices": [2]}
    assert len(sent) == 2
    assert sent[1]["response_format"] == {"type": "json_object"}


def test_groq_without_schema_uses_json_object(monkeypatch):
    """분석 경로는 기존대로 json_object 그대로여야 한다(회귀 방지)."""
    _set_keys(monkeypatch, "groq-key", "")
    sent = _record_posts(monkeypatch, [_Resp(200, _content('{"depth": 4}'))])

    asyncio.run(analyzer._call_groq("prompt"))

    assert sent[0]["response_format"] == {"type": "json_object"}


def test_groq_400_without_schema_still_raises(monkeypatch):
    """스키마를 안 쓴 호출의 400은 그대로 올려보내 폴백이 동작하게 한다."""
    _set_keys(monkeypatch, "groq-key", "")
    _record_posts(monkeypatch, [_Resp(400, text="bad request")])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(analyzer._call_groq("prompt"))
