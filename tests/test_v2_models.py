"""v2 데이터 모델(§5.4) 회귀 테스트.

Concept/Triplet/AtomicNote/Scores 생성·기본값, Scores.half_life 닫힌 vocab 검증,
그리고 ContentAnalysis가 신규 선택 필드 없이도 기존대로 검증되는 하위호환을 확인한다.

주의: 엔티티 해소·N/F축 계산·아토믹 노트 LLM 추출은 아직 구현하지 않았다(체크포인트).
여기서는 스키마(그릇)만 검증한다.

실행: pytest tests/test_v2_models.py -v
"""
import pytest
from pydantic import ValidationError

from app.models import (
    AtomicNote,
    Concept,
    ContentAnalysis,
    Scores,
    Triplet,
    HALF_LIVES,
)


# ──────────────────────────────────────────────
# Concept / Triplet / AtomicNote — 생성·기본값
# ──────────────────────────────────────────────

def test_concept_creation_and_defaults():
    c = Concept(id="ab-testing", label="A/B Testing")
    assert c.id == "ab-testing"
    assert c.label == "A/B Testing"
    assert c.aliases == []  # 기본값


def test_concept_with_aliases():
    c = Concept(id="ab-testing", label="A/B Testing", aliases=["split test", "AB test"])
    assert c.aliases == ["split test", "AB test"]


def test_triplet_creation():
    t = Triplet(subject="ab-testing", predicate="requires", object="sample-size")
    assert t.subject == "ab-testing"
    assert t.predicate == "requires"
    assert t.object == "sample-size"


def test_atomic_note_creation_and_defaults():
    n = AtomicNote(text="A/B 테스트는 충분한 표본 크기를 요구한다.")
    assert n.text == "A/B 테스트는 충분한 표본 크기를 요구한다."
    assert n.concepts == []  # 기본값
    assert n.source_timestamp is None  # 기본값
    assert n.chapter is None  # 기본값


def test_atomic_note_with_all_fields():
    n = AtomicNote(
        text="명제",
        concepts=["ab-testing"],
        source_timestamp="12:34",
        chapter="2. 실험 설계",
    )
    assert n.concepts == ["ab-testing"]
    assert n.source_timestamp == "12:34"
    assert n.chapter == "2. 실험 설계"


# ──────────────────────────────────────────────
# Scores — half_life 닫힌 vocab 검증(정상/위반)
# ──────────────────────────────────────────────

@pytest.mark.parametrize("half_life", HALF_LIVES)
def test_scores_accepts_all_closed_vocab(half_life: str):
    s = Scores(novelty=0.5, actionability=7, depth=4, half_life=half_life, fit=0.8)
    assert s.half_life == half_life


def test_scores_valid_creation():
    s = Scores(novelty=0.3, actionability=6, depth=3, half_life="durable", fit=0.9)
    assert s.novelty == 0.3
    assert s.actionability == 6
    assert s.depth == 3
    assert s.fit == 0.9


def test_scores_rejects_invalid_half_life():
    with pytest.raises(ValidationError):
        Scores(novelty=0.5, actionability=7, depth=4, half_life="forever", fit=0.8)


# ──────────────────────────────────────────────
# ContentAnalysis — 하위호환 + v2 선택 필드
# ──────────────────────────────────────────────

def test_content_analysis_backward_compatible_without_v2_fields():
    """신규 선택 필드를 주지 않아도 기존대로 검증되고 기본값이 채워진다."""
    a = ContentAnalysis(relevance_score=6, one_line_summary="요약")
    assert a.relevance_score == 6
    assert a.scores is None
    assert a.notes == []
    assert a.triplets == []


def test_content_analysis_accepts_v2_fields():
    a = ContentAnalysis(
        relevance_score=6,
        one_line_summary="요약",
        scores=Scores(novelty=0.1, actionability=5, depth=3, half_life="seasonal", fit=0.5),
        notes=[AtomicNote(text="명제")],
        triplets=[Triplet(subject="a", predicate="r", object="b")],
    )
    assert a.scores is not None
    assert a.scores.half_life == "seasonal"
    assert len(a.notes) == 1
    assert len(a.triplets) == 1
