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
            source_diversity_strength=1.0,
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
# 출처 다양성 정규화
# ──────────────────────────────────────────────
#
# 편중의 원인은 품질이 아니라 물량이다. 후보를 많이 내는 소스가 상위 점수대에 더
# 많이 걸릴 뿐이다 — 실측 40일 165건에서 HackerNews 한 소스가 32.3%를 가져갔는데,
# 수집기에 상한이 없어 키워드 4개 × 20건이 후보 풀에 들어왔기 때문이다.
#
# 하드 상한 대신 후보 풀 물량에 비례해 반복 선택을 감점한다.

from app.analyzer import _select_diverse  # noqa: E402
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


def _pick(candidates, limit=5, per_source=3, strength=1.0):
    candidates = sorted(candidates, key=lambda d: d.analysis.relevance_score, reverse=True)
    return [d.raw.source_key for d in
            _select_diverse(candidates, limit=limit, per_source=per_source, strength=strength)]


def test_high_volume_source_is_normalized():
    """물량이 많은 소스는 반복 선택이 눌린다."""
    pool = ([_cand("hn", 9, i) for i in range(20)]
            + [_cand("toss", 8), _cand("blog", 7), _cand("news", 6)])

    keys = _pick(pool, limit=4)

    assert keys.count("hn") == 1, f"물량 많은 소스가 여전히 잠식: {keys}"
    assert len(set(keys)) == 4


def test_first_pick_from_any_source_is_unpenalized():
    """첫 한 건은 감점이 없다 — 어느 소스든 한 번은 공정하게 경쟁한다."""
    pool = [_cand("hn", 9, i) for i in range(20)] + [_cand("toss", 3)]

    keys = _pick(pool, limit=2)

    assert keys[0] == "hn", "최고점 아이템이 물량 때문에 밀리면 안 된다"


def test_low_volume_source_can_take_multiple():
    """물량 이점이 없으면 여러 건을 가져갈 수 있다 — 하드 상한과 다른 점이다."""
    pool = [_cand("a", 9, 0), _cand("a", 9, 1), _cand("b", 4, 0), _cand("c", 3, 0)]

    keys = _pick(pool, limit=2)

    assert keys == ["a", "a"]


def test_strength_zero_is_pure_score_order():
    pool = [_cand("hn", 9, i) for i in range(5)] + [_cand("toss", 8)]

    keys = _pick(pool, limit=3, per_source=99, strength=0.0)

    assert keys == ["hn", "hn", "hn"]


def test_hard_cap_is_a_backstop():
    """정규화가 약해도 한 소스가 다이제스트를 통째로 먹지는 못한다."""
    pool = [_cand("hn", 9, i) for i in range(10)]

    keys = _pick(pool, limit=5, per_source=3, strength=0.0)

    # 상한 3을 넘어야 정원이 차므로 완화되지만, 완화 없이는 3건까지다.
    assert keys.count("hn") == 5  # 다른 소스가 없으면 완화된다


def test_cap_relaxes_rather_than_shrinking_digest():
    """상한을 지키면 정원을 못 채우는 날에는 상한보다 분량을 택한다.

    약한 날 다이제스트를 짧게 만드는 것이 편중보다 나쁘다 —
    §3.4가 상대 랭킹으로 해결한 바로 그 문제다.
    """
    pool = [_cand("hn", 9 - i, i) for i in range(5)]

    keys = _pick(pool, limit=5, per_source=2)

    assert len(keys) == 5


def test_output_is_ordered_by_original_score():
    """발송 순서는 사람이 읽는 순서다 — 조정점수가 아니라 원래 점수순이어야 한다."""
    pool = [_cand("hn", 9, i) for i in range(5)] + [_cand("toss", 5), _cand("blog", 4)]
    candidates = sorted(pool, key=lambda d: d.analysis.relevance_score, reverse=True)

    picked = _select_diverse(candidates, limit=3, per_source=3, strength=1.0)
    scores = [d.analysis.relevance_score for d in picked]

    assert scores == sorted(scores, reverse=True)


def test_empty_candidates():
    assert _select_diverse([], limit=5, per_source=2, strength=1.0) == []


def test_diversity_end_to_end(monkeypatch):
    """filter_and_analyze 전체를 통과해도 정원이 유지된다."""
    result = _run_filter(monkeypatch, [9, 9, 9, 9, 9], max_items=5, per_source=3)

    assert len(result) == 5


# ── 계열 정규화 ────────────────────────────────────────────────────────────
# arXiv를 14개 카테고리로 늘리자 source_key가 `arxiv:cs.LG`처럼 갈라졌다.
# 퍼널에는 그게 맞지만, 정규화까지 그 키로 세면 arXiv 16건이 13개의 작은
# 소스로 보여 감점도 안전판도 걸리지 않는다. 실측 시뮬레이션에서 논문이
# 계통적으로 2점 높게 채점되자 다이제스트 5칸이 전부 arXiv로 찼다.

from app.analyzer import source_family  # noqa: E402


def test_source_family_folds_only_prefixed_keys():
    assert source_family("arxiv:cs.LG") == "arxiv"
    assert source_family("arxiv:stat.ML") == "arxiv"
    assert source_family("hackernews") == "hackernews"
    # 피드 URL을 콜론으로 자르면 전부 https 한 덩어리가 된다 — 정반대 버그다.
    assert source_family("https://toss.tech/rss.xml") == "https://toss.tech/rss.xml"
    assert source_family("http://a.com/feed") == "http://a.com/feed"
    assert source_family("innoforest.stibee.com") == "innoforest.stibee.com"
    assert source_family("") == ""


def test_arxiv_categories_are_normalized_as_one_family():
    """카테고리로 쪼개도 arXiv 계열 전체가 다이제스트를 먹지 못한다."""
    cats = ["cs.LG", "stat.ML", "cs.CL", "cs.AI", "cs.IR", "cs.DB",
            "cs.DC", "cs.SE", "stat.ME", "econ.EM", "stat.AP", "cs.HC", "cs.CV"]
    # 논문이 계통적으로 높게 채점되는 상황(depth가 높아 실제로 그럴 만하다).
    pool = [_cand(f"arxiv:{c}", 9, i) for i, c in enumerate(cats)]
    pool += [_cand("hn", 7, i) for i in range(10)]
    pool += [_cand("toss", 7), _cand("netflix", 7), _cand("yt", 7)]

    keys = _pick(pool, limit=5)
    arxiv = [k for k in keys if k.startswith("arxiv:")]
    assert len(arxiv) <= 2, keys           # 계열로 세지 않으면 5/5가 된다
    assert len(set(keys)) >= 4, keys       # 나머지 칸은 다른 계열이 채운다
    # 그래도 가장 좋은 논문 한 편은 들어간다 — 억제가 아니라 반복만 막는 것이다.
    assert arxiv, keys


def test_family_cap_applies_across_categories():
    """안전판(per_source)도 계열 단위로 센다 — 카테고리마다 따로 세지 않는다."""
    pool = [_cand(f"arxiv:c{i}", 9, i) for i in range(10)]
    pool += [_cand("hn", 4, i) for i in range(10)]
    # 정원 4 = 계열 2개 × 상한 2. 완화 없이 채울 수 있는 크기다.
    keys = _pick(pool, limit=4, per_source=2, strength=0.0)
    assert len([k for k in keys if k.startswith("arxiv:")]) == 2, keys


def test_family_cap_relaxes_rather_than_shorten_digest():
    """모든 계열이 상한에 닿으면 상한을 풀어 정원을 채운다.

    빈 자리를 남기는 것이 편중보다 나쁘다(§3.4). 다만 완화된 뒤에도 물량
    정규화는 계속 걸리므로, 상한만 풀릴 뿐 편중으로 되돌아가지는 않는다.
    """
    pool = [_cand(f"arxiv:c{i}", 9, i) for i in range(10)]
    pool += [_cand("hn", 8, i) for i in range(10)]
    keys = _pick(pool, limit=6, per_source=2, strength=1.0)
    assert len(keys) == 6, keys                       # 짧아지지 않는다
    arxiv = len([k for k in keys if k.startswith("arxiv:")])
    assert 2 <= arxiv <= 4, keys                      # 한 계열이 독식하지 않는다
