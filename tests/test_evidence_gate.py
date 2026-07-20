"""루브릭 v2의 근거 게이트와 닫힌 패싯 회귀 테스트."""

import asyncio
from types import SimpleNamespace

import pytest

import app.analyzer as analyzer
from app.analyzer import (
    ANALYSIS_PROMPT,
    FULL_BODY_MIN_CHARS,
    FULL_TRANSCRIPT_MIN_CHARS,
    derive_relevance_score,
    determine_evidence_level,
)
from app.models import (
    ContentAnalysis,
    EvidenceLevel,
    QuizItem,
    RawContent,
    SourceType,
    UserProfile,
)


def _quiz() -> QuizItem:
    return QuizItem(
        question="근거가 있는 질문",
        options=["하나", "둘", "셋"],
        answer_index=0,
        explanation="본문에 나온 설명",
    )


def _analysis(**overrides) -> ContentAnalysis:
    values = {
        "relevance_score": 5,
        "one_line_summary": "요약",
        "actionability": 5,
        "depth": 5,
    }
    values.update(overrides)
    return ContentAnalysis(**values)


@pytest.mark.parametrize(
    ("evidence_level", "submitted_depth", "expected_depth"),
    [
        (EvidenceLevel.FULL, 9, 9),
        (EvidenceLevel.FULL, 10, 10),
        (EvidenceLevel.FULL, 11, 10),
        (EvidenceLevel.PARTIAL, 5, 5),
        (EvidenceLevel.PARTIAL, 6, 6),
        (EvidenceLevel.PARTIAL, 10, 6),
        (EvidenceLevel.DESCRIPTION, 2, 2),
        (EvidenceLevel.DESCRIPTION, 3, 3),
        (EvidenceLevel.DESCRIPTION, 10, 3),
        (EvidenceLevel.TITLE_ONLY, 0, 0),
        (EvidenceLevel.TITLE_ONLY, 1, 1),
        (EvidenceLevel.TITLE_ONLY, 10, 1),
    ],
)
def test_depth_is_clamped_by_evidence_level(
    evidence_level: EvidenceLevel,
    submitted_depth: int,
    expected_depth: int,
):
    analysis = _analysis(evidence_level=evidence_level, depth=submitted_depth)
    assert analysis.depth == expected_depth


@pytest.mark.parametrize(
    "evidence_level",
    [EvidenceLevel.DESCRIPTION, EvidenceLevel.TITLE_ONLY],
)
def test_weak_evidence_removes_quiz_and_production_ideas(evidence_level: EvidenceLevel):
    analysis = _analysis(
        evidence_level=evidence_level,
        production_ideas=["근거 없는 아이디어"],
        quiz=[_quiz()],
    )
    assert analysis.production_ideas == []
    assert analysis.quiz == []


def test_full_evidence_preserves_quiz_and_production_ideas():
    quiz = _quiz()
    analysis = _analysis(
        evidence_level=EvidenceLevel.FULL,
        production_ideas=["본문 기반 아이디어"],
        quiz=[quiz],
    )
    assert analysis.production_ideas == ["본문 기반 아이디어"]
    assert analysis.quiz == [quiz]


def test_invalid_closed_facets_fall_back_to_safe_defaults():
    analysis = _analysis(
        domain=["not-a-domain"],
        content_type="whitepaper",
        half_life="forever",
    )
    assert analysis.domain == []
    assert analysis.content_type == "news"
    assert analysis.half_life == "seasonal"


def test_invalid_domains_are_removed_and_domain_is_limited_to_two():
    analysis = _analysis(
        domain=["ai-ml", "invalid", "data-eng", "systems"],
    )
    assert analysis.domain == ["ai-ml", "data-eng"]


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (
            RawContent(
                source_type=SourceType.YOUTUBE,
                source_name="채널",
                title="긴 자막",
                url="https://example.com/full-transcript",
                transcript="가" * FULL_TRANSCRIPT_MIN_CHARS,
                body="설명",
            ),
            EvidenceLevel.FULL,
        ),
        (
            RawContent(
                source_type=SourceType.YOUTUBE,
                source_name="채널",
                title="부분 자막",
                url="https://example.com/partial-transcript",
                transcript="가" * (FULL_TRANSCRIPT_MIN_CHARS - 1),
                body="설명",
            ),
            EvidenceLevel.PARTIAL,
        ),
        (
            RawContent(
                source_type=SourceType.RSS,
                source_name="블로그",
                title="긴 본문",
                url="https://example.com/full-body",
                body="가" * FULL_BODY_MIN_CHARS,
            ),
            EvidenceLevel.FULL,
        ),
        (
            RawContent(
                source_type=SourceType.NEWSLETTER,
                source_name="뉴스레터",
                title="짧은 초록",
                url="https://example.com/partial-body",
                body="짧은 초록",
            ),
            EvidenceLevel.PARTIAL,
        ),
        (
            RawContent(
                source_type=SourceType.YOUTUBE,
                source_name="채널",
                title="설명만 있음",
                url="https://example.com/description",
                body="가" * FULL_BODY_MIN_CHARS,
            ),
            EvidenceLevel.DESCRIPTION,
        ),
        (
            RawContent(
                source_type=SourceType.RSS,
                source_name="블로그",
                title="제목만 있음",
                url="https://example.com/title-only",
                body="   ",
            ),
            EvidenceLevel.TITLE_ONLY,
        ),
    ],
)
def test_determine_evidence_level(item: RawContent, expected: EvidenceLevel):
    assert determine_evidence_level(item) == expected


@pytest.mark.parametrize(
    ("actionability", "depth", "expected"),
    [(0, 0, 0), (8, 4, 6), (9, 9, 9), (10, 10, 10)],
)
def test_relevance_score_formula(actionability: int, depth: int, expected: int):
    assert derive_relevance_score(actionability, depth) == expected


def test_analyzer_ignores_llm_relevance_and_uses_gated_axes(monkeypatch):
    item = RawContent(
        source_type=SourceType.RSS,
        source_name="블로그",
        title="분석 대상",
        url="https://example.com/item",
        body="가" * FULL_BODY_MIN_CHARS,
    )

    async def fake_call(_prompt: str) -> dict:
        return {
            "relevance_score": 10,
            "one_line_summary": "축 기반 요약",
            "domain": ["ai-ml"],
            "content_type": "tutorial",
            "half_life": "durable",
            "tags": ["LightGBM"],
            "key_points": [],
            "production_ideas": [],
            "quiz": [],
            "actionability": 8,
            "depth": 4,
            "skip_reason": None,
        }

    monkeypatch.setattr(
        analyzer,
        "get_settings",
        lambda: SimpleNamespace(dry_run=False, groq_api_key="key", groq_model="test"),
    )
    monkeypatch.setattr(analyzer, "_call_groq", fake_call)

    analysis = asyncio.run(analyzer.analyze_content(item, UserProfile()))
    assert analysis.relevance_score == 6
    assert analysis.actionability == 8
    assert analysis.depth == 4


def test_analysis_prompt_does_not_inject_preferred_topics():
    assert "preferred_topics" not in ANALYSIS_PROMPT
    assert "{topics}" not in ANALYSIS_PROMPT
    assert "관심 토픽" not in ANALYSIS_PROMPT


def test_limited_evidence_prompt_omits_unsupported_output_fields():
    item = RawContent(
        source_type=SourceType.YOUTUBE,
        source_name="채널",
        title="설명만 있는 영상",
        url="https://example.com/video",
        body="영상 설명",
    )
    prompt = analyzer._build_analysis_prompt(
        item=item,
        profile=UserProfile(preferred_topics=["절대 주입되면 안 되는 토픽"]),
        content_text=item.body or "",
        timestamp_instruction="timestamp는 null.",
        evidence_level=EvidenceLevel.DESCRIPTION,
    )
    assert "production_ideas" not in prompt
    assert "quiz" not in prompt
    assert "절대 주입되면 안 되는 토픽" not in prompt
