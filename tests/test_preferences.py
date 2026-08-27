"""👍/👎가 실제로 다음 선정을 바꾸는지 검증한다.

배경: liked_item_ids / disliked_item_ids는 지금까지 쓰기 전용이었다. Telegram
버튼 → Supabase 저장까지는 갔지만 읽는 쪽이 없어서, 버튼을 눌러도 다음날
큐레이션이 그대로였다. 이 테스트들이 그 마지막 한 칸을 지킨다.

실행: pytest tests/test_preferences.py -v
"""

from __future__ import annotations

import json

import pytest

from app.contract import item_id
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    RawContent,
    SourceType,
)
from app.preferences import (
    PreferenceSignal,
    build_item_index,
    build_signal,
    describe_for_prompt,
    preference_score,
    resolve_feedback_target,
)


URL_A = "https://youtu.be/AAA"
URL_B = "https://example.com/blog/post"


@pytest.fixture
def records_dir(tmp_path):
    """정본 두 건짜리 최소 아카이브."""
    record = {
        "date": "2026-08-01",
        "generated_at": "2026-08-01T07:10:00",
        "schema_version": 2,
        "items": [
            {
                "raw": {"url": URL_A, "source_key": "yt_alpha", "source_name": "Alpha"},
                "analysis": {"tags": ["MLOps", "Kubernetes"]},
            },
            {
                "raw": {"url": URL_B, "source_key": "rss_beta", "source_name": "Beta"},
                "analysis": {"tags": ["Causal Inference"]},
            },
        ],
    }
    (tmp_path / "digest_2026-08-01.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


# ──────────────────────────────────────────────
# 색인 · 역참조
# ──────────────────────────────────────────────

def test_index_keyed_by_both_id_and_url(records_dir):
    """id 기반 버튼 이전에 쌓인 URL 피드백도 계속 조회돼야 한다."""
    index = build_item_index(records_dir)

    assert index[item_id(URL_A)].source_key == "yt_alpha"
    assert index[URL_A].source_key == "yt_alpha"


def test_resolve_feedback_target_id_to_url(records_dir):
    index = build_item_index(records_dir)

    assert resolve_feedback_target(item_id(URL_A), index) == URL_A


def test_resolve_feedback_target_unknown_token_passes_through(records_dir):
    """정본에 아직 없는 당일 아이템이면 토큰을 그대로 둔다 — 유실보다 낫다."""
    index = build_item_index(records_dir)

    assert resolve_feedback_target("unknown-token", index) == "unknown-token"


# ──────────────────────────────────────────────
# 신호 일반화
# ──────────────────────────────────────────────

def test_build_signal_generalizes_to_source_and_tags(records_dir):
    """URL 자체는 재등장하지 않으므로 출처·태그로 일반화해야 쓸모가 있다."""
    index = build_item_index(records_dir)

    signal = build_signal([item_id(URL_A)], [URL_B], index)

    assert signal.liked_sources == {"yt_alpha"}
    assert signal.liked_tags == {"mlops", "kubernetes"}
    assert signal.disliked_sources == {"rss_beta"}
    assert signal.disliked_tags == {"causal inference"}


def test_build_signal_drops_contested_signals(records_dir):
    """같은 대상을 👍도 👎도 했다면 중립으로 둔다."""
    index = build_item_index(records_dir)

    signal = build_signal([URL_A], [URL_A], index)

    assert signal.is_empty()


def test_build_signal_empty_profile_needs_no_records():
    """피드백이 없으면 디스크를 건드리지 않고 빈 신호를 돌려준다."""
    assert build_signal([], []).is_empty()


def test_build_signal_ignores_unknown_urls(records_dir):
    index = build_item_index(records_dir)

    assert build_signal(["https://nowhere.example/x"], [], index).is_empty()


# ──────────────────────────────────────────────
# 타이브레이크 점수
# ──────────────────────────────────────────────

def test_preference_score_source_outweighs_single_tag():
    signal = PreferenceSignal(liked_sources={"yt_alpha"}, liked_tags={"mlops"})

    assert preference_score(signal, "yt_alpha", []) == 2
    assert preference_score(signal, "other", ["MLOps"]) == 1


def test_preference_score_is_bounded():
    """취향이 근거를 압도하지 못하도록 ±3으로 묶는다."""
    signal = PreferenceSignal(
        liked_sources={"s"}, liked_tags={"a", "b", "c", "d", "e"}
    )

    assert preference_score(signal, "s", ["a", "b", "c", "d", "e"]) == 3


def test_preference_score_dislike_is_negative():
    signal = PreferenceSignal(disliked_sources={"s"}, disliked_tags={"a"})

    assert preference_score(signal, "s", ["A"]) == -3


def test_preference_score_empty_signal_is_neutral():
    assert preference_score(PreferenceSignal(), "s", ["a", "b"]) == 0


def test_preference_score_tag_match_is_case_insensitive():
    signal = PreferenceSignal(liked_tags={"mlops"})

    assert preference_score(signal, None, ["MLOps"]) == 1


# ──────────────────────────────────────────────
# 프롬프트 문자열
# ──────────────────────────────────────────────

def test_describe_for_prompt_omits_source_names():
    """출처명을 프롬프트에 넣으면 그 채널이 통째로 밀려 올라가 다양성이 죽는다."""
    signal = PreferenceSignal(liked_sources={"yt_alpha"}, liked_tags={"mlops"})

    liked, disliked = describe_for_prompt(signal)

    assert "yt_alpha" not in liked
    assert liked == "mlops"
    assert disliked == "없음"


# ──────────────────────────────────────────────
# 선정 단계 통합
# ──────────────────────────────────────────────

def _digest_item(score: int, source_key: str, tags: list[str]) -> DigestItem:
    raw = RawContent(
        source_type=SourceType.RSS,
        source_name=source_key,
        source_key=source_key,
        title=f"{source_key} 아이템",
        url=f"https://example.com/{source_key}",
        body="본문",
    )
    analysis = ContentAnalysis(
        relevance_score=score,
        one_line_summary="요약",
        tags=tags,
        evidence_level=EvidenceLevel.FULL,
        domain=["ai-ml"],
        content_type="tutorial",
        half_life="durable",
        actionability=score,
        depth=score,
        key_points=[],
        production_ideas=[],
        quiz=[],
    )
    return DigestItem(raw=raw, analysis=analysis)


def test_tiebreak_orders_equal_scores_by_preference():
    """동점일 때만 취향이 순서를 정한다 — 점수는 절대 덮어쓰지 않는다."""
    signal = PreferenceSignal(liked_sources={"liked_src"}, disliked_sources={"hated_src"})
    items = [
        _digest_item(6, "hated_src", []),
        _digest_item(6, "neutral_src", []),
        _digest_item(6, "liked_src", []),
    ]

    items.sort(
        key=lambda d: (
            d.analysis.relevance_score,
            preference_score(signal, d.raw.source_key, d.analysis.tags),
        ),
        reverse=True,
    )

    assert [d.raw.source_key for d in items] == ["liked_src", "neutral_src", "hated_src"]


def test_tiebreak_never_beats_evidence_score():
    """👎한 출처라도 점수가 높으면 👍한 저점 아이템보다 앞서야 한다."""
    signal = PreferenceSignal(liked_sources={"liked_src"}, disliked_sources={"hated_src"})
    items = [
        _digest_item(5, "liked_src", []),
        _digest_item(8, "hated_src", []),
    ]

    items.sort(
        key=lambda d: (
            d.analysis.relevance_score,
            preference_score(signal, d.raw.source_key, d.analysis.tags),
        ),
        reverse=True,
    )

    assert [d.analysis.relevance_score for d in items] == [8, 5]
