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


def _run_filter(monkeypatch, scores, *, floor=4, max_items=5, per_source=5):
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
            max_items_per_source=per_source,
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


# ──────────────────────────────────────────────
# 출처 상한 — 다양성을 채점에 맡기지 않는다
# ──────────────────────────────────────────────
#
# 점수순으로만 자르면 후보를 많이 내는 소스가 다이제스트를 잠식한다. 실측 40일
# 165건에서 HackerNews 한 소스가 32.3%, 상위 3개가 52%였다. 그런데 그게 "HN이
# 다른 소스보다 좋다"는 근거는 아니다 — 후보를 많이 내는 소스가 상위 점수대에
# 더 많이 걸릴 뿐이고, 지금 채점의 변별력(관련도 IQR 1)으로는 그 차이가
# 실질적이라고 볼 수 없다.

from app.analyzer import _select_with_source_cap  # noqa: E402
from app.models import ContentAnalysis as _CA, DigestItem as _DI  # noqa: E402


def _cand(source: str, score: int, n: int = 0) -> _DI:
    return _DI(
        raw=RawContent(
            source_type=SourceType.RSS, source_name=source, source_key=source,
            title=f"{source}{n}", url=f"https://e.com/{source}/{n}",
        ),
        analysis=_CA(
            relevance_score=score, one_line_summary="요약",
            evidence_level=EvidenceLevel.FULL, skip_reason=None,
        ),
    )


def test_source_cap_limits_one_source():
    """한 소스가 다이제스트를 잠식하지 못한다.

    상한을 지키고도 정원을 채울 수 있을 만큼 소스가 있을 때의 동작이다
    (완화가 걸리는 경우는 아래 별도 테스트).
    """
    candidates = (
        [_cand("hn", 9, i) for i in range(5)]
        + [_cand("toss", 5, 0), _cand("toss", 4, 1), _cand("naver", 3, 0)]
    )

    picked = _select_with_source_cap(candidates, limit=5, per_source=2)

    keys = [d.raw.source_key for d in picked]
    assert keys.count("hn") == 2, f"HN이 상한을 넘겼다: {keys}"
    assert set(keys) == {"hn", "toss", "naver"}


def test_source_cap_prefers_higher_scores_within_source():
    """같은 소스 안에서는 여전히 점수순으로 뽑는다."""
    candidates = [_cand("hn", 9, 0), _cand("hn", 8, 1), _cand("hn", 3, 2), _cand("toss", 5)]

    picked = _select_with_source_cap(candidates, limit=3, per_source=2)

    assert [d.analysis.relevance_score for d in picked] == [9, 8, 5]


def test_source_cap_relaxes_rather_than_shrinking_digest():
    """상한을 지키면 다이제스트가 짧아지는 경우, 상한보다 분량을 택한다.

    약한 날 다이제스트를 짧게 만드는 것이 편중보다 나쁘다 —
    §3.4가 상대 랭킹으로 해결한 바로 그 문제다.
    """
    candidates = [_cand("hn", 9 - i, i) for i in range(5)]

    picked = _select_with_source_cap(candidates, limit=5, per_source=2)

    assert len(picked) == 5
    assert [d.analysis.relevance_score for d in picked] == [9, 8, 7, 6, 5]


def test_source_cap_disabled_when_zero():
    candidates = [_cand("hn", 9, i) for i in range(5)]

    assert len(_select_with_source_cap(candidates, limit=3, per_source=0)) == 3


def test_source_cap_end_to_end(monkeypatch):
    """filter_and_analyze 전체를 통과해도 상한이 적용된다."""
    result = _run_filter(
        monkeypatch, [9, 9, 9, 9, 9], max_items=5, per_source=2,
    )
    # _run_filter의 아이템은 전부 source_name="src" 하나다 → 상한에 걸리지만
    # 분량을 위해 완화돼 5건이 유지돼야 한다.
    assert len(result) == 5
