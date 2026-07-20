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

from app.config import get_settings
from app.models import (
    RawContent, ContentAnalysis, KeyPoint, QuizItem, SourceType,
    DigestItem, UserProfile, EvidenceLevel, EVIDENCE_DEPTH_CAPS,
)

logger = structlog.get_logger()

# Gemini free tier: gemini-2.5-flash 약 10 RPM → 호출 간 최소 8초
# Groq free tier: 30 RPM → 호출 간 최소 3초
_RATE_LIMIT_DELAY = 8.0
_GROQ_RATE_LIMIT_DELAY = 3.0
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


async def _call_groq(prompt: str, _retry: int = 3) -> dict:
    """Groq OpenAI-compatible API 호출 → 파싱된 JSON dict 반환.
    429 응답 시 Retry-After 헤더 기준 대기 후 최대 _retry회 재시도.
    """
    settings = get_settings()

    payload = {
        "model": settings.groq_model,
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
        if settings.groq_api_key:
            logger.info("using_groq", model=settings.groq_model, title=item.title[:50])
            data = await _call_groq(prompt)
        else:
            data = await _call_gemini(prompt)

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
