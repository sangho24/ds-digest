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


# ──────────────────────────────────────────────
# baseline 회귀 비교 - 8주 연속 실패의 마지막 원인 (PROGRESS §39)
# ──────────────────────────────────────────────
# 2026-09-07·09-14 게이트를 막은 유일한 회귀는 한 줄 요약 평균 28.56 < baseline 32.38
# 이었다. 프롬프트는 "30자 이내"를 지시하므로 짧아진 것은 개선이다. 결함은 넷이었다.
#   1. 방향이 틀린 지표(요약 길이)   2. recent 지표도 전체 구간끼리 비교
#   3. 허용오차 0                    4. THRESHOLDS가 WARN인데 회귀는 무조건 FAIL

from evals.thresholds import BASELINE_COMPARISONS, THRESHOLDS  # noqa: E402

SUMMARY_PATH = "summary_stats.one_line_summary.mean"
DERIVED = "data/records/ (파생)"


def test_summary_length_shortening_is_not_a_regression():
    """실제 규칙으로: 32.38자 → 28.56자가 회귀로 잡히면 안 된다."""
    current = {"summary_stats": {"one_line_summary": {"mean": 28.561404}}}
    baseline = {"summary_stats": {"one_line_summary": {"mean": 32.380567}}}

    regressions = evals_run.compare_baseline(current, baseline)

    assert [r for r in regressions if r["metric"] == SUMMARY_PATH] == []
    assert SUMMARY_PATH not in [rule["path"] for rule in BASELINE_COMPARISONS]


def test_summary_length_over_prompt_limit_is_warn(monkeypatch):
    """대신 프롬프트 상한(30자)을 넘으면 WARN으로 보고한다. 하한(20자) 행과 공존한다."""
    rows = [r for r in THRESHOLDS if r["path"] == SUMMARY_PATH]
    assert {(r["operator"], r["value"], r["severity"]) for r in rows} == {
        ("<", 20, "WARN"), (">", 30, "WARN"),
    }

    monkeypatch.setattr(evals_run, "THRESHOLDS", rows)
    metrics = {"summary_stats": {"one_line_summary": {"mean": 31.0}}}
    _, violations = evals_run.evaluate_thresholds(metrics, metrics, recent_item_count=50)
    assert [(v["operator"], v["status"]) for v in violations] == [(">", "WARN")]


def test_every_baseline_comparison_has_a_threshold_policy():
    """scope·severity를 THRESHOLDS에서 가져오므로 대응 행이 반드시 있어야 한다."""
    for rule in BASELINE_COMPARISONS:
        scope, severity = evals_run._threshold_policy(rule["path"])
        assert scope in ("recent", "lifetime") and severity in ("WARN", "FAIL")


def _baseline_rules(monkeypatch, thresholds, comparisons):
    monkeypatch.setattr(evals_run, "THRESHOLDS", thresholds)
    monkeypatch.setattr(evals_run, "BASELINE_COMPARISONS", comparisons)


def test_comparison_without_threshold_row_is_an_error(monkeypatch):
    _baseline_rules(monkeypatch, [], [{"path": "m.v", "direction": "lower", "label": "고아"}])
    with pytest.raises(ValueError):
        evals_run.compare_baseline({"m": {"v": 1.0}}, {"m": {"v": 2.0}})


def test_recent_rule_compares_recent_to_recent(monkeypatch):
    _baseline_rules(
        monkeypatch,
        [{"scope": "recent", "path": "m.v", "operator": "<", "value": 0, "severity": "FAIL", "label": "현재동작"}],
        [{"path": "m.v", "direction": "lower", "tolerance": 0.0, "label": "현재동작"}],
    )

    # 네 값을 전부 다르게 둔다. 어느 한쪽이라도 lifetime 값을 잘못 집으면 결론이 뒤집힌다.
    # 현재 recent 2.0 = baseline recent 2.0 → 회귀 아님.
    # (현재 lifetime 1.0이나 baseline lifetime 5.0을 집으면 회귀로 잡힌다)
    regressions = evals_run.compare_baseline(
        {"m": {"v": 1.0}}, {"m": {"v": 5.0}},
        recent_metrics={"m": {"v": 2.0}}, baseline_recent_metrics={"m": {"v": 2.0}},
        recent_item_count=50,
    )
    assert regressions == []

    # 현재 recent 1.0 < baseline recent 2.0 → 회귀.
    # (baseline lifetime 0.5나 현재 lifetime 3.0을 집으면 회귀가 사라진다)
    regressions = evals_run.compare_baseline(
        {"m": {"v": 3.0}}, {"m": {"v": 0.5}},
        recent_metrics={"m": {"v": 1.0}}, baseline_recent_metrics={"m": {"v": 2.0}},
        recent_item_count=50,
    )
    assert [(r["scope"], r["current"], r["baseline"], r["status"]) for r in regressions] == [
        ("recent", 1.0, 2.0, "FAIL"),
    ]


def test_drop_within_tolerance_passes_and_beyond_regresses(monkeypatch):
    _baseline_rules(
        monkeypatch,
        [{"path": "m.v", "operator": "<", "value": 0, "severity": "FAIL", "label": "지표"}],
        [{"path": "m.v", "direction": "lower", "label": "지표"}],   # 기본 상대 10%
    )
    assert evals_run.BASELINE_DEFAULT_TOLERANCE == pytest.approx(0.10)

    assert evals_run.compare_baseline({"m": {"v": 0.95}}, {"m": {"v": 1.0}}) == []
    assert evals_run.compare_baseline({"m": {"v": 0.91}}, {"m": {"v": 1.0}}) == []

    regressions = evals_run.compare_baseline({"m": {"v": 0.85}}, {"m": {"v": 1.0}})
    assert len(regressions) == 1
    row = regressions[0]
    assert row["tolerance"] == pytest.approx(0.10)
    assert row["allowed_delta"] == pytest.approx(0.10)
    assert row["limit"] == pytest.approx(0.90)


def test_near_zero_baseline_uses_absolute_floor(monkeypatch):
    """baseline 0이면 상대 10%는 0이다. 바닥(min_delta)이 없으면 아이템 하나에도 회귀가 된다."""
    _baseline_rules(
        monkeypatch,
        [{"path": "d.r", "operator": ">", "value": 0.05, "severity": "FAIL", "label": "중복"}],
        [{"path": "d.r", "direction": "higher", "min_delta": 0.02, "label": "중복"}],
    )

    assert evals_run.compare_baseline({"d": {"r": 0.01}}, {"d": {"r": 0.0}}) == []
    regressions = evals_run.compare_baseline({"d": {"r": 0.03}}, {"d": {"r": 0.0}})
    assert [(r["allowed_delta"], r["min_delta"]) for r in regressions] == [(0.02, 0.02)]


def _run_main(monkeypatch, capsys, tmp_path, argv, lifetime, recent,
              recent_n=40, baseline_doc=None, input_label=DERIVED):
    """run.main()을 끝까지 돌린다. 지표 계산만 고정값으로 바꾼다."""
    items = [_item(date="2026-01-01")] * 5 + [_item(date="2026-03-01")] * recent_n
    monkeypatch.setattr(evals_run, "resolve_items", lambda: (items, input_label))
    monkeypatch.setattr(
        evals_run, "calculate_metrics",
        lambda xs: lifetime if len(xs) == len(items) else recent,
    )
    baseline_path = tmp_path / "baseline.json"
    if baseline_doc is not None:
        baseline_path.write_text(json.dumps(baseline_doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(evals_run, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr("sys.argv", ["run.py", *argv])

    code = evals_run.main()
    out = capsys.readouterr().out
    report = json.loads(out) if "--json" in argv else None
    return code, report, baseline_path


def test_warn_regression_is_reported_but_does_not_block(monkeypatch, capsys, tmp_path):
    _baseline_rules(
        monkeypatch,
        [{"path": "m.v", "operator": "<", "value": 0, "severity": "WARN", "label": "경고지표"}],
        [{"path": "m.v", "direction": "lower", "label": "경고지표"}],
    )
    code, report, _ = _run_main(
        monkeypatch, capsys, tmp_path, ["--json", "--baseline"],
        lifetime={"m": {"v": 1.0}}, recent={"m": {"v": 1.0}},
        baseline_doc={"metrics": {"m": {"v": 2.0}}},
    )

    assert [r["status"] for r in report["regressions"]] == ["WARN"]
    assert report["blocking_regressions"] == []
    assert code == 0 and report["exit_code"] == 0


def test_fail_regression_still_blocks(monkeypatch, capsys, tmp_path):
    """비차단은 WARN에만 해당한다. FAIL 회귀가 면제되면 게이트가 무의미해진다."""
    _baseline_rules(
        monkeypatch,
        [{"path": "m.v", "operator": "<", "value": 0, "severity": "FAIL", "label": "치명지표"}],
        [{"path": "m.v", "direction": "lower", "label": "치명지표"}],
    )
    code, report, _ = _run_main(
        monkeypatch, capsys, tmp_path, ["--json", "--baseline"],
        lifetime={"m": {"v": 1.0}}, recent={"m": {"v": 1.0}},
        baseline_doc={"metrics": {"m": {"v": 2.0}}},
    )
    assert code == 1
    assert [r["status"] for r in report["blocking_regressions"]] == ["FAIL"]


def test_thin_recent_window_downgrades_regression_to_warn(monkeypatch, capsys, tmp_path):
    """임계값 판정과 같은 원칙(§31, §36): 표본이 모자라면 판단을 보류한다."""
    _baseline_rules(
        monkeypatch,
        [{"scope": "recent", "path": "m.v", "operator": "<", "value": 0, "severity": "FAIL", "label": "현재동작"}],
        [{"path": "m.v", "direction": "lower", "label": "현재동작"}],
    )
    code, report, _ = _run_main(
        monkeypatch, capsys, tmp_path, ["--json", "--baseline"],
        lifetime={"m": {"v": 2.0}}, recent={"m": {"v": 1.0}},
        recent_n=evals_run.MIN_ITEMS_FOR_RECENT_GATE - 1,
        baseline_doc={"metrics": {"m": {"v": 2.0}}, "recent_metrics": {"m": {"v": 2.0}}},
    )
    assert [r["status"] for r in report["regressions"]] == ["WARN"]
    assert code == 0


def test_old_format_baseline_skips_recent_rules(monkeypatch, capsys, tmp_path):
    """recent 지표가 없는 구 baseline이면 recent 규칙은 비교하지 않는다(다른 창끼리 비교 금지)."""
    _baseline_rules(
        monkeypatch,
        [
            {"scope": "recent", "path": "m.v", "operator": "<", "value": 0, "severity": "FAIL", "label": "현재동작"},
            {"path": "d.r", "operator": ">", "value": 0.9, "severity": "FAIL", "label": "누적자산"},
        ],
        [
            {"path": "m.v", "direction": "lower", "label": "현재동작"},
            {"path": "d.r", "direction": "higher", "min_delta": 0.02, "label": "누적자산"},
        ],
    )
    # 구 리포트 포맷: metrics만 있다. 전체 구간 m.v는 크게 나빠 보이지만 비교 대상이 아니다.
    code, report, _ = _run_main(
        monkeypatch, capsys, tmp_path, ["--json", "--baseline"],
        lifetime={"m": {"v": 1.0}, "d": {"r": 0.1}}, recent={"m": {"v": 1.0}, "d": {"r": 0.1}},
        baseline_doc={"input": {"path": "evals/data/archive_items.json"},
                      "metrics": {"m": {"v": 2.0}, "d": {"r": 0.1}}},
    )
    assert report["regressions"] == []
    assert "구 포맷" in report["baseline_recent_skipped"]
    assert code == 0

    # 평문 포맷(지표 dict 그 자체)도 읽고, lifetime 규칙은 그대로 비교한다.
    code, report, _ = _run_main(
        monkeypatch, capsys, tmp_path, ["--json", "--baseline"],
        lifetime={"m": {"v": 1.0}, "d": {"r": 0.5}}, recent={"m": {"v": 1.0}, "d": {"r": 0.5}},
        baseline_doc={"m": {"v": 2.0}, "d": {"r": 0.1}},
    )
    assert [r["metric"] for r in report["regressions"]] == ["d.r"]
    assert code == 1


def test_window_mismatch_skips_recent_comparison_visibly(monkeypatch, capsys, tmp_path):
    """baseline과 recent 창 길이가 다르면 비교를 건너뛰되, 건너뛴 사실과 이유를 싣는다."""
    _baseline_rules(
        monkeypatch,
        [{"scope": "recent", "path": "m.v", "operator": "<", "value": 0, "severity": "FAIL", "label": "현재동작"}],
        [{"path": "m.v", "direction": "lower", "label": "현재동작"}],
    )
    doc = {
        "input": {"recent_window_days": 7},
        "metrics": {"m": {"v": 2.0}},
        "recent_metrics": {"m": {"v": 2.0}},
    }
    code, report, _ = _run_main(
        monkeypatch, capsys, tmp_path, ["--json", "--baseline"],
        lifetime={"m": {"v": 2.0}}, recent={"m": {"v": 1.0}}, baseline_doc=doc,
    )
    assert report["regressions"] == [], "창이 다른 숫자끼리는 비교하지 않는다"
    assert "7일" in report["baseline_recent_skipped"] and "14일" in report["baseline_recent_skipped"]
    assert code == 0

    # 알림에도 보여야 한다. 조용히 사라지면 "회귀 없음"과 구분이 안 된다.
    assert "비교 생략" in format_report(report)

    # 사람이 읽는 출력에도 한 줄. 세부 요약 표가 실제 지표 구조를 요구하므로 채워 넣는다.
    sample = [_item()]
    report["metrics"] = {
        "score_distribution": score_distribution(sample),
        "duplicate_rate": duplicate_rate(sample),
        "source_reach": source_reach(sample, 30),
        "evidence_proxy": evidence_proxy(sample),
    }
    evals_run._print_report(report)
    assert "비교 생략" in capsys.readouterr().out


FUNNEL = {"source_funnel": {"starved_family_count": 3, "confirmed_silent_count": 0, "silent_family_count": 9}}


def _guard_rules(monkeypatch):
    _baseline_rules(
        monkeypatch,
        [{"scope": "recent", "path": "m.v", "operator": "<", "value": 1.5, "severity": "FAIL", "label": "현재동작"}],
        [{"path": "m.v", "direction": "lower", "label": "현재동작"}],
    )


def test_write_baseline_refuses_when_fail_threshold_violated(monkeypatch, capsys, tmp_path):
    """망가진 상태를 기준점으로 박제한 것이 이번 원인이다. 기존 파일도 건드리지 않는다."""
    _guard_rules(monkeypatch)
    previous = {"metrics": {"m": {"v": 9.9}}}
    code, _, path = _run_main(
        monkeypatch, capsys, tmp_path, ["--write-baseline"],
        lifetime={"m": {"v": 2.0}}, recent={"m": {"v": 1.0}}, baseline_doc=previous,
    )
    assert code == 1
    assert json.loads(path.read_text(encoding="utf-8")) == previous


def test_write_baseline_refuses_thin_window_and_foreign_input(monkeypatch, capsys, tmp_path):
    _guard_rules(monkeypatch)
    # 표본 부족이면 FAIL이 WARN으로 강등돼 가드를 빠져나가면 안 된다.
    code, _, path = _run_main(
        monkeypatch, capsys, tmp_path, ["--write-baseline"],
        lifetime={"m": {"v": 2.0}}, recent={"m": {"v": 2.0}},
        recent_n=evals_run.MIN_ITEMS_FOR_RECENT_GATE - 1,
    )
    assert code == 1 and not path.exists()

    # CI와 다른 입력(정적 스냅샷)으로 만든 기준점은 사과 대 오렌지다.
    code, _, path = _run_main(
        monkeypatch, capsys, tmp_path, ["--write-baseline"],
        lifetime={"m": {"v": 2.0}}, recent={"m": {"v": 2.0}},
        input_label="evals/data/archive_items.json",
    )
    assert code == 1 and not path.exists()


def test_write_baseline_refuses_empty_source_funnel(monkeypatch, capsys, tmp_path):
    """app.source_stats import가 실패한 환경(structlog 없는 시스템 파이썬)에서 만든 기준점은 거부한다.

    evals/source_funnel.py가 계열 키 없는 빈 dict를 돌려주면 퍼널 FAIL 임계값이 None으로
    통과해 가드가 거짓 통과가 된다. 실제로 그렇게 만든 baseline이 한 번 나왔다.
    """
    _guard_rules(monkeypatch)
    empty_funnel = {"source_funnel": {"days": 0, "source_count": 0, "starved_sources": [],
                                      "starved_count": 0, "sources": {}}}
    code, _, path = _run_main(
        monkeypatch, capsys, tmp_path, ["--write-baseline"],
        lifetime={"m": {"v": 2.0}, **empty_funnel}, recent={"m": {"v": 2.0}, **empty_funnel},
    )
    assert code == 1 and not path.exists()

    reasons = evals_run.baseline_write_blockers(
        DERIVED, {"m": {"v": 2.0}, **empty_funnel}, {"m": {"v": 2.0}, **FUNNEL}, 40,
    )
    assert len(reasons) == 1 and "source_funnel(lifetime)" in reasons[0]


def test_write_baseline_records_both_scopes_and_round_trips(monkeypatch, capsys, tmp_path):
    _guard_rules(monkeypatch)
    lifetime, recent = {"m": {"v": 1.8}, **FUNNEL}, {"m": {"v": 2.0}, **FUNNEL}
    code, _, path = _run_main(
        monkeypatch, capsys, tmp_path, ["--write-baseline"], lifetime=lifetime, recent=recent,
    )
    assert code == 0
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["metrics"] == lifetime
    assert document["recent_metrics"] == recent
    assert document["input"]["path"] == DERIVED
    assert document["input"]["item_count"] == 45
    assert document["input"]["recent_item_count"] == 40
    assert document["input"]["start_date"] == "2026-01-01"
    for key in ("generated_at", "git_commit", "git_dirty", "format_version"):
        assert key in document

    # 방금 쓴 기준점과 같은 입력으로 비교하면 회귀가 없어야 한다.
    code, report, _ = _run_main(
        monkeypatch, capsys, tmp_path, ["--json", "--baseline"], lifetime=lifetime, recent=recent,
    )
    assert code == 0 and report["regressions"] == []
    assert report["baseline_input"]["recent_item_count"] == 40


def test_write_baseline_rejects_since_and_baseline_flags(monkeypatch):
    for argv in (["--write-baseline", "--baseline"], ["--write-baseline", "--since", "2026-08-01"]):
        monkeypatch.setattr("sys.argv", ["run.py", *argv])
        with pytest.raises(SystemExit):
            evals_run._parse_args()


def test_format_report_separates_warn_regressions():
    """WARN 회귀는 📉(게이트 차단)로 보이면 안 된다."""
    text = format_report(_report(regressions=[
        {"label": "차단회귀", "current": 0.3, "baseline": 0.1, "status": "FAIL"},
        {"label": "경고회귀", "current": 3, "baseline": 2, "status": "WARN"},
    ]))

    assert "📉 차단회귀" in text
    assert "📉 경고회귀" not in text
    warn_line = next(line for line in text.splitlines() if "경고회귀" in line)
    assert "게이트 무관" in warn_line


# ── evals/alert.py: 알림이 실제로 읽는 채널로 나가는가 ──────────────────

import evals.alert as evals_alert  # noqa: E402
from evals.alert import MAX_DISCORD_CONTENT, build_text, send_discord, send_email  # noqa: E402


def _alert_report():
    return {
        "input": {"item_count": 10, "start_date": "2026-09-01", "end_date": "2026-09-15", "path": "data/records/"},
        "blocking_violations": [{"label": "타임스탬프 비율", "actual": 0.1, "operator": "<", "threshold": 0.7}],
        "regressions": [],
        "violations": [],
    }


def test_alert_text_carries_reason_and_run_url():
    """GitHub 기본 메일은 '실패했다'만 알려준다. 무엇이 실패했는지가 본문에 있어야 한다."""
    text = build_text(_alert_report(), "https://github.com/x/y/actions/runs/1")
    assert "타임스탬프 비율" in text
    assert "https://github.com/x/y/actions/runs/1" in text


def test_alert_skips_channel_without_config_and_reports_it():
    """설정이 없으면 조용히 성공한 척하지 않고 건너뛴 사실을 남긴다."""
    for fn in (send_discord, send_email):
        channel, ok, detail = fn("본문", {})
        assert ok and "건너뜀" in detail, (channel, detail)


def test_alert_truncates_discord_content(monkeypatch):
    """Discord 는 2000자를 넘기면 400 이다. 잘라서 보낸다."""
    sent = {}

    def _fake_post(url, payload, headers):
        sent["len"] = len(payload["content"])
        return 200, ""

    monkeypatch.setattr("evals.alert._post", _fake_post)
    send_discord("가" * 5000, {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "c"})
    assert sent["len"] == MAX_DISCORD_CONTENT


def test_alert_reports_channel_failure(monkeypatch):
    """전송 실패를 삼키면 '안 온 것'과 '못 받은 것'을 구분할 수 없다."""
    monkeypatch.setattr("evals.alert._post", lambda *a, **k: (401, "unauthorized"))
    _, ok, detail = send_discord("본문", {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "c"})
    assert not ok and "401" in detail


def test_alert_request_carries_user_agent(monkeypatch):
    """Cloudflare 는 urllib 기본 UA 를 `403 error code: 1010` 으로 막는다.

    2026-09-16 러너 실측: UA 없이 보낸 알림이 Discord·Resend 양쪽에서 막혔다.
    알림이 안 나가는 실패는 게이트 실패보다 조용해서 더 오래 방치된다.
    """
    seen = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(evals_alert.urllib.request, "urlopen", _fake_urlopen)
    status, _ = evals_alert._post("https://example.test/x", {"a": 1}, {"Authorization": "Bot t"})

    assert status == 200
    assert seen["ua"] and "python-urllib" not in seen["ua"].lower(), seen

