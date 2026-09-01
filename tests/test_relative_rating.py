"""상대 평가 — 절대 채점의 중앙 몰림을 깨는 단계 (PROGRESS 항목 D).

실측(164건): actionability의 73%, depth의 74%가 5~6점이고 relevance IQR이 1이었다.
점수가 뭉치면 후보 30여 건에서 상위 5건을 고르는 일이 사실상 동전 던지기가 된다.
프롬프트로 점수 정의를 조이는 시도는 이미 한 번 실패했다(v2 §1.1).

실행: pytest tests/test_relative_rating.py -v
"""

from __future__ import annotations

import asyncio

import pytest

import app.analyzer as analyzer
from app.analyzer import (
    _parse_ratings,
    apply_relative_rating,
    blend_relevance,
    evidence_ceiling,
)
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    RawContent,
    SourceType,
)


def _item(score: int, level: EvidenceLevel = EvidenceLevel.FULL, title="제목") -> DigestItem:
    return DigestItem(
        raw=RawContent(
            source_type=SourceType.RSS,
            source_name="출처",
            source_key="src",
            title=title,
            url=f"https://example.com/{title}",
            body="본문",
        ),
        analysis=ContentAnalysis(
            relevance_score=score,
            one_line_summary="요약",
            tags=[],
            evidence_level=level,
            domain=["ai-ml"],
            content_type="tutorial",
            half_life="durable",
            actionability=score,
            depth=score,
            key_points=[],
            production_ideas=[],
            quiz=[],
        ),
    )


# ──────────────────────────────────────────────
# 응답 파싱
# ──────────────────────────────────────────────

def test_parse_ratings_basic():
    assert _parse_ratings({"ratings": [{"index": 0, "rating": 9}]}, 3) == {0: 9}


def test_parse_ratings_drops_out_of_range_index():
    assert _parse_ratings({"ratings": [{"index": 99, "rating": 9}]}, 3) == {}


def test_parse_ratings_keeps_first_of_duplicate_index():
    data = {"ratings": [{"index": 0, "rating": 9}, {"index": 0, "rating": 2}]}

    assert _parse_ratings(data, 3) == {0: 9}


def test_parse_ratings_rejects_bools():
    """bool은 int의 하위 타입이라 True가 1로 새어 들어온다."""
    assert _parse_ratings({"ratings": [{"index": True, "rating": 5}]}, 3) == {}


def test_parse_ratings_clamps_rating():
    assert _parse_ratings({"ratings": [{"index": 0, "rating": 99}]}, 3) == {0: 10}


@pytest.mark.parametrize("bad", [{}, {"ratings": "리스트 아님"}, {"ratings": [1, 2]}])
def test_parse_ratings_handles_garbage(bad):
    assert _parse_ratings(bad, 3) == {}


# ──────────────────────────────────────────────
# 혼합 — 근거 게이트가 유지돼야 한다
# ──────────────────────────────────────────────

def test_blend_without_relative_keeps_absolute():
    assert blend_relevance(6, None) == 6


def test_blend_spreads_scores():
    """같은 절대 점수라도 상대 평가가 다르면 벌어져야 한다."""
    high = blend_relevance(6, 10, EvidenceLevel.FULL)
    low = blend_relevance(6, 2, EvidenceLevel.FULL)

    assert high - low >= 4


def test_blend_respects_evidence_ceiling():
    """제목만 있는 아이템은 상대 만점을 받아도 근거 천장을 못 넘는다.

    이게 없으면 상대 평가가 근거 게이트(§3.3)를 우회해, 제목만 본 아이템이
    전사를 확보한 아이템을 이길 수 있다.
    """
    assert blend_relevance(2, 10, EvidenceLevel.TITLE_ONLY) == evidence_ceiling(
        EvidenceLevel.TITLE_ONLY
    )


def test_evidence_ceiling_is_ordered():
    """근거가 얕을수록 천장이 낮아야 한다."""
    ceilings = [
        evidence_ceiling(EvidenceLevel.FULL),
        evidence_ceiling(EvidenceLevel.PARTIAL),
        evidence_ceiling(EvidenceLevel.DESCRIPTION),
        evidence_ceiling(EvidenceLevel.TITLE_ONLY),
    ]

    assert ceilings == sorted(ceilings, reverse=True)
    assert len(set(ceilings)) == 4


def test_blend_never_exceeds_ten():
    assert blend_relevance(10, 10, EvidenceLevel.FULL) == 10


# ──────────────────────────────────────────────
# 적용
# ──────────────────────────────────────────────

def _patch_llm(monkeypatch, result=None, error=None):
    calls = {}

    async def _fake(prompt, title, **kwargs):
        calls["prompt"] = prompt
        calls["schema"] = kwargs.get("json_schema")
        if error:
            raise error
        return result

    monkeypatch.setattr(analyzer, "_call_llm_with_fallback", _fake)
    return calls


def test_apply_rewrites_scores(monkeypatch):
    items = [_item(6), _item(6), _item(6)]
    _patch_llm(monkeypatch, {"ratings": [
        {"index": 0, "rating": 9}, {"index": 1, "rating": 5}, {"index": 2, "rating": 2},
    ]})

    rated = asyncio.run(apply_relative_rating(items))

    assert rated == 3
    scores = [d.analysis.relevance_score for d in items]
    assert scores == sorted(scores, reverse=True)
    assert max(scores) - min(scores) >= 4


def test_apply_skips_when_too_few_candidates(monkeypatch):
    """2건 이하는 비교의 의미가 없다."""
    calls = _patch_llm(monkeypatch, {"ratings": []})

    assert asyncio.run(apply_relative_rating([_item(6), _item(6)])) == 0
    assert "prompt" not in calls


def test_apply_uses_strict_schema(monkeypatch):
    """랭킹과 같은 이유 — json_object 모드에선 gpt-oss가 객체 배열을 뭉갠다."""
    calls = _patch_llm(monkeypatch, {"ratings": [{"index": 0, "rating": 5}]})

    asyncio.run(apply_relative_rating([_item(6) for _ in range(3)]))

    assert calls["schema"]["strict"] is True


def test_apply_falls_back_to_absolute_on_error(monkeypatch):
    """평가가 실패해도 다이제스트는 나가야 한다."""
    items = [_item(6), _item(5), _item(4)]
    _patch_llm(monkeypatch, error=RuntimeError("llm down"))

    assert asyncio.run(apply_relative_rating(items)) == 0
    assert [d.analysis.relevance_score for d in items] == [6, 5, 4]


def test_apply_falls_back_when_response_empty(monkeypatch):
    items = [_item(6), _item(5), _item(4)]
    _patch_llm(monkeypatch, {"ratings": []})

    assert asyncio.run(apply_relative_rating(items)) == 0
    assert [d.analysis.relevance_score for d in items] == [6, 5, 4]


def test_apply_keeps_absolute_for_missing_indices(monkeypatch):
    """일부만 와도 온 것만 반영하고 나머지는 절대 점수를 유지한다."""
    items = [_item(6), _item(5), _item(4)]
    _patch_llm(monkeypatch, {"ratings": [{"index": 0, "rating": 10}]})

    asyncio.run(apply_relative_rating(items))

    assert items[1].analysis.relevance_score == 5
    assert items[2].analysis.relevance_score == 4
    assert items[0].analysis.relevance_score > 6


def test_prompt_lists_every_candidate(monkeypatch):
    items = [_item(6, title=f"영상{i}") for i in range(4)]
    calls = _patch_llm(monkeypatch, {"ratings": [{"index": 0, "rating": 5}]})

    asyncio.run(apply_relative_rating(items))

    for i in range(4):
        assert f"[{i}]" in calls["prompt"]
        assert f"영상{i}" in calls["prompt"]
