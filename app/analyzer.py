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
_RATE_LIMIT_DELAY = 8.0
_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

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
1. relevance_score (1-10): DS 현업자에게 얼마나 유용한지. 사용자 관심 토픽과의 관련도도 반영.
2. one_line_summary: 한 줄 요약 (한국어, 30자 이내)
3. tags: 이 콘텐츠가 맞닿아 있는 기술/분야 태그 최대 5개 (예: MLOps, A/B testing, Kubernetes, 추천시스템). 영어 또는 한국어 혼용 가능.
4. key_points: 핵심 포인트 최대 3개. 영상이면 timestamp 포함 (MM:SS).
5. production_ideas: 이 내용을 실제 현업 프로덕션에 적용할 수 있는 구체적 아이디어 1-2개
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
    prompt = ANALYSIS_PROMPT.format(
        topics=", ".join(profile.preferred_topics),
        keywords=", ".join(profile.keyword_requests[-5:]) if profile.keyword_requests else "없음",
        title=item.title,
        source_name=item.source_name,
        source_type=item.source_type.value,
        content=content_text,
    )

    try:
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
        # Gemini free tier 15 RPM 준수 (dry run은 스킵)
        if i > 0 and not settings.dry_run:
            await asyncio.sleep(_RATE_LIMIT_DELAY)

        analysis = await analyze_content(item, profile)

        if analysis.relevance_score >= settings.relevance_threshold:
            digest_items.append(DigestItem(raw=item, analysis=analysis))
            logger.info("item_included", title=item.title, score=analysis.relevance_score)
        else:
            logger.info("item_skipped", title=item.title, score=analysis.relevance_score, reason=analysis.skip_reason)

    digest_items.sort(key=lambda x: x.analysis.relevance_score, reverse=True)
    return digest_items[:settings.max_items_per_digest]
