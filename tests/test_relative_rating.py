"""상대 평가 — 절대 채점의 중앙 몰림을 깨는 단계 (PROGRESS 항목 D).

실측(164건): actionability의 73%, depth의 74%가 5~6점이고 relevance IQR이 1이었다.
점수가 뭉치면 후보 30여 건에서 상위 5건을 고르는 일이 사실상 동전 던지기가 된다.
프롬프트로 점수 정의를 조이는 시도는 이미 한 번 실패했다(v2 §1.1).

실행: pytest tests/test_relative_rating.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from types import SimpleNamespace

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


# ──────────────────────────────────────────────
# 바닥값은 절대 점수에 걸려야 한다
# ──────────────────────────────────────────────
#
# 예비 점검에서 잡힌 회귀: 상대 평가가 섞인 relevance_score에 바닥값을 걸면,
# 상대 비교가 순서를 정하는 게 아니라 아이템을 탈락시킨다. 후보가 전부 약한
# 날엔 모델이 시킨 대로 낮은 점수를 뿌리므로 대량 탈락이 일어나 다이제스트가
# 짧아진다 — §3.4가 상대 랭킹을 도입한 목적과 정면으로 충돌한다.

from app.analyzer import filter_and_analyze  # noqa: E402
from app.models import UserProfile  # noqa: E402


def test_low_relative_rating_does_not_drop_item(monkeypatch):
    """상대 평가에서 최하점을 받아도 절대 품질이 바닥값 이상이면 후보로 남는다."""
    items = [_item(6, title=f"글{i}") for i in range(3)]
    raws = [d.raw for d in items]
    analyses = {d.raw.url: d.analysis for d in items}

    async def _analyze(item, profile, **_kw):
        return analyses[item.url]

    monkeypatch.setattr(analyzer, "analyze_content", _analyze)
    monkeypatch.setattr(analyzer, "load_vocabulary", lambda *a, **k: {"concepts": {}})
    monkeypatch.setattr(analyzer, "save_vocabulary", lambda *a, **k: None)
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())
    monkeypatch.setattr(analyzer, "get_settings", lambda: SimpleNamespace(
        dry_run=False, groq_api_key="k", relevance_floor=4, max_items_per_digest=5,
        max_items_per_source=5,
    ))
    _patch_llm(monkeypatch, {"ratings": [
        {"index": 0, "rating": 10}, {"index": 1, "rating": 5}, {"index": 2, "rating": 1},
    ]})

    result = asyncio.run(filter_and_analyze(raws, UserProfile()))

    # 최하점(1)을 받은 아이템도 절대 점수 6 >= floor 4 이므로 살아남아야 한다.
    assert len(result) == 3
    scores = [d.analysis.relevance_score for d in result]
    assert scores == sorted(scores, reverse=True)
    assert max(scores) - min(scores) >= 4


def test_absolute_low_quality_is_still_dropped(monkeypatch):
    """반대로 절대 품질이 바닥값 미만이면 상대 만점을 받아도 탈락한다."""
    weak = _item(0, title="약한글")
    weak.analysis.actionability = 1
    weak.analysis.depth = 1
    strong = _item(6, title="센글")
    raws = [weak.raw, strong.raw]
    analyses = {weak.raw.url: weak.analysis, strong.raw.url: strong.analysis}

    async def _analyze(item, profile, **_kw):
        return analyses[item.url]

    monkeypatch.setattr(analyzer, "analyze_content", _analyze)
    monkeypatch.setattr(analyzer, "load_vocabulary", lambda *a, **k: {"concepts": {}})
    monkeypatch.setattr(analyzer, "save_vocabulary", lambda *a, **k: None)
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())
    monkeypatch.setattr(analyzer, "get_settings", lambda: SimpleNamespace(
        dry_run=False, groq_api_key="k", relevance_floor=4, max_items_per_digest=5,
        max_items_per_source=5,
    ))
    _patch_llm(monkeypatch, {"ratings": [{"index": 0, "rating": 10}]})

    result = asyncio.run(filter_and_analyze(raws, UserProfile()))

    assert [d.raw.url for d in result] == [strong.raw.url]
