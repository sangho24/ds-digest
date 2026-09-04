"""
Groq TPM 페이서 회귀 테스트

배경(2026-09-04 실측): Groq 무료 플랜 gpt-oss-120b 한도는 RPM 30, TPM 8,000 이다.
호출 한 건이 입력 약 2,800 + 출력 약 1,000 토큰이라 3초 고정 간격은 RPM만 지키고
TPM 은 못 지켰다. 31회 호출에서 429 가 32회 났고 Retry-After 는 12~36초였다.
즉 429 가 페이서 노릇을 하고 있었고 그 왕복·+2초 여유·정수 반올림이 낭비였다.

_GroqPacer 는 응답 헤더(x-ratelimit-*)와 usage 를 보고 다음 호출 전에 필요한
만큼만 기다린다. 이 파일은 그 계산과 429 대기 파싱, 그리고 가짜 서버(토큰버킷·
고정창)를 상대로 한 다중 호출 시나리오를 고정한다.

독립 검증(2026-09-04)에서 잡힌 4가지: 여유 0 으로 usage 흔들림에 절반이 429,
429 뒤 이중 대기, 헤더 없는 429·예외 뒤 예약 차감 잔류, 모델 간 상태 혼용.

실행: pytest tests/test_groq_pacing.py -v
"""
import asyncio
import math
import random
import time

import httpx
import pytest
import structlog

import app.analyzer as analyzer
from app.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    # 페이서는 모듈 단일 인스턴스라 테스트마다 새로 바꿔 끼운다
    monkeypatch.setattr(analyzer, "_groq_pacer", analyzer._GroqPacer())
    yield
    get_settings.cache_clear()


def _model() -> str:
    return get_settings().groq_model


def _state() -> "analyzer._GroqModelState":
    return analyzer._groq_pacer.state_for(_model())


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers: dict = headers or {}

    def json(self) -> dict:
        if not self._payload and self.text:
            raise ValueError("본문이 JSON 이 아닙니다")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


def _content(body: str, usage: dict | None = None) -> dict:
    payload = {"choices": [{"message": {"content": body}}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


def _ok(remaining_tokens=None, usage=None, **extra_headers) -> _Resp:
    headers = dict(extra_headers)
    if remaining_tokens is not None:
        headers["x-ratelimit-limit-tokens"] = "8000"
        headers["x-ratelimit-remaining-tokens"] = str(remaining_tokens)
    return _Resp(200, _content('{"depth": 4}', usage), headers=headers)


def _record_posts(monkeypatch, responses):
    """AsyncClient.post 대역. 보낸 payload를 순서대로 캡처한다. 응답 대신 예외를
    넣으면 그 예외를 던진다."""
    sent: list[dict] = []
    queue = list(responses)

    async def _post(_self, _url, headers=None, json=None, **_kwargs):
        sent.append(json)
        if not queue:
            raise AssertionError("mock 응답보다 많은 호출이 발생했습니다")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return sent


class _Clock:
    """time.monotonic 과 asyncio.sleep 을 한 손에 쥔다. sleep 은 시계를 전진시킨다."""

    def __init__(self, monkeypatch, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

        async def _sleep(seconds):
            self.sleeps.append(seconds)
            self.now += max(0.0, seconds)

        monkeypatch.setattr(time, "monotonic", lambda: self.now)
        monkeypatch.setattr(analyzer.asyncio, "sleep", _sleep)

    def advance(self, seconds: float) -> None:
        self.now += seconds


# 프롬프트 2,000자(비한글) = 근사 500토큰. 출력 EMA 초기 1000 -> 순수 필요 1,500,
# 초기 여유 20% -> 예약 1,800 토큰.
PROMPT = "x" * 2000
NEEDED = 1500.0
MARGIN = NEEDED * analyzer._GroqModelState.MARGIN_MIN_RATIO
RESERVED = NEEDED + MARGIN
REFILL_PER_SEC = 8000 / 60


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────────────────────────────
# 대기 계산
# ──────────────────────────────────────────────

def test_first_call_without_headers_does_not_wait(monkeypatch):
    """헤더를 한 번도 못 본 첫 호출은 기다리지 않는다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok()])

    _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == []


def test_enough_remaining_tokens_does_not_wait(monkeypatch):
    """헤더가 충분한 remaining 을 말하면 sleep 하지 않는다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=7000), _ok(remaining_tokens=5000)])

    _run(analyzer._call_groq(PROMPT))
    _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == []


def test_insufficient_remaining_waits_for_deficit_only(monkeypatch):
    """remaining 이 부족하면 (부족분 / 초당 충전량) 만큼만 기다린다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=500), _ok(remaining_tokens=7000)])

    _run(analyzer._call_groq(PROMPT))  # 헤더 관측: remaining 500
    _run(analyzer._call_groq(PROMPT))  # 예약 1,800 -> 부족 1,300

    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx((RESERVED - 500) / REFILL_PER_SEC, rel=1e-3)


def test_elapsed_time_refills_up_to_limit(monkeypatch):
    """경과 시간만큼 충전을 반영한다. 60초가 지나면 limit 까지 차서 기다리지 않는다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=0), _ok(remaining_tokens=7000)])

    _run(analyzer._call_groq(PROMPT))
    clock.advance(60)
    _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == []
    assert _state().remaining_tokens == 7000


def test_refill_is_capped_at_limit(monkeypatch):
    """충전은 limit(8,000)에서 멈춘다. 캡이 빠지면 오래 쉰 뒤 수만 토큰이 있다고 믿는다.

    헤더 없는 응답으로 예약 차감을 누적시켜 캡의 효과를 행동으로 확인한다:
    4번 x 1,800 = 7,200 을 쓰면 800 이 남아 5번째는 기다려야 한다.
    """
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=0)] + [_ok()] * 5)

    _run(analyzer._call_groq(PROMPT))
    clock.advance(600)  # 10분: 캡이 없다면 80,000 토큰
    for _ in range(4):
        _run(analyzer._call_groq(PROMPT))
    assert clock.sleeps == []
    assert _state().remaining_tokens == pytest.approx(8000 - 4 * RESERVED)

    _run(analyzer._call_groq(PROMPT))
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx((RESERVED - (8000 - 4 * RESERVED)) / REFILL_PER_SEC, rel=1e-3)


def test_partial_elapsed_time_reduces_wait(monkeypatch):
    """일부만 경과했으면 그만큼 덜 기다린다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=0), _ok(remaining_tokens=7000)])

    _run(analyzer._call_groq(PROMPT))
    clock.advance(3)  # 3초 x 133.3 = 400 토큰 충전
    _run(analyzer._call_groq(PROMPT))

    assert len(clock.sleeps) == 1
    expected = (RESERVED - 3 * REFILL_PER_SEC) / REFILL_PER_SEC
    assert clock.sleeps[0] == pytest.approx(expected, rel=1e-3)


def test_reset_header_means_full_after_reset(monkeypatch):
    """x-ratelimit-reset-tokens 가 지나면 limit 로 본다(선형 충전 모델보다 빠를 때).

    고정창 서버는 리셋 시각에 전부 돌려주므로 선형 모델(10초 x 133 = 1,333)로
    기다리면 이중 대기가 된다.
    """
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [
        _ok(remaining_tokens=0, **{"x-ratelimit-reset-tokens": "10s"}),
        _ok(remaining_tokens=7000),
    ])

    _run(analyzer._call_groq(PROMPT))
    clock.advance(10)
    _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == []


def test_reset_header_caps_wait_before_reset(monkeypatch):
    """리셋이 선형 부족분보다 먼저 오면 리셋까지만 기다린다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [
        _ok(remaining_tokens=0, **{"x-ratelimit-reset-tokens": "4s"}),
        _ok(remaining_tokens=7000),
    ])

    _run(analyzer._call_groq(PROMPT))
    _run(analyzer._call_groq(PROMPT))  # 선형이면 13.5초, 리셋은 4초

    assert clock.sleeps == [pytest.approx(4.0)]


def test_reservation_deducts_tokens_before_headers_arrive(monkeypatch):
    """예약 후 remaining 에서 예약분을 차감해 둔다. 헤더·usage 없는 200 은 소비된
    것이므로 차감이 그대로 남는다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=4000), _ok()])

    _run(analyzer._call_groq(PROMPT))
    _run(analyzer._call_groq(PROMPT))

    assert _state().remaining_tokens == pytest.approx(4000 - RESERVED)
    assert clock.sleeps == []


def test_usage_without_headers_settles_reservation_by_actual_total(monkeypatch):
    """헤더는 없지만 usage 가 있으면 예약분 대신 실제 total 로 정산한다."""
    _Clock(monkeypatch)
    _record_posts(monkeypatch, [
        _ok(remaining_tokens=4000),
        _ok(usage={"prompt_tokens": 600, "completion_tokens": 900, "total_tokens": 1500}),
    ])

    _run(analyzer._call_groq(PROMPT))
    _run(analyzer._call_groq(PROMPT))

    assert _state().remaining_tokens == pytest.approx(4000 - 1500)


def test_zero_remaining_requests_waits_until_reset(monkeypatch):
    """remaining_requests 가 0이면 reset_requests 까지 기다린다."""
    clock = _Clock(monkeypatch)
    first = _ok(
        remaining_tokens=7000,
        **{
            "x-ratelimit-limit-requests": "30",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "5s",
        },
    )
    _record_posts(monkeypatch, [first, _ok(remaining_tokens=7000)])

    _run(analyzer._call_groq(PROMPT))
    clock.advance(1)
    _run(analyzer._call_groq(PROMPT))

    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(4.0, rel=1e-3)


def test_broken_headers_are_ignored(monkeypatch):
    """헤더가 깨진 값이어도 예외 없이 진행한다."""
    clock = _Clock(monkeypatch)
    broken = _Resp(
        200,
        _content('{"depth": 4}', usage={"prompt_tokens": "많이", "completion_tokens": None}),
        headers={
            "x-ratelimit-limit-tokens": "unlimited",
            "x-ratelimit-remaining-tokens": "??",
            "x-ratelimit-reset-tokens": "soon",
            "x-ratelimit-remaining-requests": "",
        },
    )
    _record_posts(monkeypatch, [broken, _ok()])

    assert _run(analyzer._call_groq(PROMPT)) == {"depth": 4}
    assert _run(analyzer._call_groq(PROMPT)) == {"depth": 4}
    assert clock.sleeps == []
    assert _state().limit_tokens == 8000
    assert _state().remaining_tokens is None


# ──────────────────────────────────────────────
# 여유(margin)
# ──────────────────────────────────────────────

def test_margin_starts_at_min_ratio_and_grows_with_error(monkeypatch):
    """여유는 처음엔 needed 의 20%, 오차가 관측되면 오차 EMA 의 1.5배까지 커진다."""
    _Clock(monkeypatch)
    state = _state()
    assert state.margin_for(1500) == pytest.approx(300)

    # 실제 total 2500 vs 예약 needed 1500 -> 오차 1000 -> 여유 1500
    _record_posts(monkeypatch, [_ok(remaining_tokens=7000, usage={"prompt_tokens": 1500, "completion_tokens": 1000, "total_tokens": 2500})])
    _run(analyzer._call_groq(PROMPT))
    assert state.error_ema == pytest.approx(1000)
    needed_next = state.prompt_ratio_ema * 500 + state.completion_ema
    assert state.margin_for(needed_next) == pytest.approx(1500)


def test_margin_is_reserved_but_corrected_by_headers(monkeypatch):
    """여유는 대기 계산·차감에 들어가지만 헤더가 오면 실제 값으로 덮어써 남지 않는다."""
    _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=7000), _ok(remaining_tokens=3000)])

    _run(analyzer._call_groq(PROMPT))
    _run(analyzer._call_groq(PROMPT))

    assert _state().remaining_tokens == 3000


# ──────────────────────────────────────────────
# 429 대기 파싱과 재시도
# ──────────────────────────────────────────────

def test_retry_after_float_is_accepted(monkeypatch):
    """Retry-After "14.5" -> 예외 없이 15.5초 대기 후 재시도. 예전 int() 는 여기서 죽었다."""
    clock = _Clock(monkeypatch)
    limited = _Resp(
        429,
        {"error": {"message": "Rate limit reached for tokens", "type": "tokens"}},
        headers={"Retry-After": "14.5"},
    )
    sent = _record_posts(monkeypatch, [limited, _ok()])

    with structlog.testing.capture_logs() as logs:
        result = _run(analyzer._call_groq(PROMPT))

    assert result == {"depth": 4}
    assert len(sent) == 2
    assert clock.sleeps == [pytest.approx(15.5)]
    warn = next(e for e in logs if e["event"] == "groq_rate_limited")
    assert warn["limit_type"] == "tokens"
    assert warn["wait_seconds"] == pytest.approx(15.5)


def test_missing_retry_after_uses_reset_tokens_header(monkeypatch):
    """Retry-After 없고 x-ratelimit-reset-tokens "1m3.2s" -> 64.2초 대기."""
    clock = _Clock(monkeypatch)
    limited = _Resp(
        429,
        {"error": {"message": "Rate limit reached for tokens", "type": "tokens"}},
        headers={"x-ratelimit-reset-tokens": "1m3.2s", "x-ratelimit-reset-requests": "2s"},
    )
    _record_posts(monkeypatch, [limited, _ok()])

    _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == [pytest.approx(64.2)]


def test_requests_limit_type_uses_reset_requests_header(monkeypatch):
    """본문 type 이 requests 면 reset-requests 를 본다."""
    clock = _Clock(monkeypatch)
    limited = _Resp(
        429,
        {"error": {"message": "Rate limit reached for requests", "type": "requests"}},
        headers={"x-ratelimit-reset-tokens": "1m3.2s", "x-ratelimit-reset-requests": "2s"},
    )
    _record_posts(monkeypatch, [limited, _ok()])

    _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == [pytest.approx(3.0)]


def test_no_headers_at_all_falls_back_to_30_seconds(monkeypatch):
    """아무 헤더도 없으면 30 + 1초. 본문이 JSON 이 아니면 limit_type 은 본문 앞 120자."""
    clock = _Clock(monkeypatch)
    limited = _Resp(429, text="Too Many Requests")
    _record_posts(monkeypatch, [limited, _ok()])

    with structlog.testing.capture_logs() as logs:
        _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == [pytest.approx(31.0)]
    warn = next(e for e in logs if e["event"] == "groq_rate_limited")
    assert warn["limit_type"] == "Too Many Requests"


def test_429_exhausts_retries_and_raises(monkeypatch):
    """재시도 횟수를 다 쓰면 예외를 올린다(기존 동작 유지)."""
    _Clock(monkeypatch)
    limited = [_Resp(429, headers={"Retry-After": "1"}) for _ in range(4)]
    _record_posts(monkeypatch, limited)

    with pytest.raises(httpx.HTTPStatusError):
        _run(analyzer._call_groq(PROMPT))


def test_retry_after_429_skips_pacer_wait(monkeypatch):
    """429 헤더는 페이서에 반영되지만, Retry-After 를 다 기다린 재시도는 서버가
    수용을 약속한 것이므로 페이서가 또 기다리지 않는다(이중 대기 방지)."""
    clock = _Clock(monkeypatch)
    limited = _Resp(
        429,
        {"error": {"type": "tokens"}},
        headers={
            "Retry-After": "2",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "100",
        },
    )
    _record_posts(monkeypatch, [limited, _ok()])

    with structlog.testing.capture_logs() as logs:
        _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == [pytest.approx(3.0)]
    assert [e["event"] for e in logs if e["event"] == "groq_paced"] == []
    # 429 시점 100 + 3초 충전 400 - 예약 1,800 (헤더 없는 200 이라 차감 유지)
    assert _state().remaining_tokens == pytest.approx(100 + 3 * REFILL_PER_SEC - RESERVED)


def test_headerless_429_releases_reservation(monkeypatch):
    """헤더 없는 429 는 토큰을 소비하지 않았으므로 예약 차감을 되돌린다.

    되돌리지 않으면 재시도마다 차감이 쌓여 약 28초씩 과잉 대기한다.
    """
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [
        _ok(remaining_tokens=7000),
        _Resp(429, headers={"Retry-After": "2"}),
        _ok(),
    ])

    _run(analyzer._call_groq(PROMPT))
    _run(analyzer._call_groq(PROMPT))

    assert clock.sleeps == [pytest.approx(3.0)]
    # 7000 (되돌림) + 3초 충전 400 - 재시도 예약 1,800. 되돌리지 않았다면 3,800.
    assert _state().remaining_tokens == pytest.approx(7000 + 3 * REFILL_PER_SEC - RESERVED)


def test_transport_exception_releases_reservation(monkeypatch):
    """httpx 예외로 응답을 못 받으면 예약분을 되돌리고 예외는 그대로 올린다."""
    _Clock(monkeypatch)
    _record_posts(monkeypatch, [
        _ok(remaining_tokens=7000),
        httpx.ConnectError("connection reset"),
    ])

    _run(analyzer._call_groq(PROMPT))
    with pytest.raises(httpx.ConnectError):
        _run(analyzer._call_groq(PROMPT))

    assert _state().remaining_tokens == pytest.approx(7000)


def test_json_schema_fallback_reserves_second_request(monkeypatch):
    """400 강등 경로는 예약을 되돌리고 두 번째 요청을 다시 예약한다(회귀 방지)."""
    _Clock(monkeypatch)
    schema = {"name": "t", "strict": True, "schema": {"type": "object"}}
    sent = _record_posts(monkeypatch, [
        _ok(remaining_tokens=7000),
        _Resp(400, text="json_schema not supported"),
        _Resp(200, _content('{"top_indices": [2]}')),
    ])

    _run(analyzer._call_groq(PROMPT))
    result = _run(analyzer._call_groq(PROMPT, json_schema=schema))

    assert result == {"top_indices": [2]}
    assert sent[2]["response_format"] == {"type": "json_object"}
    assert _state().remaining_tokens == pytest.approx(7000 - RESERVED)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2.5s", 2.5),
        ("7.66s", 7.66),
        ("1m3.2s", 63.2),
        ("1h2m3s", 3723.0),
        ("1m", 60.0),
        ("500ms", 0.5),
        ("14.5", 14.5),
        ("", None),
        ("soon", None),
        (None, None),
    ],
)
def test_parse_duration(value, expected):
    assert analyzer._parse_duration(value) == (pytest.approx(expected) if expected is not None else None)


# ──────────────────────────────────────────────
# usage 계측과 보정비
# ──────────────────────────────────────────────

def test_usage_is_logged_and_feeds_estimate(monkeypatch):
    """성공 응답의 usage 가 groq_usage 로 기록되고 다음 추정에 보정비가 반영된다."""
    clock = _Clock(monkeypatch)
    usage = {"prompt_tokens": 1000, "completion_tokens": 1500, "total_tokens": 2500}
    _record_posts(monkeypatch, [_ok(remaining_tokens=7000, usage=usage), _ok(remaining_tokens=7000)])

    with structlog.testing.capture_logs() as logs:
        _run(analyzer._call_groq(PROMPT))

    entry = next(e for e in logs if e["event"] == "groq_usage")
    assert entry["prompt_tokens"] == 1000
    assert entry["completion_tokens"] == 1500
    assert entry["total_tokens"] == 2500
    assert entry["remaining_tokens"] == 7000
    assert entry["estimated_prompt_tokens"] == 500  # 보정 전(초기 보정비 1.0)
    assert entry["margin_tokens"] == MARGIN
    assert entry["model"] == _model()

    state = _state()
    # 관측 1000 / 근사 500 = 2.0 -> EMA(alpha 0.3): 1 + 0.3 x (2 - 1) = 1.3
    assert state.prompt_ratio_ema == pytest.approx(1.3)
    # 출력 EMA: 1000 + 0.3 x (1500 - 1000) = 1150
    assert state.completion_ema == pytest.approx(1150)
    assert analyzer._groq_pacer.estimate_prompt_tokens(PROMPT, _model()) == pytest.approx(650)

    # 두 번째 호출은 650 + 1150 + 여유(오차 1000 x 1.5) = 3,300 으로 7,000 에서 차감된다
    _run(analyzer._call_groq(PROMPT))
    assert clock.sleeps == []


def test_korean_counts_one_token_per_char():
    """한글 1자 = 1토큰, 그 외 4자 = 1토큰."""
    pacer = analyzer._GroqPacer()
    assert pacer.raw_prompt_tokens("가나다라") == 4
    assert pacer.raw_prompt_tokens("abcdefgh") == 2
    assert pacer.raw_prompt_tokens("가나ab") == 2.5


def test_state_is_per_model(monkeypatch):
    """Groq 한도는 모델 단위다. 한 모델이 바닥나도 다른 모델 호출은 기다리지 않는다."""
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=0), _ok(remaining_tokens=5000), _ok()])

    _run(analyzer._call_groq(PROMPT))                       # 기본 모델: remaining 0
    _run(analyzer._call_groq(PROMPT, model="other-model"))  # 다른 모델: 관측 없음 -> 대기 없음
    assert clock.sleeps == []
    assert analyzer._groq_pacer.state_for("other-model").remaining_tokens == 5000
    assert _state().remaining_tokens == 0

    _run(analyzer._call_groq(PROMPT))  # 기본 모델은 여전히 부족 -> 대기
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(RESERVED / REFILL_PER_SEC, rel=1e-3)


def test_lock_survives_multiple_event_loops(monkeypatch):
    """asyncio.run 을 여러 번 돌려도 Lock 이 옛 루프에 묶여 죽지 않는다."""
    _Clock(monkeypatch)
    _record_posts(monkeypatch, [_ok(remaining_tokens=7000)] * 3)
    for _ in range(3):
        _run(analyzer._call_groq(PROMPT))


def test_dry_run_never_reaches_groq(monkeypatch):
    """dry-run 은 _call_groq 에 도달하지 않으므로 페이서도 건드리지 않는다."""
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    clock = _Clock(monkeypatch)
    _record_posts(monkeypatch, [])  # 어떤 호출도 없어야 한다

    from app.models import RawContent, SourceType, UserProfile
    item = RawContent(
        source_type=SourceType.RSS, source_name="test", title="제목",
        url="https://example.com/a", body="본문",
    )
    result = _run(analyzer.analyze_content(item, UserProfile()))

    assert result.one_line_summary.startswith("[DRY RUN]")
    assert clock.sleeps == []
    assert not analyzer._groq_pacer.has_observation


# ──────────────────────────────────────────────
# 다중 호출 시나리오 (검증자 시뮬레이터 축약판)
# ──────────────────────────────────────────────
#
# 가짜 서버 두 종류로 31회를 연달아 부른다. 호출 사이에는 filter_and_analyze 의
# 3초 고정 대기를 흉내 낸다. usage 는 seed 고정 ±20% 흔들림.

LIMIT_TOK = 8000.0
LIMIT_REQ = 30.0
N_CALLS = 31
LOOP_DELAY = 3.0


def _fmt_duration(sec: float) -> str:
    sec = max(0.0, sec)
    if sec >= 60:
        minutes = int(sec // 60)
        return f"{minutes}m{sec - minutes * 60:.1f}s"
    return f"{sec:.1f}s"


class _BucketServer:
    """연속 충전 토큰버킷 8,000/60초, 요청 30/60초."""

    def __init__(self, clock: _Clock):
        self.clock = clock
        self.tok = LIMIT_TOK
        self.req = LIMIT_REQ
        self.last = clock.now

    def _refill(self):
        dt = self.clock.now - self.last
        self.tok = min(LIMIT_TOK, self.tok + LIMIT_TOK / 60 * dt)
        self.req = min(LIMIT_REQ, self.req + LIMIT_REQ / 60 * dt)
        self.last = self.clock.now

    def headers(self) -> dict:
        self._refill()
        return {
            "x-ratelimit-limit-tokens": str(int(LIMIT_TOK)),
            "x-ratelimit-remaining-tokens": str(int(self.tok)),
            "x-ratelimit-reset-tokens": _fmt_duration((LIMIT_TOK - self.tok) / (LIMIT_TOK / 60)),
            "x-ratelimit-limit-requests": str(int(LIMIT_REQ)),
            "x-ratelimit-remaining-requests": str(int(self.req)),
            "x-ratelimit-reset-requests": _fmt_duration((LIMIT_REQ - self.req) / (LIMIT_REQ / 60)),
        }

    def admit(self, cost: float):
        self._refill()
        if self.req < 1:
            return False, math.ceil((1 - self.req) / (LIMIT_REQ / 60)), "requests"
        if self.tok < cost:
            return False, math.ceil((cost - self.tok) / (LIMIT_TOK / 60)), "tokens"
        self.tok -= cost
        self.req -= 1
        return True, None, ""


class _WindowServer:
    """고정 60초 창. 창이 바뀌면 전부 리셋."""

    def __init__(self, clock: _Clock):
        self.clock = clock
        self.win = math.floor(clock.now / 60)
        self.used_tok = 0.0
        self.used_req = 0

    def _roll(self):
        w = math.floor(self.clock.now / 60)
        if w != self.win:
            self.win = w
            self.used_tok = 0.0
            self.used_req = 0

    def _reset_in(self) -> float:
        return (self.win + 1) * 60 - self.clock.now

    def headers(self) -> dict:
        self._roll()
        return {
            "x-ratelimit-limit-tokens": str(int(LIMIT_TOK)),
            "x-ratelimit-remaining-tokens": str(int(LIMIT_TOK - self.used_tok)),
            "x-ratelimit-reset-tokens": _fmt_duration(self._reset_in()),
            "x-ratelimit-limit-requests": str(int(LIMIT_REQ)),
            "x-ratelimit-remaining-requests": str(int(LIMIT_REQ - self.used_req)),
            "x-ratelimit-reset-requests": _fmt_duration(self._reset_in()),
        }

    def admit(self, cost: float):
        self._roll()
        if self.used_req + 1 > LIMIT_REQ:
            return False, math.ceil(self._reset_in()), "requests"
        if self.used_tok + cost > LIMIT_TOK:
            return False, math.ceil(self._reset_in()), "tokens"
        self.used_tok += cost
        self.used_req += 1
        return True, None, ""


def _sim_usages(seed: int = 7) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    return [
        (int(2800 * rng.uniform(0.8, 1.2)), int(1000 * rng.uniform(0.8, 1.2)))
        for _ in range(N_CALLS + 20)
    ]


def _sim_prompt() -> str:
    rng = random.Random(11)
    ko = "데이터사이언스 파이프라인에서 릴리스 감시와 요약 품질을 검증한다 "
    en = "the release watch pipeline validates summary quality across sources and versions "
    out: list[str] = []
    while sum(len(s) for s in out) < 8500:
        out.append(ko if rng.random() < 0.36 else en)
    return "".join(out)[:8500]


def _run_scenario(monkeypatch, server_cls, usages, latency: float = 0.0):
    """가짜 서버를 상대로 N_CALLS 회 호출하고 통계를 돌려준다."""
    clock = _Clock(monkeypatch)
    server = server_cls(clock)
    stats = {"429": 0, "idx": 0}

    async def _post(_self, _url, headers=None, json=None, **_kwargs):
        p, c = usages[stats["idx"]]
        ok, retry_after, limit_type = server.admit(p + c)
        if not ok:
            stats["429"] += 1
            hdr = server.headers()
            clock.advance(0.5)  # 429 왕복
            hdr["Retry-After"] = str(retry_after)
            return _Resp(429, {"error": {"message": f"Rate limit reached for {limit_type}", "type": limit_type}}, headers=hdr)
        clock.advance(latency)  # 생성 지연
        hdr = server.headers()
        stats["idx"] += 1
        return _Resp(200, _content('{"depth": 4}', {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}), headers=hdr)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    prompt = _sim_prompt()

    async def _loop():
        for i in range(N_CALLS):
            if i > 0:
                await analyzer.asyncio.sleep(LOOP_DELAY)
            await analyzer._call_groq(prompt)

    with structlog.testing.capture_logs() as logs:
        t0 = clock.now
        _run(_loop())
        total = clock.now - t0

    events = [e["event"] for e in logs if e["event"] in ("groq_paced", "groq_rate_limited", "groq_usage")]
    paced_after_429 = sum(
        1 for prev, cur in zip(events, events[1:]) if prev == "groq_rate_limited" and cur == "groq_paced"
    )
    tokens = sum(p + c for p, c in usages[:N_CALLS])
    bound = max(0.0, tokens - LIMIT_TOK) / (LIMIT_TOK / 60)
    return {"total": total, "429": stats["429"], "bound": bound, "paced_after_429": paced_after_429}


@pytest.mark.parametrize("seed", [7, 99, 123])
@pytest.mark.parametrize("latency", [0.0, 6.0])
def test_bucket_server_scenario_few_429_and_near_bound(monkeypatch, seed, latency):
    """토큰버킷 서버: 31회 호출에서 429 는 2회 이하, 총 소요는 이론 하한의 1.02배 이하.

    검증자 시뮬레이션에서 수정 전(여유 0)은 14회, 1차 페이서 이전 코드는 29회였다.
    총 소요는 TPM 이 정하므로 페이서는 429 만 없애고 시간을 늘리지 않아야 한다.
    """
    r = _run_scenario(monkeypatch, _BucketServer, _sim_usages(seed), latency)

    assert r["429"] <= 2, r
    assert r["total"] <= r["bound"] * 1.02 + latency * N_CALLS, r


def test_window_server_scenario_has_no_double_wait(monkeypatch):
    """고정창 서버: 429 의 Retry-After 를 기다린 재시도는 페이서가 또 기다리지 않는다.

    수정 전에는 "429 시점 remaining + 선형 충전" 으로 다시 계산해 최대 74초를 더
    기다렸다. 선형 충전 모델은 고정창에서 429 자체를 없애지는 못한다(수용).
    """
    r = _run_scenario(monkeypatch, _WindowServer, _sim_usages(7), latency=6.0)

    assert r["paced_after_429"] == 0, r
    # 창 모델에서도 총 소요가 429 만 쓰던 시절(약 929초)보다 늘지는 않아야 한다
    assert r["total"] <= 930, r
