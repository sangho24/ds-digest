"""
filter_and_analyze 상대 랭킹 회귀 테스트 (v2 §3.4/§11)

절대 문턱(relevance_threshold=7)으로 거르던 방식은 약한 날 빈 다이제스트를 냈다.
근거 게이트가 얇은 근거를 정직하게 캡하므로 대부분 7점 미만이 되어, 6/2/5점만
나온 날에는 전부 탈락했다(실측). 이 테스트들은 상대 랭킹(상위 K + 낮은 바닥값)이
그 시나리오에서도 발송 후보를 남기는지 검증한다.

실제 LLM은 호출하지 않는다 — analyze_content를 통제된 ContentAnalysis로 mock한다.
pytest-asyncio가 없으므로 asyncio.run()으로 코루틴을 돌린다.
"""
import asyncio
from types import SimpleNamespace

import app.analyzer as analyzer
from app.models import (
    ContentAnalysis,
    EvidenceLevel,
    RawContent,
    SourceType,
    UserProfile,
)


async def _noop_sleep(_seconds):
    """rate-limit 대기가 테스트를 실제로 지연시키지 않도록 한다."""
    return None


def _item(idx: int) -> RawContent:
    return RawContent(
        source_type=SourceType.RSS,
        source_name="src",
        title=f"item{idx}",
        url=f"https://example.com/{idx}",
        body="본문",
    )


def _run_filter(monkeypatch, scores, *, floor=4, max_items=5):
    """scores 순서대로 relevance_score를 부여한 아이템을 filter_and_analyze에 통과시킨다."""
    items = [_item(i) for i in range(len(scores))]
    score_by_url = {item.url: score for item, score in zip(items, scores)}

    async def _fake_analyze(item, profile, **_kwargs):
        return ContentAnalysis(
            relevance_score=score_by_url[item.url],
            one_line_summary="요약",
            evidence_level=EvidenceLevel.FULL,
            skip_reason=None,
        )

    monkeypatch.setattr(analyzer, "analyze_content", _fake_analyze)
    monkeypatch.setattr(
        analyzer,
        "get_settings",
        lambda: SimpleNamespace(
            dry_run=False,
            groq_api_key="k",
            relevance_floor=floor,
            max_items_per_digest=max_items,
        ),
    )
    monkeypatch.setattr(analyzer.asyncio, "sleep", _noop_sleep)

    result = asyncio.run(analyzer.filter_and_analyze(items, UserProfile()))
    return [d.analysis.relevance_score for d in result]


def test_floor_excludes_low_quality(monkeypatch):
    """바닥값(4) 미만은 제외하고, 이상인 후보만 남긴다."""
    scores = _run_filter(monkeypatch, [3, 5, 4, 2, 7], floor=4, max_items=5)

    # 3, 2는 바닥값 미만이라 제외 → 7, 5, 4만 (점수순 내림차순)
    assert scores == [7, 5, 4]
    assert all(s >= 4 for s in scores)


def test_top_k_caps_and_sorts_desc(monkeypatch):
    """후보가 max_items_per_digest보다 많으면 상위 K건만, 높은 점수순으로 반환한다."""
    scores = _run_filter(monkeypatch, [9, 8, 7, 6, 5, 4], floor=4, max_items=3)

    assert len(scores) == 3
    assert scores == [9, 8, 7]


def test_weak_day_still_produces_digest(monkeypatch):
    """약한 날 [6, 2, 5] + 바닥값 4 → 빈 다이제스트가 아니라 2건(6, 5)을 발송한다.

    이번 전환이 고치려는 정확한 프로덕션 시나리오다(절대 문턱 7이었으면 전량 탈락).
    """
    scores = _run_filter(monkeypatch, [6, 2, 5], floor=4, max_items=5)

    assert scores == [6, 5]
    assert scores != []


def test_all_below_floor_returns_empty(monkeypatch):
    """바닥값을 넘는 후보가 하나도 없을 때만 빈 결과가 된다."""
    scores = _run_filter(monkeypatch, [1, 2, 3], floor=4, max_items=5)

    assert scores == []
