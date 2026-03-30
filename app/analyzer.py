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
    RawContent, ContentAnalysis, KeyPoint, QuizItem,
    DigestItem, UserProfile,
)

logger = structlog.get_logger()

# Gemini free tier: gemini-2.5-flash 약 10 RPM → 호출 간 최소 8초
# Groq free tier: 30 RPM → 호출 간 최소 3초
_RATE_LIMIT_DELAY = 8.0
_GROQ_RATE_LIMIT_DELAY = 3.0
_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

ANALYSIS_PROMPT = """\
당신은 Data Science 현업 팀의 시니어 DS입니다.
아래 콘텐츠를 분석해서 JSON으로 응답하세요.

## 사용자 프로필
- 관심 토픽: {topics}
- 최근 선호 키워드: {keywords}

## 콘텐츠
- 제목: {title}
- 출처: {source_name} ({source_type})
- 본문/자막:
{content}

## 분석 기준

1. relevance_score (1-10): DS 현업자에게 얼마나 즉각적으로 유용한지 엄격하게 평가.
   점수 기준 (아래 기준을 반드시 지키세요 — 7~8점이 과반수를 차지하면 안 됩니다):
   - 10: 당장 팀 전체에 공유하거나 적용해야 할 핵심 인사이트. 읽지 않으면 손해인 수준.
   - 9: 실무에서 즉시 써먹을 수 있는 기법/도구가 구체적으로 제시됨.
   - 8: 관련 있고 실용적이지만 적용에는 약간의 맥락 파악이 필요함.
   - 7: 흥미롭지만 당장 행동으로 이어지기 어려운 일반적인 내용.
   - 5-6: DS와 느슨하게 관련 있거나 너무 입문/기초 수준.
   - 1-4: DS 현업과 무관하거나 광고성/비기술적 내용.
   채점 후 자기 점수를 다시 검토하세요: "이 콘텐츠가 실제로 9점 이상인가? 아니면 그냥 7점이 더 정직한가?"

2. one_line_summary: 한 줄 요약 (한국어, 30자 이내)
   - 제목을 그대로 옮기거나 paraphrase하지 마세요.
   - "이 콘텐츠가 DS 현업자에게 왜 중요한가"를 핵심 이유 한 문장으로 담으세요.
   - 나쁜 예: "LightGBM으로 모델 성능 높이기" (제목 반복)
   - 좋은 예: "범주형 변수 전처리 없이 OHE 대비 15% 빠른 학습 가능"

3. tags: 이 콘텐츠가 맞닿아 있는 기술/분야 태그 최대 5개 (예: MLOps, A/B testing, Kubernetes, 추천시스템). 영어 또는 한국어 혼용 가능.

4. key_points: 핵심 포인트 최대 3개.
{timestamp_instruction}

5. production_ideas: 이 콘텐츠에서 언급된 구체적인 기법/도구/알고리즘을 활용해 현업 DS 팀이 실제로 적용해볼 수 있는 아이디어 1-2개.
   반드시 지켜야 할 조건:
   - 이 콘텐츠의 핵심 내용을 직접 인용하거나 근거로 삼아야 합니다.
   - "데이터 파이프라인 개선", "모델 모니터링 도입" 같은 일반적인 문장은 안 됩니다.
   - 구체적인 기술명(예: LightGBM, Ray, Kafka, dbt), 지표(예: AUC 0.02 개선), 또는 구체적인 시나리오(예: "신규 유저 cold-start 문제에 content-based fallback 추가")를 포함해야 합니다.

6. quiz: 내용 확인용 객관식 퀴즈 2문항 (4지선다, 정답 인덱스, 해설 포함)

7. skip_reason: relevance_score가 6 이하면, 왜 스킵 추천하는지 간단히

반드시 아래 JSON 구조로만 응답하세요:
{{
  "relevance_score": 8,
  "one_line_summary": "...",
  "tags": ["MLOps", "Kubernetes", "모델 서빙"],
  "key_points": [{{"point": "...", "timestamp": "12:34"}}],
  "production_ideas": ["..."],
  "quiz": [{{"question": "...", "options": ["A", "B", "C", "D"], "answer_index": 0, "explanation": "..."}}],
  "skip_reason": null
}}
"""


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
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
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
    return ContentAnalysis(
        relevance_score=8,
        one_line_summary=f"[DRY RUN] {item.title[:40]}",
        tags=["DRY RUN", "테스트"],
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

    prompt = ANALYSIS_PROMPT.format(
        topics=", ".join(profile.preferred_topics),
        keywords=", ".join(profile.keyword_requests[-5:]) if profile.keyword_requests else "없음",
        title=item.title,
        source_name=item.source_name,
        source_type=item.source_type.value,
        content=content_text,
        timestamp_instruction=timestamp_instruction,
    )

    try:
        if settings.groq_api_key:
            logger.info("using_groq", model=settings.groq_model, title=item.title[:50])
            data = await _call_groq(prompt)
        else:
            data = await _call_gemini(prompt)
        return ContentAnalysis(
            relevance_score=data["relevance_score"],
            one_line_summary=data["one_line_summary"],
            tags=data.get("tags", []),
            key_points=[KeyPoint(**kp) for kp in data["key_points"]],
            production_ideas=data["production_ideas"],
            quiz=[QuizItem(**q) for q in data["quiz"]],
            skip_reason=data.get("skip_reason"),
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
