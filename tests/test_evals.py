"""품질 계측 지표의 단위 테스트."""

from __future__ import annotations

import pytest

from evals.metrics import (
    duplicate_rate,
    evidence_proxy,
    schema_rigidity,
    score_distribution,
    source_reach,
    summary_stats,
    tag_concentration,
    tag_entropy,
)


def _item(**overrides):
    item = {
        "date": "2026-01-01",
        "source_name": "source-a",
        "title": "제목",
        "url": "https://example.com/default",
        "relevance": 5,
        "tags": ["A"],
        "one_line_summary": "요약",
        "key_points": ["핵심"],
        "production_ideas": ["적용"],
        "quiz_count": 1,
        "has_timestamp": False,
    }
    item.update(overrides)
    return item


def test_tag_entropy_and_concentration_use_the_right_denominators():
    items = [
        _item(tags=["A", "A"]),
        _item(tags=["B"]),
    ]

    entropy = tag_entropy(items)
    concentration = tag_concentration(items)

    # 엔트로피는 태그 부착 3회, 집중도는 아이템별 중복을 제거한 1/2를 센다.
    assert entropy["tag_counts"] == {"A": 2, "B": 1}
    assert entropy["entropy_bits"] == pytest.approx(0.918296)
    assert entropy["normalized_entropy"] == pytest.approx(0.918296)
    assert concentration["top_tag"] == "A"
    assert concentration["top_tag_item_count"] == 1
    assert concentration["concentration"] == 0.5


def test_items_without_tags_are_handled():
    items = [_item(tags=[]), _item(tags=None)]

    assert tag_entropy(items)["total_assignments"] == 0
    assert tag_entropy(items)["entropy_bits"] == 0.0
    assert tag_concentration(items)["top_tag"] is None
    assert tag_concentration(items)["concentration"] == 0.0


def test_score_distribution_has_population_stddev_and_interpolated_iqr():
    items = [_item(relevance=score) for score in [1, 2, 3, 4]]

    result = score_distribution(items)

    assert result["histogram"] == {"1": 1, "2": 1, "3": 1, "4": 1}
    assert result["mean"] == 2.5
    assert result["stddev"] == pytest.approx(1.118034)
    assert result["q1"] == 1.75
    assert result["q3"] == 3.25
    assert result["iqr"] == 1.5
    assert result["distinct_values"] == 4


def test_source_reach_includes_boundary_day_as_stale():
    items = [
        _item(date="2026-01-01", source_name="old"),
        _item(date="2026-01-31", source_name="new"),
        _item(date="2026-01-30", source_name="new"),
    ]

    result = source_reach(items, window_days=30)

    assert result["reference_date"] == "2026-01-31"
    assert result["source_count"] == 2
    assert result["sources"]["new"]["item_count"] == 2
    assert result["stale_source_count"] == 1
    assert result["stale_sources"][0]["source_name"] == "old"
    assert result["stale_sources"][0]["days_since_last_seen"] == 30


def test_source_reach_rejects_negative_window():
    with pytest.raises(ValueError, match="0 이상"):
        source_reach([], window_days=-1)


def test_duplicate_rate_and_consecutive_reappearance_intervals():
    items = [
        _item(date="2026-01-01", url="https://example.com/a"),
        _item(date="2026-01-11", url="https://example.com/a"),
        _item(date="2026-01-21", url="https://example.com/a"),
        _item(date="2026-01-05", url="https://example.com/b"),
    ]

    result = duplicate_rate(items)

    assert result["unique_urls"] == 2
    assert result["duplicate_urls"] == 1
    assert result["duplicate_occurrences"] == 2
    assert result["duplicate_url_rate"] == 0.5
    assert result["reappearance_intervals_days"]["histogram"] == {"10": 2}
    assert result["reappearance_intervals_days"]["mean"] == 10.0


def test_evidence_proxy_recognizes_youtube_hosts_only():
    items = [
        _item(url="https://youtu.be/one", has_timestamp=True),
        _item(url="https://www.youtube.com/watch?v=two", has_timestamp=False),
        _item(url="https://notyoutube.com/watch?v=three", has_timestamp=True),
    ]

    result = evidence_proxy(items)

    assert result == {"youtube_items": 2, "with_timestamp": 1, "timestamp_rate": 0.5}


def test_schema_rigidity_measures_dominant_counts_not_text_identity():
    items = [
        _item(production_ideas=["a", "b"], quiz_count=2),
        _item(production_ideas=["c", "d"], quiz_count=2),
        _item(production_ideas=["e"], quiz_count=2),
    ]

    result = schema_rigidity(items)

    ideas = result["production_ideas_count"]
    quizzes = result["quiz_count"]
    assert ideas["dominant_value"] == 2
    assert ideas["fixed_ratio"] == pytest.approx(0.666667)
    assert quizzes["dominant_value"] == 2
    assert quizzes["fixed_ratio"] == 1.0
    assert result["combined_fixed_values"]["fixed_ratio"] == pytest.approx(0.666667)


def test_summary_stats_measure_characters_per_entry_and_item():
    items = [
        _item(one_line_summary="abcd", key_points=["a", "bbb"], production_ideas=["xy"]),
        _item(one_line_summary="xy", key_points=["cc"], production_ideas=[]),
    ]

    result = summary_stats(items)

    assert result["one_line_summary"]["mean"] == 3.0
    assert result["key_points"]["entries_per_item"]["mean"] == 1.5
    assert result["key_points"]["characters_per_entry"]["mean"] == 2.0
    assert result["key_points"]["characters_per_item"]["mean"] == 3.0
    assert result["production_ideas"]["characters_per_item"]["mean"] == 1.0


def test_empty_list_and_single_item_boundaries():
    empty_results = [
        tag_entropy([]),
        tag_concentration([]),
        score_distribution([]),
        source_reach([], 30),
        duplicate_rate([]),
        evidence_proxy([]),
        schema_rigidity([]),
        summary_stats([]),
    ]
    assert all(isinstance(result, dict) for result in empty_results)
    assert score_distribution([])["mean"] is None
    assert duplicate_rate([])["duplicate_url_rate"] == 0.0
    assert summary_stats([])["one_line_summary"]["count"] == 0

    single_score = score_distribution([_item(relevance=7)])
    assert single_score["stddev"] == 0.0
    assert single_score["iqr"] == 0.0
    assert duplicate_rate([_item(url="https://example.com/only")])["duplicate_occurrences"] == 0

