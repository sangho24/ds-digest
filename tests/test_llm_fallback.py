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


async def _fail(_prompt, message="boom"):
    raise Exception(message)


async def _succeed(_prompt):
    return VALID_RESPONSE


def test_groq_failure_falls_back_to_gemini(monkeypatch):
    """제공자 하나가 죽어도 파이프라인은 살아야 한다 — 이 테스트가 이번 장애의 핵심이다."""
    _set_keys(monkeypatch, "groq-key", "gemini-key")

    async def groq_404(_prompt):
        raise Exception(GROQ_404)

    with patch.object(analyzer, "_call_groq", groq_404), \
         patch.object(analyzer, "_call_gemini", _succeed):
        result = asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))

    assert result == VALID_RESPONSE


def test_groq_success_does_not_call_gemini(monkeypatch):
    """Groq가 정상이면 Gemini를 부르지 않는다 (불필요한 비용·지연 방지)."""
    _set_keys(monkeypatch, "groq-key", "gemini-key")
    gemini_called = False

    async def gemini_spy(_prompt):
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

    async def groq_404(_prompt):
        raise Exception(GROQ_404)

    async def gemini_down(_prompt):
        raise Exception("gemini down")

    with patch.object(analyzer, "_call_groq", groq_404), \
         patch.object(analyzer, "_call_gemini", gemini_down):
        with pytest.raises(Exception, match="gemini down"):
            asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))


def test_no_groq_key_uses_gemini_directly(monkeypatch):
    """Groq 키가 없으면 처음부터 Gemini를 쓴다 (기존 동작 유지)."""
    _set_keys(monkeypatch, "", "gemini-key")
    groq_called = False

    async def groq_spy(_prompt):
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

    async def groq_404(_prompt):
        raise Exception(GROQ_404)

    with patch.object(analyzer, "_call_groq", groq_404):
        with pytest.raises(Exception, match="404 Not Found"):
            asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))


def test_no_provider_configured_raises_clear_error(monkeypatch):
    """키가 하나도 없으면 원인이 분명한 오류를 낸다."""
    _set_keys(monkeypatch, "", "")

    with pytest.raises(RuntimeError, match="LLM 제공자"):
        asyncio.run(analyzer._call_llm_with_fallback("prompt", "제목"))
