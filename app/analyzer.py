"""
Gemini 기반 콘텐츠 분석기
httpx로 Gemini REST API 직접 호출 — google-generativeai SDK 불필요
(Python 3.14에서 SDK의 protobuf C extension 충돌 회피)
"""
import asyncio
from collections import Counter
import json
import re
import time
import structlog
import httpx
from urllib.parse import urlparse, parse_qs

from app.config import get_settings
from app.concepts import (
    load_vocabulary,
    novelty_rate,
    register as register_concepts,
    save_vocabulary,
    vocabulary_for_prompt,
)
from app.directives import (
    Directive,
    directive_score,
    filter_sources,
    interpret as interpret_directives,
)
from app.quiz_results import weak_concepts
from app.preferences import (
    build_signal,
    describe_for_prompt,
    preference_score,
)
from app.models import (
    RawContent, ContentAnalysis, KeyPoint, QuizItem, SourceType,
    DigestItem, UserProfile, EvidenceLevel, EVIDENCE_DEPTH_CAPS,
)
from app.transcript_gemini import fetch_transcript_via_gemini

logger = structlog.get_logger()

# Gemini free tier: gemini-2.5-flash 약 10 RPM → 호출 간 최소 8초
# Groq free tier: 30 RPM → 호출 간 최소 3초. 이 고정 대기는 RPM 보호용이고,
# TPM(무료 플랜 8,000)은 _GroqPacer 가 응답 헤더를 보고 따로 맡는다. 페이서는
# monotonic 경과를 반영하므로 이 3초는 페이서 대기와 겹쳐서 계산된다.
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
# 난이도 지시가 있을 때의 depth 가중. "더 어렵게"는 곧 깊이 축을 더 세게 보라는
# 뜻이므로 새 휴리스틱을 만들지 않고 기존 두 축의 배합만 옮긴다.
DEPTH_WEIGHT_HARDER = 0.6
DEPTH_WEIGHT_EASIER = 0.2

ANALYSIS_PROMPT = """\
당신은 Data Science 현업 팀의 시니어 DS입니다.
아래 콘텐츠를 분석해서 JSON으로 응답하세요.

## 사용자가 명시적으로 요청한 키워드
- 최근 요청 키워드: {keywords}
- 사용자가 직접 남긴 지시: {standing_note}
  지시가 난이도·깊이·형식·관점에 관한 것이면 요약·핵심 포인트·퀴즈의
  깊이와 관점을 그에 맞추세요 ("더 어렵게"면 메커니즘·트레이드오프·수식의 의미까지,
  "더 쉽게"면 비유와 결론 위주로). 점수(actionability·depth)는 지시와 무관하게
  텍스트 근거로만 매기세요 — 지시는 선정 단계에서 따로 반영됩니다.

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

3-1. concepts: 이 콘텐츠가 다루는 **재사용 가능한 개념** 최대 3개.
   - tags보다 한 층 위입니다. tags가 `glm-5.3-flash`라면 concepts는 `모델 벤치마킹`
     처럼 다른 콘텐츠에도 다시 등장할 층위여야 합니다.
   - 제품명·버전·회사명 같은 고유명사는 concepts에 넣지 마세요. 그건 tags입니다.
   - **아래 기존 개념 목록에 해당하는 것이 있으면 표기를 바꾸지 말고 그대로
     재사용하세요.** 같은 뜻을 다르게 적으면 별개 개념이 되어 축적이 깨집니다.
   - 목록에 정말 없을 때만 새로 만드세요.

   기존 개념: {concept_vocabulary}

4. key_points: 핵심 포인트 최대 3개.
{timestamp_instruction}

4-1. positioning: 논문·연구 콘텐츠(content_type이 "paper"이거나 출처가 arXiv)일 때만,
   이 논문이 어디에 있는 연구인지 한국어 2~3문장(200자 이내).
   - 배경: 어떤 문제를 왜 푸는가, 기존 접근은 무엇이었고 어디서 막혔는가
   - 위치: 어느 연구 흐름의 어느 지점인가(예: "RAG 코드 생성에서 검색 단계를
     개선하는 계열"), 기존 대비 무엇이 새로운가
   - 초록·본문에 근거가 없으면 지어내지 말고 null. 논문이 아니면 null.

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
  "concepts": ["모델 서빙"],
  "key_points": [{{"point": "...", "timestamp": "12:34"}}],
  "positioning": null,
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
8. production_ideas: 본문/자막의 기법을 **다른 맥락으로 옮겨 붙인** 적용 아이디어
   0~3개. **3은 상한이지 목표가 아닙니다.**
   - **각 아이디어는 본문에 나온 기법·구조·조건 하나를 출발점으로 지목할 수 있어야
     합니다. 못 대면 빼세요.**
   - 출발점을 **다른 단계**나 **다른 도메인의 문제**로 옮기고, **무엇을 만들지**와
     **무엇이 개선되는지**를 한국어 한 문장씩 짧게 쓰세요.
   - **"이 알고리즘을 구현한다" 식 재진술은 금지**입니다. 일반론에 인용만 덧댄 것도
     같습니다. (좋은 예: "수렴 판정 기준을 재학습 시점 감지로 옮겨 쓴다")
   - **창의성은 본문의 기법을 다른 데 적용하는 상상이지 없는 사실의 창작이 아닙니다.**
     지어내지 말고, 출발점이 없으면 **빈 리스트를 반환하세요.**

9. quiz: 제공된 본문/자막만으로 답할 수 있는 객관식 퀴즈 0~2문항.
   각 문항은 3~4개 선지, 정답 인덱스, 해설을 포함하고 근거가 부족하면 빈 리스트를 반환하세요.
   - **선지가 수식의 세부만 다른 문항은 만들지 마세요.** 첨자·지수만 바꾼 보기는
     받아쓰기 시험입니다.
   - **수치·고유명사를 그대로 되묻는 암기 문항도 금지**입니다. 개수·비율·지표값·
     도구명·제품명·버전을 답으로 요구하지 마세요.
     (나쁜 예: "레이어가 몇 개인가", "98.6%를 기록한 지표는?")
   - 대신 메커니즘·판단을 물으세요: 왜 그렇게 되는가, 조건이 바뀌면 결과가 어떻게
     달라지는가, 트레이드오프는 무엇인가, 어떤 상황에서 실패하는가.
     (좋은 예: "수렴 뒤 남는 차이는 무엇으로 설명되는가")
   - 수치·고유명사가 **조건으로** 등장하는 것은 괜찮습니다. 금지는 **답으로 되묻는
     것**입니다.
   - 수식이 꼭 필요하면 **말로 풀어 쓰세요** ("반지름 r인 공의 측도의 로그를
     α제곱근으로 적분"). 기호가 꼭 필요하면 유니코드만 쓰세요(∫ Σ √ ≤ α μ ₀ ²).

## 표기 규칙 (발송 채널이 Discord·이메일이라 수식 렌더링이 없습니다)
- LaTeX를 쓰지 마세요. `\\int`, `$...$`, `_{{...}}`, `^{{...}}`, `\\frac` 모두 금지입니다.
  렌더링되지 않고 원문 그대로 보여서 오히려 읽기 어려워집니다.
- 첨자·지수는 유니코드 문자로 쓰세요: x₀, x₁, xⁿ, x², √x, μ, α, Σ, ∫, ≈, ≤, →
- 그래도 표현이 안 되면 기호를 버리고 한국어로 설명하세요. 정확한 기호보다
  읽히는 문장이 낫습니다.

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


async def _call_llm_with_fallback(
    prompt: str,
    title: str,
    groq_model: str | None = None,
    json_schema: dict | None = None,
) -> dict:
    """
    Groq를 우선 시도하고 실패하면 Gemini로 넘어간다.

    groq_model=None이면 분석용 기본 모델(settings.groq_model)을 쓴다. 메타데이터
    랭킹은 settings.groq_ranking_model을 넘겨 오버라이드하고, json_schema로
    strict 구조화 출력을 요청한다. Gemini 폴백 경로는 groq_model·json_schema
    어느 쪽의 영향도 받지 않는다(프롬프트에 적힌 형식으로만 응답한다).

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
                return await _call_groq(prompt, model=groq_model, json_schema=json_schema)
            return await _call_groq(prompt, json_schema=json_schema)
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


async def resolve_directives(items: list[RawContent]) -> Directive:
    """축적된 자연어 지시를 구조화한다. 런당 한 번만 부르고 결과를 내려 쓴다.

    directives 모듈이 analyzer를 import하면 순환이 되므로, LLM 호출자를 여기서
    주입한다. 아는 출처 식별자도 같이 넘겨 drop_sources가 실재하는 값에만
    대응되게 한다 — 하드 필터라 모델이 지어낸 이름이 통하면 안 된다.
    """
    known_sources = {i.source_key for i in items if i.source_key}
    return await interpret_directives(
        _call_llm_with_fallback,
        vocabulary_for_prompt(load_vocabulary()),
        known_sources,
    )


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

## 사용자가 👍한 콘텐츠의 태그
{liked_tags}

## 사용자가 👎한 콘텐츠의 태그
{disliked_tags}

## 사용자가 직접 남긴 지시
{standing_note}

## 영상 목록
{listing}

반드시 아래 JSON 구조로만 응답하세요.
- index는 반드시 0 이상 (목록 개수-1) 이하의 정수이며 목록에 없는 숫자는 쓰지 마세요.
- 위 목록의 각 항목 앞에 붙은 [n]의 n이 그 항목의 index입니다.
- 가장 가치 있는 순서로 최대 {budget}개까지 넣으세요.
- 👍/👎 태그는 동률일 때 참고하는 보조 신호입니다. 태그가 겹친다는 이유만으로
  내용이 얕은 영상을 올리지 마세요.
{{"top_indices": [가치 큰 순서의 index 최대 {budget}개]}}
"""

# 랭킹 응답 강제용 JSON Schema (Groq strict 구조화 출력).
#
# json_object 모드에서는 gpt-oss 계열이 이 스키마를 뭉갠다(범위 밖 인덱스·숫자
# 이어붙임). strict=true는 constrained decoding이라 문법적으로 정수 배열 외의
# 토큰이 나올 수 없다. 지원 모델은 openai/gpt-oss-20b·120b뿐이다.
#
# minItems/maximum 같은 키워드는 strict 모드 지원이 제공자마다 들쭉날쭉하므로
# 넣지 않는다. 개수 절삭과 범위 검사는 _parse_top_indices가 이미 한다.
_RANKING_JSON_SCHEMA: dict = {
    "name": "youtube_metadata_ranking",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "top_indices": {
                "type": "array",
                "items": {"type": "integer"},
            }
        },
        "required": ["top_indices"],
        "additionalProperties": False,
    },
}


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
    directive: Directive | None = None,
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

    # 👍/👎는 "무엇을 깊게 볼지" 고르는 이 단계에만 넣는다. 분석 단계에 넣으면
    # 취향이 근거 점수(actionability·depth)를 밀어올려 근거 게이트가 무의미해진다.
    signal = build_signal(profile.liked_item_ids, profile.disliked_item_ids)
    liked_tags, disliked_tags = describe_for_prompt(signal)

    prompt = _METADATA_RANKING_PROMPT.format(
        budget=budget,
        keywords=", ".join(profile.keyword_requests[-5:]) if profile.keyword_requests else "없음",
        liked_tags=liked_tags,
        disliked_tags=disliked_tags,
        standing_note=(directive.standing_note if directive else "") or "없음",
        listing="\n".join(listing_lines),
    )

    # 랭킹 출력은 모델이 아니라 스키마로 강제한다. json_object 모드에서 gpt-oss
    # 계열이 내던 범위 밖 인덱스·숫자 이어붙임은 strict constrained decoding으로
    # 사라진다. (예전엔 그 회피책으로 llama를 썼는데 2026-08-16 셧다운됐다.)
    settings = get_settings()
    try:
        data = await _call_llm_with_fallback(
            prompt,
            "youtube-metadata-ranking",
            groq_model=settings.groq_ranking_model,
            json_schema=_RANKING_JSON_SCHEMA,
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
    directive: Directive | None = None,
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
        selected = await _select_top_youtube_items(items, profile, budget, directive)

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


_RELATIVE_RATING_PROMPT = """\
당신은 Data Science 현업자를 위한 콘텐츠 큐레이터입니다.
오늘 수집된 후보 {count}건을 **서로 비교해서** 각각 1~10점으로 평가하세요.

개별 콘텐츠를 절대 기준으로 채점하지 마세요. 이 목록 안에서의 상대 위치를 매기는
것이 목표입니다.

## 점수 분포 규칙 (반드시 지킬 것)
- 최고점과 최저점의 차이가 **4점 이상**이어야 합니다.
- 전체의 절반 이상에 같은 점수를 주지 마세요.
- 가장 나은 1~2건과 가장 못한 1~2건을 먼저 정하고, 나머지를 그 사이에 배치하세요.

## 판단 기준
- 이 목록의 다른 후보 대신 이걸 읽어야 할 이유가 있는가
- 구체적인 기법·수치·사례가 있는가, 아니면 일반론인가
- 제목만 그럴듯하고 내용이 얇지는 않은가

{directive_block}## 후보 목록
{listing}

반드시 아래 JSON 구조로만 응답하세요. 모든 index에 대해 하나씩 넣으세요.
{{"ratings": [{{"index": 0, "rating": 8}}]}}
"""

# 상대 평가 응답 강제용 스키마. 랭킹과 같은 이유로 strict를 쓴다 —
# json_object 모드에서 gpt-oss는 정수 배열·객체 배열을 뭉갠다.
_RELATIVE_RATING_SCHEMA: dict = {
    "name": "relative_rating",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "ratings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "rating": {"type": "integer"},
                    },
                    "required": ["index", "rating"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["ratings"],
        "additionalProperties": False,
    },
}

# 절대 축과 상대 평가의 혼합 비율.
# 상대를 더 무겁게 두는 이유는 실측이다 — 절대 축(actionability·depth)은 73%가
# 5~6점에 몰려 변별력이 거의 없다. 그렇다고 절대를 0으로 두면 후보가 전부 약한
# 날에도 1등이 9점을 받아, "오늘은 건질 게 없었다"는 정보가 사라진다.
# 절대가 천장을 잡고 상대가 순서를 만든다.
RELATIVE_WEIGHT = 0.6
ABSOLUTE_WEIGHT = 0.4


# 근거 천장·바닥값은 **언제나 기본 배합으로** 잰다. 난이도 지시가 이 둘까지
# 움직이면 사용자가 말 한마디로 탈락 기준을 바꾸는 셈이 되고, 후보가 전부
# 얇은 날엔 다이제스트가 짧아지거나 비어버린다 — §23이 정확히 그 이유로
# 바닥값을 혼합 전 절대 점수에 걸었다. 지시는 **순서**를 정하고,
# 근거 게이트는 **자격**을 정한다.
def evidence_ceiling(level: EvidenceLevel) -> int:
    """해당 근거 수준에서 나올 수 있는 최대 relevance_score.

    근거 게이트(§3.3)는 depth를 근거 수준별로 clamp한다. 상대 평가가 그 위로
    점수를 밀어올릴 수 있으면 게이트가 무의미해진다 — 제목만 있는 아이템이
    전사를 확보한 아이템을 이길 수 있게 된다. 그래서 혼합 결과에 같은 천장을
    다시 씌운다.
    """
    return derive_relevance_score(10, EVIDENCE_DEPTH_CAPS[level])


def blend_relevance(
    absolute: int,
    relative: int | None,
    level: EvidenceLevel | None = None,
) -> int:
    """절대 점수와 상대 평가를 섞는다. 상대가 없으면 절대를 그대로 쓴다.

    level을 주면 근거 게이트의 천장을 넘지 못하게 막는다.
    """
    if relative is None:
        return absolute
    bounded = min(10, max(0, relative))
    blended = absolute * ABSOLUTE_WEIGHT + bounded * RELATIVE_WEIGHT
    score = min(10, max(0, int(blended + 0.5)))
    if level is not None:
        score = min(score, evidence_ceiling(level))
    return score


def _parse_ratings(data: dict, item_count: int) -> dict[int, int]:
    """상대 평가 응답을 {index: rating}으로 방어적으로 판다.

    범위 밖 index·bool·중복은 버린다. 유효한 것만 남기므로 일부만 와도
    그만큼은 쓸 수 있다(빠진 아이템은 절대 점수로 폴백된다).
    """
    rows = (data or {}).get("ratings")
    if not isinstance(rows, list):
        return {}

    ratings: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx, rating = row.get("index"), row.get("rating")
        if isinstance(idx, bool) or isinstance(rating, bool):
            continue
        try:
            idx, rating = int(idx), int(rating)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < item_count and idx not in ratings:
            ratings[idx] = min(10, max(0, rating))
    return ratings


async def apply_relative_rating(
    analyzed: list[DigestItem], standing_note: str = ""
) -> int:
    """후보 전체를 한 번에 비교시켜 relevance_score를 다시 매긴다.

    왜 필요한가 (PROGRESS 항목 D):
        아이템을 하나씩 절대 채점하면 LLM이 중앙으로 몰린다. 실측 164건에서
        actionability의 73%, depth의 74%가 5~6점이었고 relevance IQR이 1이었다.
        점수가 뭉치면 상위 5건을 고르는 일이 사실상 동전 던지기가 된다.
        프롬프트로 점수 정의를 조이는 시도는 이미 한 번 실패했다(§1.1).

        LLM은 절대 평가보다 상대 비교에서 훨씬 안정적이다. 그래서 후보를 한
        목록에 놓고 서로 비교시킨다.

    실패해도 파이프라인은 멈추지 않는다. 호출·파싱이 깨지면 절대 점수가 그대로
    남고, 일부 index만 오면 온 것만 반영된다.

    반환: 상대 평가가 반영된 건수(로깅용).
    """
    # 후보가 2건 이하면 비교의 의미가 없다.
    if len(analyzed) < 3:
        return 0

    listing = "\n".join(
        f"[{i}] 제목: {d.raw.title}\n"
        f"     출처: {d.raw.source_name} / 근거수준: {d.analysis.evidence_level.value}\n"
        f"     요약: {d.analysis.one_line_summary}\n"
        f"     핵심: {' | '.join(kp.point for kp in d.analysis.key_points[:2]) or '없음'}"
        for i, d in enumerate(analyzed)
    )
    # 사용자 지시는 점수를 벌리는 이 단계에도 들어가야 한다. 분석 프롬프트에만
    # 있으면 글쓰기는 바뀌어도 순위는 그대로다(실측 2026-09-03).
    directive_block = (
        f"## 사용자 지시\n{standing_note}\n"
        "이 지시에 맞는 후보를 위로 두세요. 단, 근거가 얇은 후보를 지시만으로 "
        "올리지는 마세요.\n\n"
        if standing_note
        else ""
    )
    prompt = _RELATIVE_RATING_PROMPT.format(
        count=len(analyzed), listing=listing, directive_block=directive_block
    )

    try:
        data = await _call_llm_with_fallback(
            prompt, "relative-rating", json_schema=_RELATIVE_RATING_SCHEMA
        )
        ratings = _parse_ratings(data, len(analyzed))
    except Exception as e:
        logger.warning("relative_rating_failed", error=str(e)[:200], fallback="absolute")
        return 0

    if not ratings:
        logger.warning("relative_rating_empty", fallback="absolute")
        return 0

    for i, d in enumerate(analyzed):
        d.analysis.relevance_score = blend_relevance(
            d.analysis.relevance_score, ratings.get(i), d.analysis.evidence_level
        )

    # 모델이 분포 규칙(최고-최저 4점 이상)을 실제로 지켰는지 남긴다.
    # 이 단계의 효과 전체가 "모델이 시킨 대로 벌려주는가"에 걸려 있고, 그건
    # 프롬프트 지시라 보장이 아니다 — 실측으로만 확인된다. 이 로그가 그 증거다.
    values = sorted(ratings.values())
    logger.info(
        "relative_rating_spread",
        rated=len(ratings),
        low=values[0],
        high=values[-1],
        spread=values[-1] - values[0],
        obeyed=values[-1] - values[0] >= 4,
        distinct=len(set(values)),
    )

    return len(ratings)


def depth_weight_for(directive) -> float:
    """난이도 지시 → depth 가중. 지시가 없으면 기본 배합."""
    bias = getattr(directive, "depth_bias", 0) if directive else 0
    if bias > 0:
        return DEPTH_WEIGHT_HARDER
    if bias < 0:
        return DEPTH_WEIGHT_EASIER
    return DEPTH_WEIGHT


def derive_relevance_score(
    actionability: int, depth: int, depth_weight: float = DEPTH_WEIGHT
) -> int:
    """A와 D를 기존 0~10 관련도 점수로 투영한다. 기본 배합은 A 60% / D 40%.

    depth_weight는 난이도 지시가 옮긴다(depth_weight_for). 두 축의 합이 1이
    되게 actionability 가중은 여기서 맞춘다.
    """
    bounded_actionability = min(10, max(0, actionability))
    bounded_depth = min(10, max(0, depth))
    depth_weight = min(1.0, max(0.0, depth_weight))
    weighted_score = (
        bounded_actionability * (1.0 - depth_weight)
        + bounded_depth * depth_weight
    )
    # 점수가 음수가 아니므로 +0.5 후 절삭해 일반적인 사사오입을 적용한다.
    return min(10, max(0, int(weighted_score + 0.5)))


def _build_analysis_prompt(
    item: RawContent,
    profile: UserProfile,
    content_text: str,
    timestamp_instruction: str,
    evidence_level: EvidenceLevel,
    concept_vocabulary: str = "(아직 없음 — 자유롭게 만드세요)",
    standing_note: str = "",
) -> str:
    """공통 분석 프롬프트에 근거 수준상 허용된 출력만 추가한다."""
    prompt = ANALYSIS_PROMPT.format(
        keywords=", ".join(profile.keyword_requests[-5:]) if profile.keyword_requests else "없음",
        standing_note=standing_note or "없음",
        concept_vocabulary=concept_vocabulary,
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


_DURATION_RE = re.compile(
    r"^\s*(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<ms>\d+(?:\.\d+)?)ms)?(?:(?P<s>\d+(?:\.\d+)?)s)?\s*$"
)


def _parse_duration(value: object) -> float | None:
    """Groq 헤더의 기간 문자열("2.5s", "7.66s", "1m3.2s", "1h2m3s", "500ms")을 초로 바꾼다.

    단위 없는 숫자("14.5")는 초로 본다. 해석할 수 없으면 None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    match = _DURATION_RE.match(text)
    if not match or not any(match.group(k) for k in ("h", "m", "ms", "s")):
        return None
    seconds = 0.0
    if match.group("h"):
        seconds += float(match.group("h")) * 3600
    if match.group("m"):
        seconds += float(match.group("m")) * 60
    if match.group("ms"):
        seconds += float(match.group("ms")) / 1000
    if match.group("s"):
        seconds += float(match.group("s"))
    return seconds


def _header(headers: object, name: str) -> str | None:
    """헤더 값을 읽는다. httpx.Headers 는 대소문자를 무시하지만 테스트 대역은
    일반 dict 일 수 있어 원래 이름과 소문자 이름을 모두 시도한다."""
    try:
        value = headers.get(name)  # type: ignore[union-attr]
        if value is None:
            value = headers.get(name.lower())  # type: ignore[union-attr]
        return None if value is None else str(value)
    except Exception:
        return None


class _GroqReservation:
    """reserve() 가 돌려주는 예약 정보.

    observe()/release() 가 이 객체를 보고 예약분을 정산한다. 헤더가 remaining 을
    덮어쓰면 정산 완료, 응답을 못 받았거나(예외) 헤더 없는 429 처럼 소비되지
    않은 예약은 release() 가 되돌린다.
    """

    __slots__ = (
        "model",
        "raw_prompt_tokens",
        "estimated_prompt_tokens",
        "needed_tokens",
        "margin_tokens",
        "reserved_tokens",
        "reserved_requests",
        "settled_tokens",
        "settled_requests",
    )

    def __init__(
        self,
        model: str,
        raw_prompt_tokens: float,
        estimated_prompt_tokens: float,
        needed_tokens: float,
        margin_tokens: float,
    ):
        self.model = model
        self.raw_prompt_tokens = raw_prompt_tokens
        self.estimated_prompt_tokens = estimated_prompt_tokens
        # needed 는 여유를 뺀 순수 추정(프롬프트 추정 + 출력 EMA). 오차 추적의 기준.
        self.needed_tokens = needed_tokens
        self.margin_tokens = margin_tokens
        # 실제로 차감한 양. 관측 전이라 차감하지 않았으면 0.
        self.reserved_tokens = 0.0
        self.reserved_requests = 0.0
        self.settled_tokens = True
        self.settled_requests = True

    @property
    def total_tokens(self) -> float:
        return self.needed_tokens + self.margin_tokens


class _GroqModelState:
    """모델 하나의 한도 상태. Groq 한도는 모델 단위라 페이서가 모델별로 하나씩 둔다."""

    EMA_ALPHA = 0.3
    DEFAULT_LIMIT_TOKENS = 8000.0
    DEFAULT_COMPLETION_TOKENS = 1000.0
    # 예약 오차 EMA 의 몇 배를 여유로 두는가, 그리고 최소 여유(needed 대비 비율)
    MARGIN_ERROR_FACTOR = 1.5
    MARGIN_MIN_RATIO = 0.20

    def __init__(self) -> None:
        # 토큰 한도
        self.limit_tokens: float = self.DEFAULT_LIMIT_TOKENS
        self.remaining_tokens: float | None = None
        self.tokens_seen_at: float | None = None
        # 토큰 한도가 가득 차는(또는 창이 리셋되는) 절대 시각(monotonic). 헤더의
        # x-ratelimit-reset-tokens 에서 온다. 버킷 모델에서는 "가득 참" 시각,
        # 고정창 모델에서는 리셋 시각이라 양쪽에서 "그 뒤엔 limit" 이 맞는다.
        self.tokens_reset_at: float | None = None
        # 요청 한도
        self.limit_requests: float | None = None
        self.remaining_requests: float | None = None
        self.requests_seen_at: float | None = None
        self.requests_reset_at: float | None = None
        # 비용 추정
        self.completion_ema: float = self.DEFAULT_COMPLETION_TOKENS
        self.prompt_ratio_ema: float = 1.0
        # |실제 total - 예약 needed| 의 EMA. 관측 전에는 None.
        self.error_ema: float | None = None

    @property
    def has_observation(self) -> bool:
        return self.remaining_tokens is not None or self.remaining_requests is not None

    @property
    def reset_tokens(self) -> float | None:
        """마지막 관측 시각 기준 리셋까지 남은 초(테스트·로그용)."""
        if self.tokens_reset_at is None or self.tokens_seen_at is None:
            return None
        return max(0.0, self.tokens_reset_at - self.tokens_seen_at)

    def margin_for(self, needed: float) -> float:
        """예약 여유. 오차 EMA 의 1.5배, 최소 needed 의 20%.

        시뮬레이션(토큰버킷 8,000/60초, usage ±20%, seed 3개)에서 10% 바닥은 31회 중
        429 가 최대 3회, 20% 는 0~1회였고 총 소요 차이는 0.2% 미만이었다. 버킷
        모델에서 여유는 매 호출 비용이 아니라 바닥 재고 한 번 분량이라 싸다.
        """
        floor = needed * self.MARGIN_MIN_RATIO
        if self.error_ema is None:
            return floor
        return max(floor, self.error_ema * self.MARGIN_ERROR_FACTOR)

    def available_tokens(self, now: float) -> float | None:
        if self.remaining_tokens is None or self.tokens_seen_at is None:
            return None
        if self.tokens_reset_at is not None and now >= self.tokens_reset_at:
            return self.limit_tokens
        elapsed = max(0.0, now - self.tokens_seen_at)
        refill = self.limit_tokens / 60.0
        return min(self.limit_tokens, self.remaining_tokens + refill * elapsed)

    def available_requests(self, now: float) -> float | None:
        if self.remaining_requests is None or self.requests_seen_at is None:
            return None
        if self.requests_reset_at is not None and now >= self.requests_reset_at:
            return self.limit_requests if self.limit_requests else max(self.remaining_requests, 1.0)
        elapsed = max(0.0, now - self.requests_seen_at)
        if self.limit_requests:
            refill = self.limit_requests / 60.0
            return min(self.limit_requests, self.remaining_requests + refill * elapsed)
        return self.remaining_requests

    def wait_seconds(self, now: float, needed_total: float) -> tuple[float, float | None, float | None]:
        """(대기 초, 가용 토큰, 가용 요청). 관측이 없으면 대기 0."""
        wait = 0.0
        available_tokens = self.available_tokens(now)
        if available_tokens is not None and available_tokens < needed_total:
            refill = self.limit_tokens / 60.0
            linear = (needed_total - available_tokens) / refill if refill > 0 else 0.0
            # 리셋 시각이 더 빠르면 거기까지만 기다리면 된다
            if self.tokens_reset_at is not None:
                linear = min(linear, max(0.0, self.tokens_reset_at - now))
            wait = max(wait, linear)

        available_requests = self.available_requests(now)
        if available_requests is not None and available_requests < 1.0:
            if self.requests_reset_at is not None:
                wait = max(wait, max(0.0, self.requests_reset_at - now))
            elif self.limit_requests:
                wait = max(wait, (1.0 - available_requests) / (self.limit_requests / 60.0))
        return wait, available_tokens, available_requests

    def deduct(self, reservation: _GroqReservation, sent_at: float) -> None:
        """전송 시점 기준으로 예약분을 차감한다(헤더가 오면 덮어쓴다)."""
        available_tokens = self.available_tokens(sent_at)
        if available_tokens is not None:
            self.remaining_tokens = available_tokens - reservation.total_tokens
            self.tokens_seen_at = sent_at
            reservation.reserved_tokens = reservation.total_tokens
            reservation.settled_tokens = False
            # 버킷 모델에서는 차감만큼 "가득 참" 시각이 늦춰진다. 창 모델에서는
            # 과보수적이지만 헤더가 오면 곧 덮어쓰므로 감수한다.
            refill = self.limit_tokens / 60.0
            if self.tokens_reset_at is not None and refill > 0:
                full_at = sent_at + max(0.0, self.limit_tokens - self.remaining_tokens) / refill
                self.tokens_reset_at = max(self.tokens_reset_at, full_at)
        available_requests = self.available_requests(sent_at)
        if available_requests is not None:
            self.remaining_requests = available_requests - 1.0
            self.requests_seen_at = sent_at
            reservation.reserved_requests = 1.0
            reservation.settled_requests = False
            if self.requests_reset_at is not None and self.limit_requests:
                refill = self.limit_requests / 60.0
                full_at = sent_at + max(0.0, self.limit_requests - self.remaining_requests) / refill
                self.requests_reset_at = max(self.requests_reset_at, full_at)

    def observe_headers(self, headers: object, now: float, reservation: _GroqReservation | None) -> None:
        try:
            limit = _parse_duration(_header(headers, "x-ratelimit-limit-tokens"))
            if limit is not None and limit > 0:
                self.limit_tokens = limit
            remaining = _parse_duration(_header(headers, "x-ratelimit-remaining-tokens"))
            if remaining is not None:
                self.remaining_tokens = max(0.0, remaining)
                self.tokens_seen_at = now
                self.tokens_reset_at = None
                if reservation is not None:
                    reservation.settled_tokens = True
            reset = _parse_duration(_header(headers, "x-ratelimit-reset-tokens"))
            if reset is not None and remaining is not None:
                self.tokens_reset_at = now + max(0.0, reset)
        except Exception:
            pass
        try:
            limit_req = _parse_duration(_header(headers, "x-ratelimit-limit-requests"))
            if limit_req is not None and limit_req > 0:
                self.limit_requests = limit_req
            remaining_req = _parse_duration(_header(headers, "x-ratelimit-remaining-requests"))
            if remaining_req is not None:
                self.remaining_requests = max(0.0, remaining_req)
                self.requests_seen_at = now
                self.requests_reset_at = None
                if reservation is not None:
                    reservation.settled_requests = True
            reset_req = _parse_duration(_header(headers, "x-ratelimit-reset-requests"))
            if reset_req is not None and remaining_req is not None:
                self.requests_reset_at = now + max(0.0, reset_req)
        except Exception:
            pass

    def observe_usage(self, usage: dict, reservation: _GroqReservation | None) -> None:
        try:
            completion = usage.get("completion_tokens")
            if isinstance(completion, (int, float)) and completion > 0:
                self.completion_ema += self.EMA_ALPHA * (float(completion) - self.completion_ema)
            prompt_tokens = usage.get("prompt_tokens")
            if (
                reservation is not None
                and reservation.raw_prompt_tokens > 0
                and isinstance(prompt_tokens, (int, float))
                and prompt_tokens > 0
            ):
                ratio = float(prompt_tokens) / reservation.raw_prompt_tokens
                ratio = min(4.0, max(0.25, ratio))
                self.prompt_ratio_ema += self.EMA_ALPHA * (ratio - self.prompt_ratio_ema)
            total = usage.get("total_tokens")
            if not isinstance(total, (int, float)) and isinstance(prompt_tokens, (int, float)) and isinstance(completion, (int, float)):
                total = prompt_tokens + completion
            if reservation is not None and isinstance(total, (int, float)) and total > 0:
                error = abs(float(total) - reservation.needed_tokens)
                if self.error_ema is None:
                    self.error_ema = error
                else:
                    self.error_ema += self.EMA_ALPHA * (error - self.error_ema)
                # 헤더가 없었으면 실제 소비량으로 예약분을 정정한다
                if not reservation.settled_tokens and self.remaining_tokens is not None:
                    self.remaining_tokens += reservation.reserved_tokens - float(total)
                    reservation.settled_tokens = True
        except Exception:
            pass

    def release(self, reservation: _GroqReservation) -> None:
        """헤더로 정산되지 않은 예약분을 되돌린다(소비되지 않은 요청)."""
        if not reservation.settled_tokens and self.remaining_tokens is not None:
            self.remaining_tokens = min(self.limit_tokens, self.remaining_tokens + reservation.reserved_tokens)
        reservation.settled_tokens = True
        if not reservation.settled_requests and self.remaining_requests is not None:
            cap = self.limit_requests if self.limit_requests else float("inf")
            self.remaining_requests = min(cap, self.remaining_requests + reservation.reserved_requests)
        reservation.settled_requests = True


class _GroqPacer:
    """Groq 무료 플랜 TPM/RPM 을 응답 헤더 기준으로 앞서서 지키는 페이서.

    배경: 3초 고정 간격은 RPM(30)만 지키고 TPM(8,000)은 못 지킨다. 호출 한 건이
    입력 약 2,800 + 출력 약 1,000 토큰이라 429 가 사실상 페이서 노릇을 해 왔고,
    그 왕복과 Retry-After 의 정수 반올림, +2초 여유가 전부 낭비였다.

    동작: 마지막으로 관측한 remaining 토큰에 (limit/60 x 경과초)만큼 충전을 더해
    현재 가용량을 추정하고(리셋 시각이 지났으면 limit), 이번 호출에 필요한 토큰
    (프롬프트 추정 + 출력 EMA + 오차 여유)이 모자라면 부족분 / 초당 충전량 만큼만
    기다린다. 요청 한도도 같은 논리로 본다. 헤더를 한 번도 못 본 첫 호출은
    기다리지 않는다. 한도는 모델 단위라 상태를 모델별로 나눠 둔다.

    호출은 지금 직렬이지만 나중에 병렬화돼도 안전하도록 "대기 계산 + 예약" 구간을
    asyncio.Lock 으로 보호한다. 파싱 실패는 전부 조용히 무시한다(기존 동작 유지가
    최우선).
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self.states: dict[str, _GroqModelState] = {}

    def _get_lock(self) -> asyncio.Lock:
        """이벤트 루프마다 Lock 을 새로 만든다. 테스트가 asyncio.run 을 여러 번
        돌려도 다른 루프에 묶인 Lock 때문에 죽지 않게 한다."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def state_for(self, model: str) -> _GroqModelState:
        state = self.states.get(model)
        if state is None:
            state = _GroqModelState()
            self.states[model] = state
        return state

    @property
    def has_observation(self) -> bool:
        return any(s.has_observation for s in self.states.values())

    @staticmethod
    def raw_prompt_tokens(prompt: str) -> float:
        """한글(가-힣) 1자 = 1토큰, 그 외 4자 = 1토큰으로 근사한다(보정 전)."""
        hangul = sum(1 for ch in prompt if "\uac00" <= ch <= "\ud7a3")
        return hangul + (len(prompt) - hangul) / 4

    def estimate_prompt_tokens(self, prompt: str, model: str) -> float:
        """근사치에 관측 보정비(EMA)를 곱한 프롬프트 토큰 추정."""
        return self.raw_prompt_tokens(prompt) * self.state_for(model).prompt_ratio_ema

    async def reserve(self, prompt: str, model: str, skip_wait: bool = False) -> _GroqReservation:
        """이번 호출에 필요한 토큰·요청을 예약한다. 부족하면 그만큼만 기다린다.

        skip_wait=True 는 429 의 Retry-After 를 다 기다린 직후의 재시도용. 서버가
        수용을 약속한 것이므로 추가 대기는 건너뛰고 차감만 한다(이중 대기 방지).
        """
        state = self.state_for(model)
        raw = self.raw_prompt_tokens(prompt)
        estimated = raw * state.prompt_ratio_ema
        needed = estimated + state.completion_ema
        margin = state.margin_for(needed)
        reservation = _GroqReservation(model, raw, estimated, needed, margin)

        async with self._get_lock():
            now = time.monotonic()
            wait, available_tokens, available_requests = state.wait_seconds(now, reservation.total_tokens)
            if skip_wait:
                wait = 0.0
            if wait > 0:
                logger.info(
                    "groq_paced",
                    model=model,
                    wait_seconds=round(wait, 2),
                    needed_tokens=round(reservation.total_tokens),
                    margin_tokens=round(margin),
                    available_tokens=None if available_tokens is None else round(available_tokens),
                    available_requests=None if available_requests is None else round(available_requests, 2),
                )
                await asyncio.sleep(wait)
            # 시계가 안 움직이는 환경(테스트)도 대기만큼은 충전된 것으로 친다.
            sent_at = max(time.monotonic(), now + wait)
            state.deduct(reservation, sent_at)
        return reservation

    def observe(
        self,
        headers: object,
        usage: dict | None = None,
        reservation: _GroqReservation | None = None,
        model: str | None = None,
        consumed: bool = False,
    ) -> None:
        """응답 헤더(성공·429 모두)와 usage 로 상태를 갱신한다. 파싱 실패는 무시.

        consumed=True 는 요청이 실제로 처리된(200) 경우. 헤더도 usage 도 없으면
        예약 차감을 그대로 둔다(토큰은 소비됐으므로 되돌리면 안 된다).
        """
        model_name = model or (reservation.model if reservation is not None else None)
        if model_name is None:
            return
        state = self.state_for(model_name)
        state.observe_headers(headers, time.monotonic(), reservation)
        if isinstance(usage, dict):
            state.observe_usage(usage, reservation)
        if consumed and reservation is not None:
            reservation.settled_tokens = True
            reservation.settled_requests = True

    def release(self, reservation: _GroqReservation | None) -> None:
        """응답을 못 받았거나 헤더 없는 429 처럼 소비되지 않은 예약을 되돌린다."""
        if reservation is None:
            return
        self.state_for(reservation.model).release(reservation)


# 프로세스 전체가 공유하는 단일 페이서. 메타데이터 랭킹·상대 랭킹 등 모든
# _call_groq 호출처가 같은 한도를 나눠 쓰므로 인스턴스도 하나여야 한다.
_groq_pacer = _GroqPacer()


def _groq_limit_type(resp: httpx.Response) -> str:
    """429 본문에서 어느 한도에 걸렸는지 뽑는다(error.type, 없으면 message 앞 120자)."""
    try:
        body = resp.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            if error.get("type"):
                return str(error["type"])
            if error.get("message"):
                return str(error["message"])[:120]
    except Exception:
        pass
    try:
        return (resp.text or "")[:120]
    except Exception:
        return ""


def _groq_retry_wait(resp: httpx.Response, limit_type: str) -> float:
    """429 대기 시간(초). Retry-After -> x-ratelimit-reset-* -> 30, 여유 +1초.

    Retry-After 는 float 로 읽는다. 예전에는 int() 라 "14.5" 가 오면 ValueError 로
    죽었다.
    """
    wait = _parse_duration(_header(resp.headers, "Retry-After"))
    if wait is None:
        reset_name = (
            "x-ratelimit-reset-requests"
            if "request" in limit_type.lower()
            else "x-ratelimit-reset-tokens"
        )
        wait = _parse_duration(_header(resp.headers, reset_name))
    if wait is None:
        wait = 30.0
    return max(0.0, wait) + 1.0


async def _call_groq(
    prompt: str,
    _retry: int = 3,
    model: str | None = None,
    json_schema: dict | None = None,
) -> dict:
    """Groq OpenAI-compatible API 호출 → 파싱된 JSON dict 반환.

    전송 전에 _GroqPacer 로 TPM/RPM 여유를 확인해 필요한 만큼만 기다리고, 응답
    헤더와 usage 로 페이서를 갱신한다. 429 가 그래도 오면 Retry-After(없으면
    x-ratelimit-reset-*) 기준 대기 후 최대 _retry회 재시도.

    model=None이면 settings.groq_model(분석용 gpt-oss-120b)을 쓴다. 메타데이터
    랭킹처럼 다른 모델이 필요한 호출은 model을 명시해 오버라이드한다.

    json_schema를 주면 strict 구조화 출력(constrained decoding)을 요청한다.
    지원 모델이 아니거나 스키마를 거부하면 Groq가 400을 내는데, 그때는 조용히
    json_object로 한 번 내려간다 - 랭킹이 구조화 출력 하나 때문에 파이프라인을
    멈추면 안 되고, 파싱은 _parse_top_indices가 이미 방어적으로 한다.
    """
    settings = get_settings()
    request_model = model or settings.groq_model
    pacer = _groq_pacer

    def _payload(schema: dict | None) -> dict:
        response_format = (
            {"type": "json_schema", "json_schema": schema}
            if schema is not None
            else {"type": "json_object"}
        )
        return {
            "model": request_model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": response_format,
            "temperature": 0.3,
        }

    payload = _payload(json_schema)
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    skip_wait = False
    for attempt in range(_retry + 1):
        reservation = await pacer.reserve(prompt, request_model, skip_wait=skip_wait)
        skip_wait = False
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(_GROQ_API_URL, headers=headers, json=payload)

            if resp.status_code == 429:
                pacer.observe(resp.headers, reservation=reservation)
                limit_type = _groq_limit_type(resp)
                retry_after = _groq_retry_wait(resp, limit_type)
                if attempt < _retry:
                    logger.warning(
                        "groq_rate_limited",
                        attempt=attempt + 1,
                        wait_seconds=retry_after,
                        limit_type=limit_type,
                    )
                    await asyncio.sleep(retry_after)
                    # 서버가 Retry-After 뒤 수용을 약속했으므로 재시도는 페이서 대기를 건너뛴다
                    skip_wait = True
                    continue
                else:
                    resp.raise_for_status()

            # 400 = 스키마/구조화 출력 거부. 같은 시도 안에서 json_object로 강등한다.
            if resp.status_code == 400 and payload["response_format"]["type"] == "json_schema":
                logger.warning(
                    "groq_json_schema_rejected",
                    model=request_model,
                    error=resp.text[:200],
                    fallback="json_object",
                )
                pacer.observe(resp.headers, reservation=reservation)
                pacer.release(reservation)
                payload = _payload(None)
                reservation = await pacer.reserve(prompt, request_model)
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(_GROQ_API_URL, headers=headers, json=payload)

            resp.raise_for_status()
            result = resp.json()
            usage = result.get("usage") if isinstance(result, dict) else None
            pacer.observe(resp.headers, usage, reservation, consumed=True)
            if isinstance(usage, dict):
                logger.info(
                    "groq_usage",
                    model=request_model,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    remaining_tokens=pacer.state_for(request_model).remaining_tokens,
                    estimated_prompt_tokens=round(reservation.estimated_prompt_tokens),
                    margin_tokens=round(reservation.margin_tokens),
                )
            break
        finally:
            # 응답을 못 받았거나(예외) 헤더 없는 429/400 이면 예약분을 되돌린다.
            # 정산된 예약에는 아무 영향이 없다.
            pacer.release(reservation)

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
        concepts=["드라이런 개념"],
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


def _split_concepts(raw_concepts) -> list[str]:
    """모델이 쉼표로 이어 낸 개념을 나누고 상한까지만 남긴다.

    상한을 다시 거는 이유: 나누면 개수가 늘어날 수 있는데 ContentAnalysis의
    concepts는 최대 3개다. 넘기면 pydantic이 거부해 **그 아이템의 분석이 통째로
    실패**한다 — 읽기 좋게 만들려다 아이템을 잃는 쪽이 훨씬 나쁘다.
    """
    from app.concepts import MAX_CONCEPTS_PER_ITEM, split_raw

    out: list[str] = []
    for item in raw_concepts or []:
        for part in split_raw(item):
            if part not in out:
                out.append(part)
    return out[:MAX_CONCEPTS_PER_ITEM]


def _clean_positioning(value) -> str | None:
    """모델이 null 대신 "null"·"없음"·빈 문자열을 내는 경우를 None으로 접는다."""
    text = str(value or "").strip()
    if not text or text.lower() in {"null", "none", "없음", "해당 없음", "n/a"}:
        return None
    return text[:400]


async def analyze_content(
    item: RawContent,
    profile: UserProfile,
    concept_vocabulary: str | None = None,
    standing_note: str = "",
    depth_weight: float = DEPTH_WEIGHT,
) -> ContentAnalysis:
    """단일 콘텐츠를 Gemini로 분석.

    concept_vocabulary는 기존 개념 목록 문자열이다. 프롬프트에 넣어 재사용을
    유도하는 것이 엔티티 해소의 예방책이다(app/concepts.py). 호출부가 런당 한 번
    읽어 넘긴다 — 아이템마다 파일을 읽을 이유가 없다.
    """
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
        concept_vocabulary=concept_vocabulary or "(아직 없음 — 자유롭게 만드세요)",
        standing_note=standing_note,
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
            relevance_score=derive_relevance_score(actionability, depth, depth_weight),
            one_line_summary=data.get("one_line_summary") or item.title,
            tags=data.get("tags", []),
            concepts=_split_concepts(data.get("concepts", [])),
            key_points=[KeyPoint(**kp) for kp in data.get("key_points", [])],
            positioning=_clean_positioning(data.get("positioning")),
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


def source_family(source_key: str) -> str:
    """정규화의 단위를 정하는 함수 — 소스 키가 아니라 **계열**로 묶는다.

    arXiv를 14개 카테고리로 늘리면서 source_key가 `arxiv:cs.LG`처럼 카테고리별로
    갈라졌다. 퍼널 기록에는 그게 맞다(어느 카테고리가 굶는지 보여야 한다).
    그런데 물량 정규화까지 그 키로 세면 **arXiv 16건이 13개의 작은 소스로 보인다.**
    각 키의 물량이 평균 아래라 감점이 아예 걸리지 않고, 안전판(per_source)도
    키마다 따로 세므로 걸리지 않는다.

    실측 시뮬레이션 — 논문이 계통적으로 2점 높게 채점될 때(depth가 높으니 그럴
    만하다) 다이제스트 5칸이 전부 arXiv로 찼다. 강도를 2.0으로 올려도 5/5였다.
    HackerNews에서 방금 고친 편중이 다른 문을 통해 그대로 돌아온 것이다.

        arxiv:cs.CL, arxiv:cs.IR, arxiv:cs.DC, arxiv:stat.ML, arxiv:stat.AP

    그래서 정규화·안전판은 계열로 센다. 같은 실험을 계열 기준으로 돌리면 1/5다.

    `prefix:suffix` 형태만 접는다. RSS·뉴스레터의 source_key는 피드 URL이라
    `https://...`인데, 콜론만 보고 자르면 전부 `https` 한 덩어리가 된다 —
    다양성을 지키려다 진짜 다양성을 지워버리는 정반대 버그가 된다.
    """
    key = str(source_key or "")
    if "://" in key or ":" not in key:
        return key
    return key.split(":", 1)[0]


def _select_diverse(
    candidates: list[DigestItem],
    limit: int,
    per_source: int,
    strength: float,
) -> list[DigestItem]:
    """점수순 후보에서 출처 편중을 **물량 기준으로 정규화**하며 limit건을 고른다.

    왜 하드 상한이 아니라 정규화인가:
        편중의 원인은 품질이 아니라 물량이다. 후보를 많이 내는 소스가 상위
        점수대에 더 많이 걸릴 뿐이다 — 실측에서 HackerNews 한 소스가 40일
        165건의 32.3%를 가져갔는데, 그건 수집기에 상한이 없어 키워드 4개 ×
        20건이 후보 풀에 들어왔기 때문이다.

        "출처당 2건까지" 같은 하드 상한은 딱 떨어지지만 거칠다. 물량 이점 없이
        정말 좋은 소스도 똑같이 잘린다. 대신 **후보 풀에서의 물량에 비례해
        반복 선택을 감점**한다.

            조정점수 = 점수 - strength × (이미 뽑은 수) × (물량 ÷ 평균 물량)

        물량이 평균인 소스는 배수 1, 평균의 5배를 낸 소스는 5배로 감점된다.
        첫 한 건은 감점이 없으므로(이미 뽑은 수 = 0) 어느 소스든 한 번은
        공정하게 경쟁한다.

    per_source는 안전판이다. 정규화가 예상 밖으로 약할 때 한 소스가 다이제스트를
    통째로 먹는 것만 막는다. 상한 때문에 정원을 못 채우면 상한을 완화한다 —
    약한 날 다이제스트를 짧게 만드는 것이 편중보다 나쁘다(§3.4가 해결한 문제다).
    """
    if not candidates or limit <= 0:
        return []

    def key_of(item: DigestItem) -> str:
        return source_family(item.raw.source_key or item.raw.source_name)

    volume = Counter(key_of(d) for d in candidates)
    mean_volume = len(candidates) / len(volume)

    picked: list[DigestItem] = []
    taken: Counter = Counter()
    remaining = list(candidates)

    while remaining and len(picked) < limit:
        def adjusted(item: DigestItem) -> float:
            source = key_of(item)
            over = volume[source] / mean_volume if mean_volume else 1.0
            return item.analysis.relevance_score - strength * taken[source] * over

        # 하드 상한에 걸리지 않은 것 중에서 조정점수 최대를 고른다.
        eligible = [d for d in remaining if taken[key_of(d)] < per_source] or remaining
        # max는 첫 최대값을 돌려주고 remaining이 점수순이므로 동점은 원래 순서를 따른다.
        best = max(eligible, key=adjusted)
        picked.append(best)
        taken[key_of(best)] += 1
        remaining.remove(best)

    if len(picked) < limit:
        logger.info("diversity_selection_short", picked=len(picked), limit=limit)

    # 발송 순서는 사람이 읽는 순서이므로 조정점수가 아니라 원래 점수순으로 되돌린다.
    picked.sort(key=lambda d: d.analysis.relevance_score, reverse=True)
    return picked


async def filter_and_analyze(
    items: list[RawContent],
    profile: UserProfile,
    directive: Directive | None = None,
) -> list[DigestItem]:
    """수집된 콘텐츠를 분석 후 상대 랭킹으로 다이제스트 아이템을 선정한다.

    절대 문턱(relevance_threshold=7)으로 거르던 방식은 약한 날 빈 다이제스트를
    낳았다. 근거 게이트가 얇은 근거(제목만 depth≤1, 설명글 depth≤3)를 정직하게
    캡하므로 대부분의 피드 아이템이 7점 미만이 되고, 6/2/5점만 나온 날에는 전부
    탈락해 발송이 통째로 비었다(실측). 그래서 v2 §3.4/§11대로 절대 문턱을 버리고
    상대 랭킹으로 전환한다: 후보를 점수순 정렬해 상위 max_items_per_digest건을
    뽑되, 명백한 저품질(relevance_floor 미만)만 제외한다. 약한 날에도 가용 후보
    중 최선을 발송해 빈 다이제스트를 구조적으로 없앤다.
    """
    settings = get_settings()

    # 지시의 하드 필터를 분석 **전에** 적용한다. 어차피 뺄 아이템을 분석하면
    # LLM 호출만 낭비된다. filter_sources는 제외 후 0건이 되면 제외를 포기한다.
    if directive is not None:
        items = filter_sources(items, directive)

    # 개념 어휘는 런당 한 번만 읽는다. 프롬프트에 넣어 기존 개념 재사용을
    # 유도하는 것이 엔티티 해소의 1차 방어선이다(app/concepts.py).
    vocabulary = load_vocabulary()
    vocabulary_text = vocabulary_for_prompt(vocabulary)
    # 난이도 지시는 점수 축의 배합을 옮긴다("더 어렵게" = depth를 더 세게).
    # 단 **정렬에만** 쓴다. 바닥값 판정은 아래에서 기본 배합으로 따로 잰다.
    depth_weight = depth_weight_for(directive)

    # 1) 모든 아이템을 먼저 분석한다(선정은 전체 점수를 본 뒤 상대적으로 결정).
    analyzed: list[DigestItem] = []
    for i, item in enumerate(items):
        # API rate limit 준수 (dry run은 스킵)
        if i > 0 and not settings.dry_run:
            delay = _GROQ_RATE_LIMIT_DELAY if settings.groq_api_key else _RATE_LIMIT_DELAY
            await asyncio.sleep(delay)

        analysis = await analyze_content(
            item,
            profile,
            concept_vocabulary=vocabulary_text,
            standing_note=directive.standing_note if directive else "",
            depth_weight=depth_weight,
        )
        analyzed.append(DigestItem(raw=item, analysis=analysis))

    # 2) 후보끼리 비교시켜 점수를 다시 매긴다(PROGRESS D).
    #    개별 절대 채점은 중앙으로 몰린다 — 실측 164건에서 actionability 73%,
    #    depth 74%가 5~6점이었고 relevance IQR이 1이었다. 그 상태로 상위 5건을
    #    고르면 사실상 동전 던지기다. 드라이런은 mock 점수라 비교할 게 없다.
    # 혼합 전 절대 점수를 붙잡아 둔다. 바닥값 판정에 쓴다(아래 참조).
    # actionability·depth로 되계산하지 않는 이유: relevance_score가 늘 그 둘에서
    # 파생된다는 불변식은 어디서도 강제되지 않는다. 지금 값을 그대로 스냅샷하는
    # 편이 어떤 경로로 만들어진 점수든 정확하다.
    absolute_score = {id(d): d.analysis.relevance_score for d in analyzed}

    # 난이도 지시가 걸린 런에서는 바닥값을 **기본 배합 점수**로 잰다.
    # 지시가 정렬을 바꾸는 것은 의도지만, 탈락 기준까지 바꾸면 "더 어렵게" 한
    # 마디에 얇은 후보가 무더기로 바닥 아래로 떨어진다(실측: 제목만 아이템이
    # 4점 → 3점). 후보가 전부 얇은 날엔 그대로 빈 다이제스트다.
    #
    # 여기서만 actionability·depth로 되계산하는 이유: 바로 위 analyze_content가
    # 그 두 값에 depth_weight만 적용해 점수를 만들었으므로, 이 지점에서는
    # 파생 불변식이 실제로 성립한다(§23이 경계한 것은 상대 평가로 값이 섞인
    # **뒤에** 되계산하는 경우다). 분석이 실패한 아이템은 두 값이 0이라
    # 결과도 0으로 남는다.
    if depth_weight != DEPTH_WEIGHT:
        absolute_score = {
            id(d): derive_relevance_score(d.analysis.actionability, d.analysis.depth)
            for d in analyzed
        }

    if not settings.dry_run:
        rated = await apply_relative_rating(
            analyzed, standing_note=directive.standing_note if directive else ""
        )
        if rated:
            logger.info("relative_rating_applied", rated=rated, total=len(analyzed))

    # 3) 바닥값 이상 후보만 남겨 점수순 정렬 후 상위 K건 선정.
    #
    #    동점은 👍/👎로 가른다. 점수 자체는 건드리지 않는다 — 근거가 같은 급일
    #    때만 취향이 순서를 정하므로 근거 게이트가 유지된다. 상대 평가로 점수가
    #    벌어져도 동점은 여전히 생기고, 그때 취향이 순서를 정한다.
    floor = settings.relevance_floor
    # 퀴즈에서 반복해서 틀린 개념을 선정에 되먹인다 — 여기가 §4.2 ⑥→⑦이
    # 닫히는 지점이다. 응답이 없거나 표본이 부족하면 빈 집합이라 무해하다.
    review = weak_concepts()
    signal = build_signal(
        profile.liked_item_ids, profile.disliked_item_ids, review=review
    )
    # 바닥값은 **혼합 전 절대 점수**에 건다. 상대 비교가 섞인 값에 문턱을 걸면
    # 상대 평가가 순서를 정하는 게 아니라 **탈락**을 시킨다. 후보가 전부 약한
    # 날엔 모델이 시킨 대로 낮은 점수를 뿌리므로 대량 탈락이 일어나 다이제스트가
    # 짧아지거나 비어버린다 — §3.4가 상대 랭킹을 도입한 목적(빈 다이제스트
    # 구조적 해소)과 정면으로 충돌한다.
    #
    # 바닥값의 역할은 "명백한 저품질 제외"이고, 그건 근거 기반의 절대 판단이다.
    # 정렬만 혼합 점수로 한다.
    candidates = [d for d in analyzed if absolute_score[id(d)] >= floor]
    candidates.sort(
        key=lambda d: (
            d.analysis.relevance_score,
            # 명시적 지시(±3)가 추론된 취향(±2)보다 무겁다. 사용자가 직접
            # 말한 것이 내가 눈치로 알아낸 것보다 우선해야 한다.
            preference_score(
                signal, d.raw.source_key, d.analysis.tags, d.analysis.concepts
            )
            + (
                directive_score(
                    directive, d.analysis.tags, d.analysis.concepts, d.analysis.depth
                )
                if directive
                else 0
            ),
        ),
        reverse=True,
    )
    selected = _select_diverse(
        candidates,
        limit=settings.max_items_per_digest,
        per_source=settings.max_items_per_source,
        strength=settings.source_diversity_strength,
    )

    # 4) 감사 로그: 각 아이템을 정확히 한 범주로 남긴다. DigestItem은 pydantic
    #    모델이라 값 기반 `in`은 O(n²)에다 오탐 위험이 있으므로 id()로 판별한다.
    selected_ids = {id(d) for d in selected}
    for d in analyzed:
        score = d.analysis.relevance_score
        if id(d) in selected_ids:
            logger.info(
                "item_included",
                title=d.raw.title,
                score=score,
                evidence_level=d.analysis.evidence_level.value,
            )
        elif score >= floor:
            logger.info("item_ranked_out", title=d.raw.title, score=score)
        else:
            logger.info("item_below_floor", title=d.raw.title, score=score, reason=d.analysis.skip_reason)

    # 5) 발송이 확정된 것만 어휘에 등록한다. 후보 전체를 넣으면 읽히지도 않은
    #    콘텐츠의 개념이 어휘를 채워, 재사용 유도 목록이 노이즈로 희석된다.
    #    등록은 표준명으로 되돌려 레코드에도 표준명이 남게 한다.
    all_resolved: list[str] = []
    all_created: list[str] = []
    for d in selected:
        resolved, created = register_concepts(d.analysis.concepts, vocabulary)
        d.analysis.concepts = resolved
        all_resolved.extend(resolved)
        all_created.extend(created)

    # 드라이런은 상태를 바꾸지 않는다 — mock 개념이 어휘에 섞이면 재사용 유도
    # 목록이 오염되고, 그게 실제 발송 프롬프트에 들어간다.
    if all_resolved and not settings.dry_run:
        save_vocabulary(vocabulary)
        rate = novelty_rate(all_resolved, all_created)
        # §3.6 메타 루브릭 "개념 신규율". §8.2 기준 0.8 초과면 해소 실패(어휘
        # 예방이 새는 중), 0.1 미만이면 정체다. 초기엔 당연히 1.0에 가깝다.
        logger.info(
            "concepts_registered",
            resolved=len(all_resolved),
            created=len(all_created),
            novelty_rate=rate,
            vocabulary_size=len(vocabulary.get("concepts") or {}),
        )

    logger.info(
        "relative_ranking_done",
        analyzed=len(analyzed),
        above_floor=len(candidates),
        selected=len(selected),
        sources=len({d.raw.source_key for d in selected}),
        depth_weight=depth_weight,
        floor=floor,
        preference_applied=not signal.is_empty(),
        review_concepts=sorted(review),
    )
    return selected
