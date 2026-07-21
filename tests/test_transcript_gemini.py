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

from app import collectors, transcript_gemini
from app.config import Settings
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
# 5. collectors 폴백 체인 통합
#    transcript_source는 평가 하니스의 계측 축이므로 라벨이 흔들리면 안 된다.
# ──────────────────────────────────────────────

class _FakeEntry(dict):
    """feedparser entry 대역 — .get()만 쓰이므로 dict로 충분하다."""


@pytest.fixture
def patch_chain(monkeypatch):
    """_resolve_transcript가 쓰는 외부 의존 3개(api·gemini·settings)를 갈아끼운다."""

    def _apply(*, api_transcript, gemini_transcript, api_key="k", dry_run=False, enabled=True):
        calls = {"gemini_called": False, "url": None, "duration": "UNSET"}

        async def _fake_gemini(url, duration_seconds):
            calls["gemini_called"] = True
            calls["url"] = url
            calls["duration"] = duration_seconds
            return gemini_transcript

        monkeypatch.setattr(collectors, "_get_transcript", lambda vid: api_transcript)
        monkeypatch.setattr(collectors, "fetch_transcript_via_gemini", _fake_gemini)
        monkeypatch.setattr(
            collectors,
            "get_settings",
            lambda: Settings(
                gemini_api_key=api_key,
                yt_gemini_transcript=enabled,
                dry_run=dry_run,
                _env_file=None,
            ),
        )
        return calls

    return _apply


def _resolve(entry=None, body="설명글"):
    return asyncio.run(
        collectors._resolve_transcript("VID123", entry or _FakeEntry(), body)
    )


def test_chain_prefers_transcript_api(patch_chain):
    """1단계가 성공하면 Gemini를 호출하지 않는다 — 타임스탬프가 있는 쪽이 우선."""
    calls = patch_chain(api_transcript="[00:01] API 자막", gemini_transcript="G")

    assert _resolve() == ("[00:01] API 자막", "api")
    assert calls["gemini_called"] is False


def test_chain_falls_back_to_gemini(patch_chain):
    """1단계 실패 시 Gemini로 넘어가고 source가 gemini로 기록된다."""
    calls = patch_chain(api_transcript=None, gemini_transcript="Gemini 전사")

    assert _resolve() == ("Gemini 전사", "gemini")
    # Gemini에는 공식 문서 예제 형식(watch?v=)을 넘긴다.
    assert calls["url"] == "https://www.youtube.com/watch?v=VID123"


def test_chain_falls_back_to_description(patch_chain):
    """둘 다 실패하고 설명글이 있으면 transcript=None + source=description."""
    patch_chain(api_transcript=None, gemini_transcript=None)

    assert _resolve(body="설명글") == (None, "description")


def test_chain_reports_none_when_nothing_available(patch_chain):
    """근거가 전혀 없으면 source=none — 계측에서 구분되어야 한다."""
    patch_chain(api_transcript=None, gemini_transcript=None)

    assert _resolve(body=None) == (None, "none")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": ""},        # GEMINI_API_KEY 없음
        {"dry_run": True},      # DRY_RUN
        {"enabled": False},     # 토글 off
    ],
)
def test_chain_skips_gemini_when_gated(patch_chain, kwargs):
    """API 키 없음 / DRY_RUN / 토글 off 셋 중 하나라도 걸리면 Gemini를 호출하지 않는다."""
    calls = patch_chain(api_transcript=None, gemini_transcript="G", **kwargs)

    assert _resolve(body="설명글") == (None, "description")
    assert calls["gemini_called"] is False


def test_duration_extracted_when_present(patch_chain):
    """피드가 duration을 주면 그대로 Gemini에 전달한다(사전 차단이 동작하도록)."""
    calls = patch_chain(api_transcript=None, gemini_transcript="G")
    entry = _FakeEntry(media_content=[{"duration": "300"}])

    _resolve(entry=entry)

    assert calls["duration"] == 300


def test_duration_is_none_for_real_youtube_feed(patch_chain):
    """
    YouTube videos.xml에는 duration이 없다 — 이때 None이 넘어가야 한다.

    None이면 사전 차단이 동작하지 않으며, 2시간+ 영상은 Gemini 쪽 400 후
    None으로 처리된다(모듈 docstring에 명시된 알려진 한계).
    """
    calls = patch_chain(api_transcript=None, gemini_transcript="G")

    _resolve(entry=_FakeEntry())

    assert calls["duration"] is None
    assert collectors._extract_yt_duration_seconds(_FakeEntry()) is None


def test_malformed_duration_does_not_raise(patch_chain):
    """duration이 파싱 불가한 값이어도 수집이 중단되면 안 된다."""
    patch_chain(api_transcript=None, gemini_transcript="G")
    entry = _FakeEntry(media_content=[{"duration": "not-a-number"}])

    assert collectors._extract_yt_duration_seconds(entry) is None
    assert _resolve(entry=entry) == ("G", "gemini")
