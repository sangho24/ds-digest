"""품질 계측 지표의 단위 테스트."""

from __future__ import annotations

import json

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



# ──────────────────────────────────────────────
# 정본 → 계측 입력 파생 (Weekly Evals 5/5 실패 회귀)
# ──────────────────────────────────────────────
#
# Weekly Evals는 만들어진 이래(2026-07-27~08-24) 5번 실행해 5번 전부 exit 2로
# 실패했다. run.py가 evals/data/archive_items.json을 읽는데 evals/data/ 는
# .gitignore 대상이라 CI엔 절대 없었기 때문이다. 커밋되는 data/records/에서
# 입력을 만들 수 있어야 게이트가 실제로 동작한다.

from evals.build_items import build_items, flatten_record  # noqa: E402
from evals import run as evals_run  # noqa: E402


def _record(date="2026-08-01", **overrides):
    analysis = {
        "relevance_score": 7,
        "one_line_summary": "요약 문장",
        "tags": ["MLOps", "LLM"],
        "key_points": [{"point": "핵심", "timestamp": "01:23"}],
        "production_ideas": ["아이디어1", "아이디어2"],
        "quiz": [{"question": "q"}],
    }
    analysis.update(overrides.pop("analysis", {}))
    return {
        "date": date,
        "generated_at": f"{date}T07:10:00",
        "schema_version": 2,
        "items": [
            {
                "raw": {
                    "url": "https://youtu.be/VID1",
                    "source_key": "yt_channel",
                    "source_name": "채널",
                },
                "analysis": analysis,
            }
        ],
    }


def test_flatten_record_maps_metric_fields():
    """metrics.py가 읽는 키가 전부 채워져야 한다."""
    items = flatten_record(_record())

    assert len(items) == 1
    item = items[0]
    assert item["date"] == "2026-08-01"
    assert item["url"] == "https://youtu.be/VID1"
    assert item["source_key"] == "yt_channel"
    assert item["tags"] == ["MLOps", "LLM"]
    assert item["relevance"] == 7
    assert item["has_timestamp"] is True
    assert item["quiz_count"] == 1
    assert item["one_line_summary"] == "요약 문장"


def test_flatten_record_timestamp_absent_is_false():
    """자막이 없으면 timestamp는 null이 정상 — has_timestamp가 False여야 한다."""
    record = _record(analysis={"key_points": [{"point": "핵심", "timestamp": None}]})

    assert flatten_record(record)[0]["has_timestamp"] is False


def test_build_items_reads_records_dir(tmp_path):
    """날짜순으로 모든 레코드를 편다."""
    for date in ("2026-08-02", "2026-08-01"):
        (tmp_path / f"digest_{date}.json").write_text(
            json.dumps(_record(date), ensure_ascii=False), encoding="utf-8"
        )

    items = build_items(tmp_path)

    assert [i["date"] for i in items] == ["2026-08-01", "2026-08-02"]


def test_build_items_skips_broken_file(tmp_path):
    """깨진 파일 하나가 계측 전체를 막으면 안 된다."""
    (tmp_path / "digest_2026-08-01.json").write_text(
        json.dumps(_record("2026-08-01"), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "digest_2026-08-02.json").write_text("{ 깨짐", encoding="utf-8")

    items = build_items(tmp_path)

    assert [i["date"] for i in items] == ["2026-08-01"]


def test_build_items_missing_dir_returns_empty(tmp_path):
    assert build_items(tmp_path / "없음") == []


def test_resolve_items_prefers_snapshot(monkeypatch, tmp_path):
    """스냅샷이 있으면 그것을 쓴다 — baseline과 같은 계측 창을 유지해야 한다."""
    snapshot = tmp_path / "archive_items.json"
    snapshot.write_text(json.dumps([_item(date="2026-05-01")]), encoding="utf-8")
    monkeypatch.setattr(evals_run, "DATA_PATH", snapshot)

    items, path = evals_run.resolve_items()

    assert items[0]["date"] == "2026-05-01"
    assert "archive_items.json" in path


def test_resolve_items_falls_back_to_records(monkeypatch, tmp_path):
    """스냅샷이 없으면 커밋된 정본에서 만든다 — 이게 CI에서 매주 죽던 지점이다."""
    monkeypatch.setattr(evals_run, "DATA_PATH", tmp_path / "없음.json")
    monkeypatch.setattr(evals_run, "build_items", lambda: [_item(date="2026-08-01")])

    items, path = evals_run.resolve_items()

    assert items[0]["date"] == "2026-08-01"
    assert "data/records/" in path


def test_resolve_items_raises_when_nothing_available(monkeypatch, tmp_path):
    """입력이 정말 하나도 없으면 조용히 0건으로 통과시키지 말고 오류를 낸다."""
    monkeypatch.setattr(evals_run, "DATA_PATH", tmp_path / "없음.json")
    monkeypatch.setattr(evals_run, "build_items", lambda: [])

    with pytest.raises(ValueError):
        evals_run.resolve_items()


# ──────────────────────────────────────────────
# 게이트 판정 — WARN은 실패시키지 않는다
# ──────────────────────────────────────────────
#
# severity 필드가 있는데 exit_code가 violations 전체를 보고 있어서, WARN 하나가
# 잡을 영구히 빨간불로 묶었다. 그러면 "실패"가 신호가 아니라 소음이 되고
# 아무도 로그를 열지 않게 된다.

from evals.notify import format_report  # noqa: E402


def _threshold_rows(monkeypatch, rules):
    monkeypatch.setattr(evals_run, "THRESHOLDS", rules)


def test_warn_violation_does_not_block(monkeypatch):
    _threshold_rows(monkeypatch, [
        {"path": "a.b", "operator": ">", "value": 0, "severity": "WARN", "label": "경고지표"},
    ])

    rows, violations = evals_run.evaluate_thresholds({"a": {"b": 1}})

    assert violations[0]["status"] == "WARN"
    assert [r for r in violations if r["status"] == "FAIL"] == []


def test_fail_violation_blocks(monkeypatch):
    _threshold_rows(monkeypatch, [
        {"path": "a.b", "operator": ">", "value": 0, "severity": "FAIL", "label": "치명지표"},
    ])

    _, violations = evals_run.evaluate_thresholds({"a": {"b": 1}})

    assert [r["status"] for r in violations] == ["FAIL"]


# ──────────────────────────────────────────────
# 알림 문구
# ──────────────────────────────────────────────

def _report(**over):
    base = {
        "input": {"path": "data/records/ (파생)", "item_count": 163,
                  "start_date": "2026-07-22", "end_date": "2026-08-30"},
        "blocking_violations": [],
        "regressions": [],
        "violations": [],
    }
    base.update(over)
    return base


def test_format_report_lists_each_category():
    text = format_report(_report(
        blocking_violations=[{"label": "타임스탬프", "actual": 0.0,
                              "operator": "<", "threshold": 0.7}],
        regressions=[{"label": "요약 길이", "current": 28.1, "baseline": 32.4}],
        violations=[{"label": "미등장 소스", "actual": 1, "status": "WARN"}],
    ))

    assert "❌ 타임스탬프" in text
    assert "📉 요약 길이" in text
    assert "⚠️ 미등장 소스" in text
    assert "163건" in text


def test_format_report_says_so_when_nothing_found():
    """위반이 없는데 실패했다면 실행 자체가 깨진 것이다 — 빈 메시지를 보내면 안 된다."""
    text = format_report(_report())

    assert "실행 자체가 실패" in text


def test_format_report_omits_pass_rows():
    """PASS 행은 WARN 목록에 섞이면 안 된다."""
    text = format_report(_report(
        violations=[{"label": "통과지표", "actual": 1, "status": "PASS"}],
    ))

    assert "통과지표" not in text


# ──────────────────────────────────────────────
# 계측 창 (scope) — 고쳐진 버그가 영원히 FAIL로 남지 않게
# ──────────────────────────────────────────────
# 실측: YouTube 타임스탬프가 2026-09-01에 고쳐졌는데도 전체 42일 평균이 1/55라
# 게이트는 8/17부터 매주 빨간불이었다. 창이 계속 커지므로 저절로 회복되지 않는다.

def _recent_rules(monkeypatch, rules):
    monkeypatch.setattr(evals_run, "THRESHOLDS", rules)


def test_recent_scope_is_judged_on_the_window_not_lifetime(monkeypatch):
    _recent_rules(monkeypatch, [
        {"scope": "recent", "path": "m.v", "operator": "<", "value": 1.5,
         "severity": "FAIL", "label": "현재동작"},
    ])
    lifetime = {"m": {"v": 1.0}}     # 전체로 보면 위반
    recent = {"m": {"v": 2.0}}       # 최근만 보면 정상

    rows, violations = evals_run.evaluate_thresholds(lifetime, recent, recent_item_count=50)

    assert violations == []
    assert rows[0]["actual"] == 2.0
    assert rows[0]["lifetime_actual"] == 1.0, "전체 값도 함께 보여야 진단이 된다"


def test_lifetime_scope_ignores_the_window(monkeypatch):
    """중복 재유입 같은 지표는 정의상 긴 창이 필요하다."""
    _recent_rules(monkeypatch, [
        {"path": "m.v", "operator": ">", "value": 0.05, "severity": "FAIL", "label": "누적자산"},
    ])
    rows, violations = evals_run.evaluate_thresholds(
        {"m": {"v": 0.2}}, {"m": {"v": 0.0}}, recent_item_count=50,
    )
    assert [r["status"] for r in violations] == ["FAIL"]
    assert rows[0]["actual"] == 0.2 and rows[0]["scope"] == "lifetime"


def test_thin_window_downgrades_recent_fail_to_warn(monkeypatch):
    """근거가 모자랄 때의 답은 '문제 없음'이 아니라 '판단할 근거가 없음'이다(§31)."""
    _recent_rules(monkeypatch, [
        {"scope": "recent", "path": "m.v", "operator": "<", "value": 1.5,
         "severity": "FAIL", "label": "현재동작"},
    ])
    _, violations = evals_run.evaluate_thresholds(
        {"m": {"v": 1.0}}, {"m": {"v": 1.0}},
        recent_item_count=evals_run.MIN_ITEMS_FOR_RECENT_GATE - 1,
    )
    assert [v["status"] for v in violations] == ["WARN"], "표본 부족이면 게이트를 막지 않는다"

    # 표본이 충분하면 그대로 FAIL이다 — 보류가 영구 면제가 되면 안 된다.
    _, violations = evals_run.evaluate_thresholds(
        {"m": {"v": 1.0}}, {"m": {"v": 1.0}},
        recent_item_count=evals_run.MIN_ITEMS_FOR_RECENT_GATE,
    )
    assert [v["status"] for v in violations] == ["FAIL"]


def test_evaluate_thresholds_without_recent_metrics_is_unchanged(monkeypatch):
    """recent_metrics를 안 주면 예전과 똑같이 전체 구간으로만 판정한다."""
    _recent_rules(monkeypatch, [
        {"scope": "recent", "path": "m.v", "operator": "<", "value": 1.5,
         "severity": "FAIL", "label": "현재동작"},
    ])
    _, violations = evals_run.evaluate_thresholds({"m": {"v": 1.0}})
    assert [v["status"] for v in violations] == ["FAIL"]


def test_recent_window_starts_from_last_data_day_not_today():
    """파이프라인이 멈춰도 창이 비지 않아야 한다 — 비면 멈춤을 알릴 지표가 조용해진다."""
    items = [{"date": "2026-01-01"}, {"date": "2026-01-10"}, {"date": "2026-01-14"}]
    since = evals_run._recent_since(items, days=5)
    assert since.isoformat() == "2026-01-10"     # 마지막 날(01-14) 기준 5일 창
    assert evals_run._recent_since([], days=5) is None
    assert evals_run._recent_since([{"date": "깨짐"}], days=5) is None
