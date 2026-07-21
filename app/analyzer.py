"""
Gemini 기반 콘텐츠 분석기
httpx로 Gemini REST API 직접 호출 — google-generativeai SDK 불필요
(Python 3.14에서 SDK의 protobuf C extension 충돌 회피)
"""
import asyncio
import json
import re
import structlog
import httpx
from urllib.parse import urlparse, parse_qs

from app.config import get_settings
from app.models import (
    RawContent, ContentAnalysis, KeyPoint, QuizItem, SourceType,
    DigestItem, UserProfile, EvidenceLevel, EVIDENCE_DEPTH_CAPS,
)
from app.transcript_gemini import fetch_transcript_via_gemini

logger = structlog.get_logger()

# Gemini free tier: gemini-2.5-flash 약 10 RPM → 호출 간 최소 8초
# Groq free tier: 30 RPM → 호출 간 최소 3초
_RATE_LIMIT_DELAY = 8.0
_GROQ_RATE_LIMIT_DELAY = 3.0
# Stage 2 전사 호출 사이 대기(초). 무료 티어 TPM 보호용 — 첫 호출 앞에는 두지 않는다.
_TRANSCRIPT_PACING_DELAY = 5.0
_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# 1,000자는 제목·리드문을 넘어 메커니즘과 맥락을 판단할 최소 분량이다.
# 수집기가 본문을 최대 5,000자, 분석기가 4,000자까지 사용하므로 전문 판정에
# 충분하면서도 짧은 초록·요약을 PARTIAL로 분리할 수 있는 보수적 경계다.
FULL_TRANSCRIPT_MIN_CHARS = 1_000
FULL_BODY_MIN_CHARS = 1_000

# 다이제스트의 목적상 즉시 활용 가능성을 조금 더 중시한다. 두 축의 가중합은
# 기존 필터 계약을 유지하기 위해 0~10 정수 relevance_score로 반올림한다.
ACTIONABILITY_WEIGHT = 0.6
DEPTH_WEIGHT = 0.4

ANALYSIS_PROMPT = """\
당신은 Data Science 현업 팀의 시니어 DS입니다.
아래 콘텐츠를 분석해서 JSON으로 응답하세요.

## 사용자가 명시적으로 요청한 키워드
- 최근 요청 키워드: {keywords}

## 콘텐츠
- 제목: {title}
- 출처: {source_name} ({source_type})
- 본문/자막:
{content}

## 분석 기준

1. one_line_summary: 한 줄 요약 (한국어, 30자 이내)
   - 제목을 그대로 옮기거나 paraphrase하지 마세요.
   - "이 콘텐츠가 DS 현업자에게 왜 중요한가"를 핵심 이유 한 문장으로 담으세요.
   - 나쁜 예: "LightGBM으로 모델 성능 높이기" (제목 반복)
   - 좋은 예: "범주형 변수 전처리 없이 OHE 대비 15% 빠른 학습 가능"

2. 닫힌 패싯: 반드시 아래 목록에 있는 값만 고르세요.
   - domain (1~2개): ["ai-ml", "data-eng", "software-eng", "systems", "product", "business", "research-method", "career", "tools"]
   - content_type (1개): ["paper", "case-study", "tutorial", "talk", "news", "interview", "opinion", "release"]
   - half_life (1개): ["ephemeral", "seasonal", "durable", "foundational"]

3. tags: 분류 라벨이 아니라 본문/자막에 실제로 등장한 고유명사·기술명만 최대 5개.
   콘텐츠에 나오지 않은 관심 분야나 일반 분류명을 추측해 추가하지 마세요.

4. key_points: 핵심 포인트 최대 3개.
{timestamp_instruction}

5. actionability (0~10): 읽은 뒤 구체적으로 실행할 수 있는 정도.
   - 0: 관점이나 인식만 제시하고 실행 단서가 없음.
   - 5: 적용 방향은 있으나 추가 조사·설계가 필요함.
   - 10: 구체적 기법·도구·수치·절차를 바로 적용할 수 있음.

6. depth (0~10): 확보된 텍스트 안의 정보 밀도와 설명 깊이.
   - 0: 제목이나 홍보 문구를 재진술하는 수준.
   - 5: 작동 방식 또는 구체적 사례를 설명함.
   - 10: 메커니즘·트레이드오프·실패 사례를 함께 다룸.
   제공된 텍스트 밖의 내용을 추측해서 점수를 높이지 마세요.

7. skip_reason: actionability와 depth가 모두 낮으면 스킵 사유를 간단히, 아니면 null.

반드시 아래 JSON 구조로만 응답하세요:
{{
  "one_line_summary": "...",
  "domain": ["ai-ml"],
  "content_type": "tutorial",
  "half_life": "durable",
  "tags": ["LightGBM", "Kubernetes"],
  "key_points": [{{"point": "...", "timestamp": "12:34"}}],
  "actionability": 7,
  "depth": 6,
  "skip_reason": null
}}
"""

_EVIDENCE_OUTPUT_PROMPT = """

## 근거 수준
- 현재 근거: {evidence_level}
- depth의 최대값: {depth_cap}

## 근거 기반 추가 분석
8. production_ideas: 본문/자막에 나온 구체적인 기법·도구·수치를 근거로 한 현업 적용 아이디어 0~2개.
   일반론을 채우지 말고, 만들 근거가 부족하면 빈 리스트를 반환하세요.

9. quiz: 제공된 본문/자막만으로 답할 수 있는 객관식 퀴즈 0~2문항.
   각 문항은 3~4개 선지, 정답 인덱스, 해설을 포함하고 근거가 부족하면 빈 리스트를 반환하세요.

위 JSON 객체에 다음 두 필드도 포함하세요:
  "production_ideas": ["..."],
  "quiz": [{{"question": "...", "options": ["A", "B", "C", "D"], "answer_index": 0, "explanation": "..."}}]
"""

_LIMITED_EVIDENCE_PROMPT = """

## 근거 수준
- 현재 근거: {evidence_level}
- 제공된 근거만 사용하고 depth는 최대 {depth_cap}점으로 채점하세요.
"""


def determine_evidence_level(item: RawContent) -> EvidenceLevel:
    """수집된 실제 텍스트의 종류와 분량으로 근거 수준을 결정한다."""
    transcript = (item.transcript or "").strip()
    body = (item.body or "").strip()

    if transcript:
        if len(transcript) >= FULL_TRANSCRIPT_MIN_CHARS:
            return EvidenceLevel.FULL
        return EvidenceLevel.PARTIAL

    # YouTube의 body에는 수집기가 영상 설명글 fallback을 저장한다. 설명글은
    # 길어도 영상 본문을 대체하지 못하므로 전문이나 초록으로 승격하지 않는다.
    if item.source_type == SourceType.YOUTUBE:
        if body:
            return EvidenceLevel.DESCRIPTION
        return EvidenceLevel.TITLE_ONLY

    if body:
        if len(body) >= FULL_BODY_MIN_CHARS:
            return EvidenceLevel.FULL
        return EvidenceLevel.PARTIAL

    return EvidenceLevel.TITLE_ONLY


async def _call_llm_with_fallback(prompt: str, title: str, groq_model: str | None = None) -> dict:
    """
    Groq를 우선 시도하고 실패하면 Gemini로 넘어간다.

    groq_model=None이면 분석용 기본 모델(settings.groq_model)을 쓴다. 메타데이터
    랭킹은 정수 인덱스 배열 출력이 안정적인 모델(settings.groq_ranking_model)을
    넘겨 오버라이드한다. Gemini 폴백 경로는 groq_model의 영향을 받지 않는다.

    이전 구현은 `if groq_api_key: Groq else: Gemini` 라서 폴백이
    "키가 없을 때"만 동작하고 "호출이 실패했을 때"는 동작하지 않았다.
    2026-07-17경 Groq 모델이 404(폐기 추정)를 내기 시작하자 6건 전부
    analysis_failed -> relevance 0 -> 전량 탈락했고, 다이제스트가
    3일간 발행되지 않았다. 제공자 하나가 죽어도 파이프라인은 살아야 한다.

    두 제공자가 모두 실패하면 마지막 예외를 올려 보내 기존 처리(스킵)를 따른다.
    """
    settings = get_settings()
    last_error: Exception | None = None
    request_model = groq_model or settings.groq_model

    if settings.groq_api_key:
        try:
            logger.info("using_groq", model=request_model, title=title[:50])
            # 기본(분석) 경로는 기존 호출 형태를 그대로 유지한다. 랭킹처럼 모델을
            # 오버라이드해야 할 때만 model을 넘긴다(_call_groq가 model or groq_model 처리).
            if groq_model is not None:
                return await _call_groq(prompt, model=groq_model)
            return await _call_groq(prompt)
        except Exception as e:
            last_error = e
            logger.warning(
                "groq_failed_falling_back",
                model=request_model,
                error=str(e)[:200],
                title=title[:50],
            )

    if settings.gemini_api_key:
        logger.info("using_gemini", model=settings.gemini_model, title=title[:50])
        return await _call_gemini(prompt)

    if last_error is not None:
        raise last_error
    raise RuntimeError("사용 가능한 LLM 제공자가 없습니다 (GROQ_API_KEY / GEMINI_API_KEY 미설정)")


# ──────────────────────────────────────────────
# Stage 1 메타 랭킹 → Stage 2 Gemini 전사 (v2 §5)
# ──────────────────────────────────────────────

_METADATA_RANKING_PROMPT = """\
당신은 Data Science 현업자를 위한 콘텐츠 큐레이터입니다.
아래 YouTube 영상 목록에서 DS 현업자에게 가장 가치 있는 {budget}개를 고르세요.
아직 전사(자막)는 없고 제목·출처·설명글만 주어집니다. 이 메타데이터만으로
"전사를 확보해 깊게 분석할 가치가 큰" 영상을 상대 비교로 골라내는 것이 목표입니다.

## 사용자가 최근 요청한 키워드
{keywords}

## 영상 목록
{listing}

반드시 아래 JSON 구조로만 응답하세요.
- index는 반드시 0 이상 (목록 개수-1) 이하의 정수이며 목록에 없는 숫자는 쓰지 마세요.
- 위 목록의 각 항목 앞에 붙은 [n]의 n이 그 항목의 index입니다.
- 가장 가치 있는 순서로 최대 {budget}개까지 넣으세요.
{{"top_indices": [가치 큰 순서의 index 최대 {budget}개]}}
"""


def _recover_youtube_video_id(url: str) -> str | None:
    """RawContent.url에서 YouTube video_id를 복원한다.

    수집기(collectors.fetch_youtube_recent)는 항상 https://youtu.be/{video_id}
    형식으로 저장하므로 경로 세그먼트가 곧 video_id다. watch?v=·embed·shorts
    형식도 방어적으로 처리한다. 복원 실패 시 None.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.endswith("youtu.be"):
            segment = parsed.path.lstrip("/").split("/", 1)[0]
            return segment or None
        query_v = parse_qs(parsed.query).get("v")
        if query_v and query_v[0]:
            return query_v[0]
        # /embed/{id}, /shorts/{id}, /v/{id} 등은 마지막 경로 세그먼트로 폴백한다.
        segments = [seg for seg in parsed.path.split("/") if seg]
        if segments:
            return segments[-1]
    except Exception as e:
        logger.warning("yt_video_id_recover_failed", url=url, error=str(e))
    return None


def _parse_top_indices(data: dict, item_count: int, budget: int) -> list[int]:
    """랭킹 응답의 top_indices를 방어적으로 파싱한다.

    정수로 변환 가능한 값만, 범위 내(0 <= i < item_count)만, 중복 제거,
    등장 순서를 보존하며 budget개로 절삭한다. 유효한 인덱스가 없으면 빈 리스트.
    """
    raw = (data or {}).get("top_indices")
    if not isinstance(raw, list):
        return []

    result: list[int] = []
    seen: set[int] = set()
    for value in raw:
        # bool은 int의 하위 타입이라 True/False가 0/1로 새는 것을 막는다.
        if isinstance(value, bool):
            continue
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < item_count and idx not in seen:
            seen.add(idx)
            result.append(idx)
        if len(result) >= budget:
            break
    return result


async def _select_top_youtube_items(
    items: list[RawContent],
    profile: UserProfile,
    budget: int,
) -> list[RawContent]:
    """Stage 1: 값싼 메타데이터(제목·출처·설명글)만으로 상위 budget건을 고른다.

    랭킹 LLM 호출·파싱이 어떤 이유로든 실패하면 앞 budget건으로 폴백해
    파이프라인이 절대 멈추지 않게 한다.
    """
    listing_lines: list[str] = []
    for idx, item in enumerate(items):
        description = (item.body or "").strip()[:200] or "(설명 없음)"
        listing_lines.append(
            f"[{idx}] 제목: {item.title}\n"
            f"     출처: {item.source_name}\n"
            f"     설명: {description}"
        )

    prompt = _METADATA_RANKING_PROMPT.format(
        budget=budget,
        keywords=", ".join(profile.keyword_requests[-5:]) if profile.keyword_requests else "없음",
        listing="\n".join(listing_lines),
    )

    # 랭킹은 정수 인덱스 배열 출력이 안정적인 llama 계열을 쓴다. gpt-oss 계열
    # (분석용 groq_model)은 이 스키마에서 범위 밖 인덱스·숫자 이어붙임을 낸다.
    settings = get_settings()
    try:
        data = await _call_llm_with_fallback(
            prompt, "youtube-metadata-ranking", groq_model=settings.groq_ranking_model
        )
        indices = _parse_top_indices(data, len(items), budget)
        if not indices:
            raise ValueError("top_indices가 비어 있거나 유효하지 않음")
        logger.info("yt_metadata_ranking_selected", indices=indices, total=len(items))
        return [items[i] for i in indices]
    except Exception as e:
        logger.warning(
            "yt_metadata_ranking_failed",
            error=str(e)[:200],
            fallback="first_n",
        )
        return items[:budget]


async def resolve_youtube_transcripts(
    items: list[RawContent],
    profile: UserProfile,
    budget: int,
) -> list[RawContent]:
    """dedup·채널 캡 이후 상위 N건만 Gemini로 전사한다(v2 §5 Stage 1→2).

    구버전은 수집 시점에 모든 YouTube 아이템을 전사해서, dedup·채널 캡 이전에
    런당 20여 건의 무거운 Gemini 호출이 터졌고 무료 티어 429 폭풍으로
    파이프라인이 50분간 멈춘 채 다이제스트를 못 냈다. 그래서 전사를 이 단계로
    미루고, 값싼 메타데이터 랭킹(Stage 1)으로 고른 상위 budget건에만 Gemini
    전사(Stage 2)를 쓴다. Groq가 랭킹·분석을 담당하므로 Gemini는 전사에만
    쓰이고 budget이 곧 Gemini 무료 토큰 사용량 상한이 된다.

    반환: items 전체(선택된 아이템만 .transcript가 채워지고 나머지는 None 유지).
    """
    settings = get_settings()

    if not items:
        return items

    # 게이트: 아래 중 하나라도 걸리면 전사를 건너뛰고 items를 그대로 돌려준다
    # (모든 transcript=None 유지). 이유를 로그로 남겨 계측 가능하게 한다.
    skip_reason: str | None = None
    if budget <= 0:
        skip_reason = "budget_non_positive"
    elif settings.dry_run:
        skip_reason = "dry_run"
    elif not settings.yt_gemini_transcript:
        skip_reason = "yt_gemini_transcript_disabled"
    elif not settings.gemini_api_key:
        skip_reason = "no_gemini_api_key"

    if skip_reason:
        logger.warning("yt_transcript_skipped", reason=skip_reason, item_count=len(items))
        return items

    # 선택: budget 이하면 랭킹 LLM 호출이 불필요하므로 전체를 전사한다.
    if len(items) <= budget:
        selected = items
    else:
        selected = await _select_top_youtube_items(items, profile, budget)

    # Stage 2: 선택된 아이템만 Gemini 전사. 무료 티어 TPM 보호를 위해 호출 사이에만
    # 대기하고(첫 호출 앞에는 두지 않음), transcript_source를 로그로 남겨 평가
    # 하니스가 근거 경로를 계측할 수 있게 한다.
    for position, item in enumerate(selected):
        if position > 0:
            await asyncio.sleep(_TRANSCRIPT_PACING_DELAY)

        video_id = _recover_youtube_video_id(item.url)
        if not video_id:
            logger.warning("yt_transcript_video_id_unresolved", url=item.url, title=item.title[:60])
            logger.info("yt_transcript_resolved", title=item.title[:60], transcript_source="none")
            continue

        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        transcript = await fetch_transcript_via_gemini(watch_url, None)
        if transcript:
            item.transcript = transcript
            logger.info("yt_transcript_resolved", title=item.title[:60], transcript_source="gemini")
        else:
            # 전사 실패 시 body(설명글)가 있으면 DESCRIPTION 근거로, 없으면 none.
            source = "description" if item.body else "none"
            logger.info("yt_transcript_resolved", title=item.title[:60], transcript_source=source)

    return items


def _coerce_score(data: dict, key: str, title: str) -> int:
    """
    LLM 응답에서 0~10 점수를 안전하게 읽는다.

    키 누락·문자열("8")·소수(7.5)·범위 초과를 모두 흡수한다.
    누락 시 0을 반환하되 반드시 경고를 남긴다 — 조용히 0이 되면
    해당 아이템이 필터에서 탈락한 이유를 나중에 추적할 수 없다.
    """
    raw = data.get(key)
    if raw is None:
        logger.warning("score_key_missing", key=key, title=title[:50])
        return 0
    try:
        return min(10, max(0, int(float(raw))))
    except (TypeError, ValueError):
        logger.warning("score_key_invalid", key=key, value=repr(raw), title=title[:50])
        return 0


def derive_relevance_score(actionability: int, depth: int) -> int:
    """A(60%)와 D(40%)를 기존 0~10 관련도 점수로 투영한다."""
    bounded_actionability = min(10, max(0, actionability))
    bounded_depth = min(10, max(0, depth))
    weighted_score = (
        bounded_actionability * ACTIONABILITY_WEIGHT
        + bounded_depth * DEPTH_WEIGHT
    )
    # 점수가 음수가 아니므로 +0.5 후 절삭해 일반적인 사사오입을 적용한다.
    return min(10, max(0, int(weighted_score + 0.5)))


def _build_analysis_prompt(
    item: RawContent,
    profile: UserProfile,
    content_text: str,
    timestamp_instruction: str,
    evidence_level: EvidenceLevel,
) -> str:
    """공통 분석 프롬프트에 근거 수준상 허용된 출력만 추가한다."""
    prompt = ANALYSIS_PROMPT.format(
        keywords=", ".join(profile.keyword_requests[-5:]) if profile.keyword_requests else "없음",
        title=item.title,
        source_name=item.source_name,
        source_type=item.source_type.value,
        content=content_text,
        timestamp_instruction=timestamp_instruction,
    )
    evidence_values = {
        "evidence_level": evidence_level.value,
        "depth_cap": EVIDENCE_DEPTH_CAPS[evidence_level],
    }
    if evidence_level in {EvidenceLevel.FULL, EvidenceLevel.PARTIAL}:
        return prompt + _EVIDENCE_OUTPUT_PROMPT.format(**evidence_values)
    return prompt + _LIMITED_EVIDENCE_PROMPT.format(**evidence_values)


async def _call_gemini(prompt: str, _retry: int = 3) -> dict:
    """Gemini REST API 호출 → 파싱된 JSON dict 반환.
    429 응답 시 retryDelay만큼 대기 후 최대 _retry회 재시도.
    """
    settings = get_settings()
    url = _GEMINI_API_URL.format(model=settings.gemini_model)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }

    for attempt in range(_retry + 1):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, params={"key": settings.gemini_api_key}, json=payload)

        if resp.status_code == 429:
            # retryDelay는 에러 응답 body에 포함 (없으면 기본 30초)
            retry_after = 30
            try:
                details = resp.json().get("error", {}).get("details", [])
                for d in details:
                    if d.get("@type", "").endswith("RetryInfo"):
                        delay_str = d.get("retryDelay", "30s")
                        retry_after = int(delay_str.rstrip("s")) + 2
                        break
            except Exception:
                pass

            if attempt < _retry:
                logger.warning("gemini_rate_limited", attempt=attempt + 1, wait_seconds=retry_after)
                await asyncio.sleep(retry_after)
                continue
            else:
                resp.raise_for_status()

        resp.raise_for_status()
        result = resp.json()
        break

    # 응답 구조: result["candidates"][0]["content"]["parts"][0]["text"]
    text = result["candidates"][0]["content"]["parts"][0]["text"]

    # response_mime_type=application/json 지정 시 대부분 순수 JSON 반환.
    # 간혹 ```json 블록으로 감싸는 경우를 대비해 제거.
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)

    return json.loads(text)


async def _call_groq(prompt: str, _retry: int = 3, model: str | None = None) -> dict:
    """Groq OpenAI-compatible API 호출 → 파싱된 JSON dict 반환.
    429 응답 시 Retry-After 헤더 기준 대기 후 최대 _retry회 재시도.

    model=None이면 settings.groq_model(분석용 gpt-oss-120b)을 쓴다. 메타데이터
    랭킹처럼 다른 모델이 필요한 호출은 model을 명시해 오버라이드한다.
    """
    settings = get_settings()
    request_model = model or settings.groq_model

    payload = {
        "model": request_model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    for attempt in range(_retry + 1):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(_GROQ_API_URL, headers=headers, json=payload)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "30")) + 2
            if attempt < _retry:
                logger.warning("groq_rate_limited", attempt=attempt + 1, wait_seconds=retry_after)
                await asyncio.sleep(retry_after)
                continue
            else:
                resp.raise_for_status()

        resp.raise_for_status()
        result = resp.json()
        break

    text = result["choices"][0]["message"]["content"]
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _mock_analysis(item: RawContent) -> ContentAnalysis:
    """DRY_RUN=true 시 Gemini 호출 없이 반환하는 더미 분석 결과."""
    evidence_level = determine_evidence_level(item)
    depth = min(8, EVIDENCE_DEPTH_CAPS[evidence_level])
    return ContentAnalysis(
        relevance_score=derive_relevance_score(8, depth),
        one_line_summary=f"[DRY RUN] {item.title[:40]}",
        tags=["DRY RUN", "테스트"],
        evidence_level=evidence_level,
        domain=["tools"],
        content_type="tutorial",
        half_life="seasonal",
        actionability=8,
        depth=depth,
        key_points=[
            KeyPoint(point="[DRY RUN] 핵심 포인트 1", timestamp="01:00" if item.source_type.value == "youtube" else None),
            KeyPoint(point="[DRY RUN] 핵심 포인트 2", timestamp="02:00" if item.source_type.value == "youtube" else None),
        ],
        production_ideas=["[DRY RUN] 현업 적용 아이디어 1", "[DRY RUN] 현업 적용 아이디어 2"],
        quiz=[
            QuizItem(
                question="[DRY RUN] 테스트 퀴즈 문항",
                options=["A", "B", "C", "D"],
                answer_index=0,
                explanation="[DRY RUN] 정답 해설",
            )
        ],
        skip_reason=None,
    )


async def analyze_content(
    item: RawContent,
    profile: UserProfile,
) -> ContentAnalysis:
    """단일 콘텐츠를 Gemini로 분석"""
    settings = get_settings()

    if settings.dry_run:
        logger.info("dry_run_mock_analysis", title=item.title[:50])
        return _mock_analysis(item)

    evidence_level = determine_evidence_level(item)
    content_text = (item.transcript or item.body or "")[:4000]

    is_youtube = item.source_type.value == "youtube"
    has_transcript = bool(item.transcript)
    if is_youtube and has_transcript:
        timestamp_instruction = (
            "   - 반드시 위 자막 텍스트에 실제로 등장한 [MM:SS] 형식의 시간만 timestamp로 사용하세요.\n"
            "     자막에 없는 시간을 추측하거나 임의로 생성하지 마세요. 확실하지 않으면 timestamp를 null로 두세요."
        )
    elif is_youtube and not has_transcript:
        timestamp_instruction = (
            "   - 이 영상은 자막을 가져올 수 없어 실제 타임라인 정보가 없습니다.\n"
            "     timestamp를 임의로 만들지 말고 모든 key_point의 timestamp는 반드시 null로 설정하세요."
        )
    else:
        timestamp_instruction = "   - timestamp는 null."

    prompt = _build_analysis_prompt(
        item=item,
        profile=profile,
        content_text=content_text,
        timestamp_instruction=timestamp_instruction,
        evidence_level=evidence_level,
    )

    try:
        data = await _call_llm_with_fallback(prompt, item.title)

        # 점수 키를 대괄호로 직접 접근하면, 모델이 키 하나를 빠뜨렸을 때
        # KeyError -> except -> relevance_score=0 이 되어 그날 아이템이 전량 탈락한다.
        # 다이제스트가 통째로 비는 실패 모드이므로 방어적으로 읽는다.
        actionability = _coerce_score(data, "actionability", item.title)
        # relevance_score에는 근거 게이트가 적용된 depth만 반영한다.
        depth = min(_coerce_score(data, "depth", item.title), EVIDENCE_DEPTH_CAPS[evidence_level])
        return ContentAnalysis(
            relevance_score=derive_relevance_score(actionability, depth),
            one_line_summary=data.get("one_line_summary") or item.title,
            tags=data.get("tags", []),
            key_points=[KeyPoint(**kp) for kp in data.get("key_points", [])],
            production_ideas=data.get("production_ideas", []),
            quiz=[QuizItem(**q) for q in data.get("quiz", [])],
            skip_reason=data.get("skip_reason"),
            evidence_level=evidence_level,
            domain=data.get("domain", []),
            content_type=data.get("content_type", "news"),
            half_life=data.get("half_life", "seasonal"),
            actionability=actionability,
            depth=depth,
        )

    except Exception as e:
        logger.error("analysis_failed", title=item.title, error=str(e))
        return ContentAnalysis(
            relevance_score=0,
            one_line_summary="분석 실패",
            key_points=[],
            production_ideas=[],
            quiz=[],
            skip_reason=f"분석 중 오류: {str(e)}",
            evidence_level=evidence_level,
        )


async def filter_and_analyze(
    items: list[RawContent],
    profile: UserProfile,
) -> list[DigestItem]:
    """수집된 콘텐츠를 필터링 + 분석하여 다이제스트 아이템 생성"""
    settings = get_settings()
    digest_items: list[DigestItem] = []

    for i, item in enumerate(items):
        # API rate limit 준수 (dry run은 스킵)
        if i > 0 and not settings.dry_run:
            delay = _GROQ_RATE_LIMIT_DELAY if settings.groq_api_key else _RATE_LIMIT_DELAY
            await asyncio.sleep(delay)

        analysis = await analyze_content(item, profile)

        if analysis.relevance_score >= settings.relevance_threshold:
            digest_items.append(DigestItem(raw=item, analysis=analysis))
            logger.info("item_included", title=item.title, score=analysis.relevance_score)
        else:
            logger.info("item_skipped", title=item.title, score=analysis.relevance_score, reason=analysis.skip_reason)

    digest_items.sort(key=lambda x: x.analysis.relevance_score, reverse=True)
    return digest_items[:settings.max_items_per_digest]
