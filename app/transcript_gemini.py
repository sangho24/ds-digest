"""
Gemini API 기반 YouTube 자막(전사) 확보 모듈

배경:
  GitHub Actions/VPS의 datacenter IP가 YouTube에 차단되어 youtube-transcript-api가
  거의 항상 실패한다(실측: 98일간 YouTube 116건 중 자막 확보 1건 = 0.9%).
  PO Token·쿠키로도 해결되지 않는다 — bgutil 메인테이너가 "IP 평판이 축"이라고 명시.
  Gemini API에 YouTube URL을 직접 넘기면 Google 내부에서 영상을 처리하므로
  우리 쪽 IP 평판과 무관하게 전사를 얻을 수 있다.

구현 방침:
  analyzer.py와 동일하게 httpx로 REST를 직접 호출한다.
  google-generativeai SDK는 Python 3.14 protobuf C extension 충돌로 사용 불가.

요청 스키마 근거 (공식 문서 확인):
  - Part.videoMetadata 는 Part의 필드이며 fileData/inlineData와 형제 관계다.
    VideoMetadata = {startOffset, endOffset, fps}, fps 범위 (0.0, 24.0], 기본값 1.0.
    https://ai.google.dev/api/caching#VideoMetadata
  - FileData = {mime_type, file_uri}. YouTube URL은 file_uri에 그대로 넣는다.
    https://ai.google.dev/api/generate-content
  - YouTube URL 입력 자체의 개요: https://ai.google.dev/gemini-api/docs/video-understanding
  ※ 위 레퍼런스는 VideoMetadata를 deprecated로 표기하고
    GenerateContentRequest.processing_options 사용을 권고하지만,
    별도 세션이 실제 호출로 검증한 것은 video_metadata.fps 경로이므로 이쪽을 쓴다.
"""
import asyncio
import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger()

_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# 프레임 샘플링 비율.
# 자막은 오디오 트랙에서 나오므로 프레임을 적게 봐도 전사 정확도가 떨어지지 않는다.
# 실측(20분 영상): 기본값(fps=1.0) 330,428 토큰 → fps=0.1 적용 시 65,312 토큰.
# 5.1배 감소하면서 전사 품질은 동일했다(youtube-transcript-api 결과와 문자 단위 일치).
TRANSCRIPT_FPS = 0.1

# duration 사전 차단 임계값(초).
# 2시간 이상 영상은 fps=0.1을 적용해도 1M 토큰 컨텍스트를 초과해 400 에러로 실패한다.
# 100분은 그 앞에 둔 보수적 경계로, 무의미한 실패 호출과 토큰 비용을 사전에 막는다.
MAX_VIDEO_DURATION_SECONDS = 100 * 60

# 영상 1건 처리에 20~64초가 걸린다(실측). 긴 영상은 더 걸릴 수 있으므로 여유를 둔다.
_REQUEST_TIMEOUT = 180

# 429 응답에 RetryInfo가 없을 때 사용할 기본 대기(초) — analyzer._call_gemini와 동일 규약.
_DEFAULT_RETRY_DELAY = 30

# 전사가 아니라 "생성"으로 넘어간 응답을 걸러내기 위한 finishReason 목록.
_BLOCKED_FINISH_REASONS = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}

# 요약이 아니라 축어적 전사를 요구한다. 한국어 콘텐츠를 영어로 번역해버리면
# 원문 고유명사·수치가 소실되므로 반드시 원어 그대로 받아야 한다.
TRANSCRIPT_PROMPT = """\
이 영상에서 말하는 내용을 처음부터 끝까지 그대로 받아쓰세요.

규칙:
- 요약하지 마세요. 설명하지 마세요. 논평하지 마세요.
- 말한 문장을 빠짐없이 그대로 옮겨 적으세요.
- 영상에서 사용된 언어 그대로 전사하세요. 한국어로 말하면 한국어로,
  영어로 말하면 영어로 적으세요. 번역하지 마세요.
- 화면에 보이는 자막이나 슬라이드 텍스트가 아니라 실제로 말한 음성을 기준으로 하세요.
- "다음은 전사입니다" 같은 머리말이나 맺음말을 붙이지 말고 전사 본문만 출력하세요.
"""


def _parse_retry_delay(resp: httpx.Response) -> int:
    """429 응답 body의 RetryInfo에서 재시도 대기 초를 읽는다.

    analyzer._call_gemini와 동일한 파싱 규약 — 파싱 실패 시 기본값으로 폴백한다.
    """
    try:
        details = resp.json().get("error", {}).get("details", [])
        for d in details:
            if d.get("@type", "").endswith("RetryInfo"):
                delay_str = d.get("retryDelay", f"{_DEFAULT_RETRY_DELAY}s")
                # 여유 2초는 서버 시계와의 오차를 흡수하기 위한 것(analyzer와 동일).
                return int(float(delay_str.rstrip("s"))) + 2
    except Exception:
        pass
    return _DEFAULT_RETRY_DELAY


def _build_payload(video_url: str) -> dict:
    """generateContent 요청 body를 만든다.

    video_metadata는 file_data와 같은 part 안에 나란히 들어간다(형제 필드).
    proto JSON은 snake_case/camelCase를 모두 받으므로 공식 curl 예제 표기를 따랐다.

    generationConfig는 의도적으로 비워둔다. 실제 호출로 검증된 구성이
    "text part + file_data + video_metadata.fps" 조합이므로, 검증되지 않은
    파라미터(thinkingConfig·maxOutputTokens·mediaResolution 등)를 추가해
    400 위험을 만들지 않는다. 출력 길이는 모델 기본 상한을 쓰고,
    잘렸는지는 finishReason=MAX_TOKENS로 감지해 경고를 남긴다.
    """
    return {
        "contents": [
            {
                "parts": [
                    {"text": TRANSCRIPT_PROMPT},
                    {
                        # YouTube URL은 mime_type 없이 file_uri만 넘긴다
                        # (공식 YouTube 예제가 file_uri만 지정한다).
                        "file_data": {"file_uri": video_url},
                        "video_metadata": {"fps": TRANSCRIPT_FPS},
                    },
                ]
            }
        ],
    }


def _extract_text(result: dict, video_url: str) -> str | None:
    """응답에서 전사 텍스트를 꺼낸다. 실패 사유는 반드시 로그로 남긴다(조용한 실패 금지)."""
    # 프롬프트 단계에서 차단되면 candidates 자체가 없고 promptFeedback만 온다.
    candidates = result.get("candidates") or []
    if not candidates:
        block_reason = (result.get("promptFeedback") or {}).get("blockReason")
        logger.warning(
            "gemini_transcript_no_candidates",
            video_url=video_url,
            block_reason=block_reason,
        )
        return None

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    if finish_reason in _BLOCKED_FINISH_REASONS:
        logger.warning(
            "gemini_transcript_blocked",
            video_url=video_url,
            finish_reason=finish_reason,
        )
        return None

    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()

    if not text:
        logger.warning(
            "gemini_transcript_empty",
            video_url=video_url,
            finish_reason=finish_reason,
        )
        return None

    # 출력 토큰 상한에 걸려 잘린 경우. 잘린 전사도 설명글보다는 근거로서 낫기 때문에
    # 버리지 않고 반환하되, 계측할 수 있도록 경고를 남긴다.
    if finish_reason == "MAX_TOKENS":
        logger.warning(
            "gemini_transcript_truncated",
            video_url=video_url,
            chars=len(text),
        )

    return text


async def fetch_transcript_via_gemini(
    video_url: str,
    duration_seconds: int | None = None,
) -> str | None:
    """Gemini API에 YouTube URL을 직접 넘겨 영상 전사를 확보한다.

    youtube-transcript-api가 IP 차단으로 실패할 때의 폴백 경로다.
    Google 내부에서 영상을 처리하므로 호출자 IP 평판의 영향을 받지 않는다.

    ⚠️ 타임스탬프 없음 — 이 경로로 얻는 것은 평문 전사뿐이다.
    youtube-transcript-api와 달리 [MM:SS] 구간 정보를 얻을 수 없으므로,
    이 결과로 key_point의 timestamp를 만들면 안 된다.

    Args:
        video_url: YouTube 영상 URL(공개 영상만 가능. 비공개·미등록 영상은 실패).
        duration_seconds: 영상 길이(초). None이면 사전 차단이 동작하지 않으며
            2시간 이상 영상이면 API가 1M 토큰 초과로 400을 반환할 수 있다.

    Returns:
        전사 텍스트. 확보 실패 시 None(예외를 던지지 않는다).
    """
    settings = get_settings()

    if not settings.gemini_api_key:
        logger.warning("gemini_transcript_no_api_key", video_url=video_url)
        return None

    # duration 사전 차단: 실패가 확실한 호출은 아예 보내지 않는다.
    # 목적은 무의미한 실패 대기와 토큰 비용을 막는 것이다.
    max_duration = settings.yt_gemini_max_duration_seconds or MAX_VIDEO_DURATION_SECONDS
    if duration_seconds is not None and duration_seconds > max_duration:
        logger.warning(
            "gemini_transcript_too_long",
            video_url=video_url,
            duration_seconds=duration_seconds,
            max_duration_seconds=max_duration,
        )
        return None

    url = _GEMINI_API_URL.format(model=settings.gemini_model)
    payload = _build_payload(video_url)
    retry = 3

    try:
        for attempt in range(retry + 1):
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    url, params={"key": settings.gemini_api_key}, json=payload
                )

            if resp.status_code == 429:
                if attempt < retry:
                    wait_seconds = _parse_retry_delay(resp)
                    logger.warning(
                        "gemini_transcript_rate_limited",
                        video_url=video_url,
                        attempt=attempt + 1,
                        wait_seconds=wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                # 재시도를 다 써도 429면 전사를 포기한다. 자막은 파이프라인의
                # 부가 근거이므로 예외로 파이프라인 전체를 세우지 않는다.
                logger.warning(
                    "gemini_transcript_rate_limit_exhausted",
                    video_url=video_url,
                    attempts=attempt + 1,
                )
                return None

            resp.raise_for_status()
            result = resp.json()
            break
        else:
            # for가 break 없이 끝나는 경로(이론상 도달 불가)를 방어한다.
            logger.warning("gemini_transcript_no_response", video_url=video_url)
            return None

    except Exception as e:
        # 400(토큰 초과·비공개 영상)·타임아웃·네트워크 오류 등 모두 여기로 온다.
        # 호출자는 description 폴백으로 넘어가면 되므로 예외를 전파하지 않는다.
        logger.warning(
            "gemini_transcript_failed",
            video_url=video_url,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None

    return _extract_text(result, video_url)
