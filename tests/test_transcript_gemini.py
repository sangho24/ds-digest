"""
Gemini 자막 확보 모듈 테스트

실제 Gemini API는 절대 호출하지 않는다 — httpx.AsyncClient.post를 mock한다.
검증 대상은 "실패해도 예외를 던지지 않는가"와 "요청 페이로드가 의도대로인가" 두 축이다.

이 저장소에는 pytest-asyncio가 없으므로 기존 테스트(test_evidence_gate 등)와 동일하게
동기 테스트 함수 안에서 asyncio.run()으로 코루틴을 돌린다.

실행: pytest tests/test_transcript_gemini.py -v
"""
import asyncio

import httpx
import pytest

from app import analyzer, transcript_gemini
from app.config import Settings
from app.models import RawContent, SourceType, UserProfile
from app.transcript_gemini import (
    MAX_VIDEO_DURATION_SECONDS,
    TRANSCRIPT_FPS,
    fetch_transcript_via_gemini,
)

VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ──────────────────────────────────────────────
# 테스트 하네스
# ──────────────────────────────────────────────

class _FakeResponse:
    """httpx.Response 중 이 모듈이 실제로 쓰는 부분만 흉내낸다."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


class _PostRecorder:
    """AsyncClient.post 대역. 호출 횟수와 요청 페이로드를 캡처한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, url, params=None, json=None, **kwargs):
        self.calls.append({"url": url, "params": params, "json": json})
        if not self.responses:
            raise AssertionError("mock 응답보다 많은 호출이 발생했습니다")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)


async def _noop_sleep(_seconds):
    """재시도 대기가 테스트를 실제로 지연시키지 않도록 한다."""
    return None


@pytest.fixture
def patch_gemini(monkeypatch):
    """설정과 httpx.post를 한꺼번에 갈아끼우고 recorder를 돌려준다."""

    def _apply(responses, *, api_key="test-key", max_duration=0):
        settings = Settings(
            gemini_api_key=api_key,
            gemini_model="gemini-2.5-flash",
            yt_gemini_max_duration_seconds=max_duration,
            _env_file=None,
        )
        monkeypatch.setattr(transcript_gemini, "get_settings", lambda: settings)

        recorder = _PostRecorder(responses)
        monkeypatch.setattr(httpx.AsyncClient, "post", recorder)
        monkeypatch.setattr(transcript_gemini.asyncio, "sleep", _noop_sleep)
        return recorder

    return _apply


def _fetch(*args, **kwargs):
    """코루틴을 동기 컨텍스트에서 실행하는 헬퍼."""
    return asyncio.run(fetch_transcript_via_gemini(*args, **kwargs))


def _ok_response(text: str, finish_reason: str = "STOP") -> _FakeResponse:
    return _FakeResponse(
        200,
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": text}]},
                    "finishReason": finish_reason,
                }
            ]
        },
    )


def _rate_limited_response(delay: str = "5s") -> _FakeResponse:
    return _FakeResponse(
        429,
        {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": delay,
                    }
                ]
            }
        },
    )


# ──────────────────────────────────────────────
# 1. duration 사전 차단
# ──────────────────────────────────────────────

def test_over_duration_does_not_call_api(patch_gemini):
    """
    임계값 초과 영상은 API 호출 자체가 일어나면 안 된다.

    2시간+ 영상은 1M 토큰 초과로 400이 확정이므로, 호출하면 응답 대기와
    토큰 비용만 낭비된다. 사전 차단의 존재 이유가 이것이다.
    """
    recorder = patch_gemini([], max_duration=MAX_VIDEO_DURATION_SECONDS)

    result = _fetch(VIDEO_URL, duration_seconds=MAX_VIDEO_DURATION_SECONDS + 1)

    assert result is None
    assert recorder.call_count == 0


def test_under_duration_calls_api(patch_gemini):
    """임계값 이하 영상은 정상 호출된다(사전 차단이 과잉 동작하지 않는지)."""
    recorder = patch_gemini(
        [_ok_response("전사 내용")], max_duration=MAX_VIDEO_DURATION_SECONDS
    )

    result = _fetch(VIDEO_URL, duration_seconds=MAX_VIDEO_DURATION_SECONDS - 1)

    assert result == "전사 내용"
    assert recorder.call_count == 1


def test_unknown_duration_still_calls_api(patch_gemini):
    """duration을 모르면(None) 차단하지 않고 호출한다 — RSS에 duration이 없는 것이 일반적."""
    recorder = patch_gemini([_ok_response("전사 내용")])

    result = _fetch(VIDEO_URL, duration_seconds=None)

    assert result == "전사 내용"
    assert recorder.call_count == 1


# ──────────────────────────────────────────────
# 2. 실패 경로 — 전부 None, 예외 없음
# ──────────────────────────────────────────────

def test_missing_api_key_returns_none(patch_gemini):
    """API 키가 없으면 호출하지 않고 None."""
    recorder = patch_gemini([], api_key="")

    result = _fetch(VIDEO_URL)

    assert result is None
    assert recorder.call_count == 0


def test_empty_text_returns_none(patch_gemini):
    """전사 텍스트가 공백뿐이면 None (빈 문자열을 근거로 승격시키면 안 된다)."""
    patch_gemini([_ok_response("   \n  ")])

    assert _fetch(VIDEO_URL) is None


def test_no_candidates_returns_none(patch_gemini):
    """프롬프트 단계 차단 시 candidates가 없고 promptFeedback만 온다."""
    patch_gemini([_FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}})])

    assert _fetch(VIDEO_URL) is None


def test_safety_finish_reason_returns_none(patch_gemini):
    """finishReason=SAFETY면 부분 텍스트가 있어도 신뢰할 수 없으므로 None."""
    patch_gemini([_ok_response("일부 텍스트", finish_reason="SAFETY")])

    assert _fetch(VIDEO_URL) is None


def test_http_error_returns_none(patch_gemini):
    """400(토큰 초과·비공개 영상 등)에도 예외가 아니라 None을 돌려준다."""
    patch_gemini([_FakeResponse(400, {"error": {"message": "too long"}})])

    assert _fetch(VIDEO_URL) is None


def test_network_error_returns_none(patch_gemini):
    """타임아웃 등 네트워크 예외도 삼키고 None — 폴백 체인이 계속되어야 한다."""
    patch_gemini([httpx.ReadTimeout("timeout")])

    assert _fetch(VIDEO_URL) is None


# ──────────────────────────────────────────────
# 3. 429 재시도
# ──────────────────────────────────────────────

def test_rate_limit_then_success(patch_gemini):
    """429 후 재시도해서 성공하면 전사를 반환한다."""
    recorder = patch_gemini([_rate_limited_response(), _ok_response("재시도 후 전사")])

    result = _fetch(VIDEO_URL)

    assert result == "재시도 후 전사"
    assert recorder.call_count == 2


def test_rate_limit_exhausted_returns_none_not_raise(patch_gemini):
    """
    재시도를 모두 소진해도 예외를 던지지 않고 None을 반환해야 한다.

    자막은 파이프라인의 부가 근거이므로, 여기서 예외가 나면 해당 채널의
    수집 루프 전체가 중단되어 다른 영상까지 잃는다.
    """
    recorder = patch_gemini([_rate_limited_response()] * 4)

    result = _fetch(VIDEO_URL)

    assert result is None
    assert recorder.call_count == 4  # 최초 1회 + 재시도 3회


def test_retry_delay_is_parsed_from_retry_info(patch_gemini, monkeypatch):
    """RetryInfo.retryDelay를 읽어 그만큼(+2초) 대기하는지."""
    waits: list[float] = []

    async def _capture_sleep(seconds):
        waits.append(seconds)

    patch_gemini([_rate_limited_response("7s"), _ok_response("전사")])
    monkeypatch.setattr(transcript_gemini.asyncio, "sleep", _capture_sleep)

    assert _fetch(VIDEO_URL) == "전사"
    assert waits == [9]  # 7s + 오차 흡수 2초


# ──────────────────────────────────────────────
# 4. 정상 응답 파싱 + 요청 페이로드 검증
# ──────────────────────────────────────────────

def test_multi_part_text_is_joined(patch_gemini):
    """응답이 여러 part로 쪼개져 와도 하나의 전사로 이어붙인다."""
    patch_gemini([
        _FakeResponse(200, {
            "candidates": [{
                "content": {"parts": [{"text": "앞부분 "}, {"text": "뒷부분"}]},
                "finishReason": "STOP",
            }]
        })
    ])

    assert _fetch(VIDEO_URL) == "앞부분 뒷부분"


def test_truncated_response_is_still_returned(patch_gemini):
    """MAX_TOKENS로 잘린 전사도 설명글보다는 나은 근거이므로 버리지 않는다."""
    patch_gemini([_ok_response("잘린 전사", finish_reason="MAX_TOKENS")])

    assert _fetch(VIDEO_URL) == "잘린 전사"


def test_payload_contains_fps_and_video_url(patch_gemini):
    """
    요청 페이로드에 fps=0.1과 영상 URL이 정확한 위치에 들어가는지 검증한다.

    fps가 빠지면 20분 영상 기준 토큰이 5.1배(65k → 330k)로 뛰므로,
    조용히 누락되면 비용만 늘고 품질 이득은 없다.
    video_metadata는 file_data와 같은 part 안의 형제 필드여야 한다.
    """
    recorder = patch_gemini([_ok_response("전사")])

    _fetch(VIDEO_URL)

    payload = recorder.calls[0]["json"]
    parts = payload["contents"][0]["parts"]

    # 텍스트 프롬프트 part가 있어야 한다
    assert any("text" in p for p in parts)

    video_parts = [p for p in parts if "file_data" in p]
    assert len(video_parts) == 1
    video_part = video_parts[0]

    assert video_part["file_data"]["file_uri"] == VIDEO_URL
    assert video_part["video_metadata"]["fps"] == TRANSCRIPT_FPS
    assert TRANSCRIPT_FPS == 0.1


def test_prompt_requires_verbatim_original_language(patch_gemini):
    """프롬프트가 요약 금지 + 원어 유지를 명시하는지(한국어 영상 번역 방지)."""
    recorder = patch_gemini([_ok_response("전사")])

    _fetch(VIDEO_URL)

    text_part = recorder.calls[0]["json"]["contents"][0]["parts"][0]["text"]
    assert "요약하지" in text_part
    assert "번역하지" in text_part


def test_api_key_passed_as_query_param(patch_gemini):
    """analyzer.py와 동일하게 API 키는 쿼리 파라미터로 전달한다."""
    recorder = patch_gemini([_ok_response("전사")])

    _fetch(VIDEO_URL)

    assert recorder.calls[0]["params"] == {"key": "test-key"}
    assert "gemini-2.5-flash" in recorder.calls[0]["url"]


# ──────────────────────────────────────────────
# 5. Stage 1 메타 랭킹 → Stage 2 전사 (analyzer.resolve_youtube_transcripts)
#    구버전 collectors._resolve_transcript 체인을 대체한다. 전사는 이제 수집이
#    아니라 dedup·캡 이후 상위 N건에만 붙으므로, 여기서는 게이트·선택·폴백을 검증한다.
#    transcript_source는 평가 하니스의 계측 축이므로 선택 규칙이 흔들리면 안 된다.
# ──────────────────────────────────────────────

def _yt_item(idx: int, *, body: str | None = "설명글") -> RawContent:
    """수집기가 만드는 형식(url=https://youtu.be/{video_id})의 YouTube 아이템."""
    return RawContent(
        source_type=SourceType.YOUTUBE,
        source_name=f"채널{idx}",
        title=f"영상 {idx}",
        url=f"https://youtu.be/VID{idx}",
        body=body,
    )


@pytest.fixture
def patch_resolve(monkeypatch):
    """resolve_youtube_transcripts의 외부 의존을 갈아끼운다.

    - get_settings: 게이트(dry_run / gemini_api_key / yt_gemini_transcript) 제어
    - fetch_transcript_via_gemini: 전사 대역(호출된 watch_url을 순서대로 기록)
    - _call_llm_with_fallback: 메타 랭킹 대역(호출 여부·반환값·예외 제어)
    - asyncio.sleep: 페이싱 대기 제거(테스트가 실제로 멈추지 않도록)
    """

    def _apply(*, api_key="k", dry_run=False, enabled=True, ranking_result=None, ranking_error=None):
        state = {"gemini_urls": [], "ranking_called": False, "ranking_groq_model": None}

        settings = Settings(
            gemini_api_key=api_key,
            yt_gemini_transcript=enabled,
            dry_run=dry_run,
            _env_file=None,
        )
        monkeypatch.setattr(analyzer, "get_settings", lambda: settings)

        async def _fake_transcript(url, duration_seconds):
            state["gemini_urls"].append(url)
            return f"전사::{url}"

        monkeypatch.setattr(analyzer, "fetch_transcript_via_gemini", _fake_transcript)

        async def _fake_ranking(prompt, title, groq_model=None):
            state["ranking_called"] = True
            state["ranking_groq_model"] = groq_model
            if ranking_error is not None:
                raise ranking_error
            return ranking_result

        monkeypatch.setattr(analyzer, "_call_llm_with_fallback", _fake_ranking)
        monkeypatch.setattr(analyzer.asyncio, "sleep", _noop_sleep)
        return state

    return _apply


def _resolve_yt(items, budget):
    """코루틴을 동기 컨텍스트에서 실행하는 헬퍼."""
    return asyncio.run(analyzer.resolve_youtube_transcripts(items, UserProfile(), budget))


@pytest.mark.parametrize(
    "kwargs, budget",
    [
        ({"dry_run": True}, 5),   # DRY_RUN
        ({"api_key": ""}, 5),     # GEMINI_API_KEY 없음
        ({"enabled": False}, 5),  # 토글 off
        ({}, 0),                  # budget <= 0
    ],
)
def test_resolve_gate_skips_transcription(patch_resolve, kwargs, budget):
    """게이트 조건 중 하나라도 걸리면 전사·랭킹 호출 없이 items를 그대로 돌려준다."""
    state = patch_resolve(**kwargs)
    items = [_yt_item(i) for i in range(3)]

    result = _resolve_yt(items, budget)

    assert result is items
    assert state["gemini_urls"] == []
    assert state["ranking_called"] is False
    assert all(item.transcript is None for item in result)


def test_resolve_empty_items_returns_empty(patch_resolve):
    """아이템이 없으면 아무 것도 하지 않는다."""
    state = patch_resolve()

    result = _resolve_yt([], 5)

    assert result == []
    assert state["gemini_urls"] == []
    assert state["ranking_called"] is False


def test_resolve_within_budget_skips_ranking_transcribes_all(patch_resolve):
    """items 수 <= budget이면 랭킹 LLM을 호출하지 않고 전부 전사한다."""
    state = patch_resolve()
    items = [_yt_item(i) for i in range(3)]

    result = _resolve_yt(items, budget=5)

    assert state["ranking_called"] is False
    assert all(item.transcript is not None for item in result)
    # 수집기가 저장한 youtu.be URL에서 video_id를 복원해 watch?v= 형식으로 넘긴다.
    assert state["gemini_urls"] == [
        "https://www.youtube.com/watch?v=VID0",
        "https://www.youtube.com/watch?v=VID1",
        "https://www.youtube.com/watch?v=VID2",
    ]


def test_resolve_over_budget_ranks_and_transcribes_selected(patch_resolve):
    """items 수 > budget이면 랭킹 LLM이 고른 정확히 budget개만 전사한다."""
    state = patch_resolve(ranking_result={"top_indices": [1, 3]})
    items = [_yt_item(i) for i in range(5)]

    result = _resolve_yt(items, budget=2)

    assert state["ranking_called"] is True
    assert result is items  # 순서 보존, 전체 리스트 반환
    # 선택된 1, 3만 전사되고 나머지는 None 유지
    assert result[1].transcript is not None
    assert result[3].transcript is not None
    assert result[0].transcript is None
    assert result[2].transcript is None
    assert result[4].transcript is None
    assert state["gemini_urls"] == [
        "https://www.youtube.com/watch?v=VID1",
        "https://www.youtube.com/watch?v=VID3",
    ]


def test_resolve_ranking_uses_llama_model(patch_resolve):
    """랭킹 호출은 gpt-oss가 아니라 groq_ranking_model(llama)로 나가야 한다.

    gpt-oss 계열은 정수 인덱스 배열 스키마에서 범위 밖 인덱스·숫자 이어붙임을
    내므로(실측), 랭킹만 llama로 오버라이드한다. 분석은 groq_model 그대로다.
    """
    state = patch_resolve(ranking_result={"top_indices": [1, 3]})
    items = [_yt_item(i) for i in range(5)]

    _resolve_yt(items, budget=2)

    assert state["ranking_called"] is True
    assert state["ranking_groq_model"] == "llama-3.3-70b-versatile"


def test_resolve_ranking_indices_sanitized(patch_resolve):
    """범위 밖·중복 인덱스는 제거하고 유효한 것만 등장 순서대로 최대 budget개 쓴다."""
    state = patch_resolve(ranking_result={"top_indices": [99, 1, 1, 0]})
    items = [_yt_item(i) for i in range(5)]

    result = _resolve_yt(items, budget=2)

    # 99(범위밖)·중복 1 제거 → [1, 0] 순서로 선택
    assert result[1].transcript is not None
    assert result[0].transcript is not None
    assert result[2].transcript is None
    assert state["gemini_urls"] == [
        "https://www.youtube.com/watch?v=VID1",
        "https://www.youtube.com/watch?v=VID0",
    ]


def test_resolve_ranking_exception_falls_back_to_first_n(patch_resolve):
    """랭킹 LLM이 예외를 던지면 앞 budget건으로 폴백하고 예외를 전파하지 않는다."""
    state = patch_resolve(ranking_error=RuntimeError("LLM down"))
    items = [_yt_item(i) for i in range(5)]

    result = _resolve_yt(items, budget=2)

    assert state["ranking_called"] is True
    assert result[0].transcript is not None
    assert result[1].transcript is not None
    assert all(item.transcript is None for item in result[2:])
    assert state["gemini_urls"] == [
        "https://www.youtube.com/watch?v=VID0",
        "https://www.youtube.com/watch?v=VID1",
    ]


def test_resolve_ranking_bad_json_falls_back_to_first_n(patch_resolve):
    """랭킹 LLM이 엉뚱한 JSON을 주면(top_indices 없음) 앞 budget건으로 폴백한다."""
    state = patch_resolve(ranking_result={"unexpected": "shape"})
    items = [_yt_item(i) for i in range(5)]

    result = _resolve_yt(items, budget=2)

    assert state["ranking_called"] is True
    assert result[0].transcript is not None
    assert result[1].transcript is not None
    assert all(item.transcript is None for item in result[2:])


def test_resolve_transcript_failure_keeps_none(patch_resolve, monkeypatch):
    """전사가 None을 반환하면(확보 실패) 해당 아이템은 transcript=None을 유지한다."""
    state = patch_resolve()

    async def _fail_transcript(url, duration_seconds):
        state["gemini_urls"].append(url)
        return None

    monkeypatch.setattr(analyzer, "fetch_transcript_via_gemini", _fail_transcript)
    items = [_yt_item(i) for i in range(2)]

    result = _resolve_yt(items, budget=5)

    assert all(item.transcript is None for item in result)
    assert state["gemini_urls"] == [
        "https://www.youtube.com/watch?v=VID0",
        "https://www.youtube.com/watch?v=VID1",
    ]
