"""과거 산출물 품질 계측 실행기.

실행 예:
    py evals/run.py
    py evals/run.py --json
    py evals/run.py --since 2026-06-01 --baseline
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

try:  # 패키지 import와 직접 스크립트 실행을 모두 지원한다.
    from .metrics import (
        duplicate_rate,
        evidence_proxy,
        schema_rigidity,
        score_distribution,
        source_reach,
        summary_stats,
        tag_concentration,
        tag_entropy,
    )
    from .thresholds import BASELINE_COMPARISONS, THRESHOLDS
except ImportError:
    from metrics import (  # type: ignore[no-redef]
        duplicate_rate,
        evidence_proxy,
        schema_rigidity,
        score_distribution,
        source_reach,
        summary_stats,
        tag_concentration,
        tag_entropy,
    )
    from thresholds import BASELINE_COMPARISONS, THRESHOLDS  # type: ignore[no-redef]


EVALS_DIR = Path(__file__).resolve().parent
DATA_PATH = EVALS_DIR / "data" / "archive_items.json"
BASELINE_PATH = EVALS_DIR / "baseline.json"


def calculate_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    """실행기가 사용하는 모든 지표를 한 번에 계산한다."""
    return {
        "tag_entropy": tag_entropy(items),
        "tag_concentration": tag_concentration(items),
        "score_distribution": score_distribution(items),
        "source_reach": source_reach(items, window_days=30, reference_date=date.today()),
        "duplicate_rate": duplicate_rate(items),
        "evidence_proxy": evidence_proxy(items),
        "schema_rigidity": schema_rigidity(items),
        "summary_stats": summary_stats(items),
    }


def _get_path(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _violates(actual: Any, operator: str, expected: Any) -> bool:
    if actual is None:
        return False
    operations = {
        ">": lambda: actual > expected,
        ">=": lambda: actual >= expected,
        "<": lambda: actual < expected,
        "<=": lambda: actual <= expected,
        "==": lambda: actual == expected,
        "!=": lambda: actual != expected,
    }
    if operator not in operations:
        raise ValueError(f"지원하지 않는 임계값 연산자: {operator}")
    return operations[operator]()


def evaluate_thresholds(metrics: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """모든 임계값의 판정 행과 위반 목록을 반환한다."""
    rows: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for rule in THRESHOLDS:
        actual = _get_path(metrics, rule["path"])
        violated = _violates(actual, rule["operator"], rule["value"])
        row = {
            "metric": rule["path"],
            "label": rule["label"],
            "status": rule["severity"] if violated else "PASS",
            "actual": actual,
            "operator": rule["operator"],
            "threshold": rule["value"],
        }
        rows.append(row)
        if violated:
            violations.append(row)
    return rows, violations


def compare_baseline(metrics: dict[str, Any], baseline_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """방향성이 명확한 핵심 지표만 baseline보다 나빠졌는지 비교한다."""
    regressions: list[dict[str, Any]] = []
    for rule in BASELINE_COMPARISONS:
        current = _get_path(metrics, rule["path"])
        baseline = _get_path(baseline_metrics, rule["path"])
        if not isinstance(current, (int, float)) or not isinstance(baseline, (int, float)):
            continue
        worse = current < baseline if rule["direction"] == "lower" else current > baseline
        if worse:
            regressions.append(
                {
                    "metric": rule["path"],
                    "label": rule["label"],
                    "status": "FAIL",
                    "current": current,
                    "baseline": baseline,
                    "direction": rule["direction"],
                }
            )
    return regressions


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    rendered = [[_format_value(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    print(line(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rendered:
        print(line(row))


def _print_report(report: dict[str, Any]) -> None:
    source = report["input"]
    print(
        f"품질 계측: {source['item_count']}건 "
        f"({source['start_date'] or '-'} ~ {source['end_date'] or '-'})"
    )
    print()
    threshold_rows = [
        [
            row["status"],
            row["label"],
            row["actual"],
            f"{row['operator']} {row['threshold']}",
        ]
        for row in report["threshold_results"]
    ]
    _print_table(["상태", "지표", "실측값", "경보 조건"], threshold_rows)

    metrics = report["metrics"]
    score = metrics["score_distribution"]
    duplicate = metrics["duplicate_rate"]
    reach = metrics["source_reach"]
    evidence = metrics["evidence_proxy"]
    interval = duplicate["reappearance_intervals_days"]
    stale_names = ", ".join(source["source_name"] for source in reach["stale_sources"]) or "없음"
    print()
    print("세부 요약")
    detail_rows = [
        ["관련도 히스토그램", json.dumps(score["histogram"], ensure_ascii=False)],
        ["소스 수 / 장기 미등장", f"{reach['source_count']} / {reach['stale_source_count']} ({stale_names})"],
        ["고유 URL / 중복 발생분", f"{duplicate['unique_urls']} / {duplicate['duplicate_occurrences']}"],
        ["재등장 간격(일)", f"{interval['min']}~{interval['max']}, 평균 {interval['mean']}"],
        ["YouTube 타임스탬프", f"{evidence['with_timestamp']}/{evidence['youtube_items']}"],
    ]
    _print_table(["항목", "값"], detail_rows)

    if report["regressions"]:
        print()
        print("baseline 대비 회귀")
        regression_rows = [
            [row["status"], row["label"], row["current"], row["baseline"]]
            for row in report["regressions"]
        ]
        _print_table(["상태", "지표", "현재", "baseline"], regression_rows)
    elif report["baseline_compared"]:
        print()
        print("baseline 대비 나빠진 핵심 지표가 없습니다.")

    print()
    print(
        f"판정: {'위반 있음 (exit 1)' if report['exit_code'] else '통과 (exit 0)'} "
        f"- 임계값 위반 {len(report['violations'])}개, 회귀 {len(report['regressions'])}개"
    )


def _load_items(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"입력 파일은 객체의 JSON 배열이어야 합니다: {path}")
    return data


def _filter_since(items: list[dict[str, Any]], since: date | None) -> list[dict[str, Any]]:
    if since is None:
        return items
    filtered: list[dict[str, Any]] = []
    for item in items:
        try:
            item_date = date.fromisoformat(str(item.get("date"))[:10])
        except (TypeError, ValueError):
            continue
        if item_date >= since:
            filtered.append(item)
    return filtered


def _date_range(items: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates: list[date] = []
    for item in items:
        try:
            dates.append(date.fromisoformat(str(item.get("date"))[:10]))
        except (TypeError, ValueError):
            continue
    return (min(dates).isoformat(), max(dates).isoformat()) if dates else (None, None)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="과거 콘텐츠 산출물 품질을 자동 계측합니다.")
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON으로 출력")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="해당 날짜부터의 아이템만 포함")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="evals/baseline.json과 비교해 품질 회귀를 탐지",
    )
    args = parser.parse_args()
    if args.since:
        try:
            args.since = date.fromisoformat(args.since)
        except ValueError:
            parser.error("--since는 YYYY-MM-DD 형식이어야 합니다.")
    return args


def main() -> int:
    args = _parse_args()
    try:
        items = _filter_since(_load_items(DATA_PATH), args.since)
        metrics = calculate_metrics(items)
        threshold_rows, violations = evaluate_thresholds(metrics)
        regressions: list[dict[str, Any]] = []
        if args.baseline:
            with BASELINE_PATH.open("r", encoding="utf-8") as file:
                baseline_document = json.load(file)
            baseline_metrics = baseline_document.get("metrics", baseline_document)
            regressions = compare_baseline(metrics, baseline_metrics)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2

    start_date, end_date = _date_range(items)
    exit_code = 1 if violations or regressions else 0
    report = {
        "input": {
            "path": "evals/data/archive_items.json",
            "item_count": len(items),
            "start_date": start_date,
            "end_date": end_date,
            "since": args.since.isoformat() if args.since else None,
        },
        "metrics": metrics,
        "threshold_results": threshold_rows,
        "violations": violations,
        "baseline_compared": bool(args.baseline),
        "regressions": regressions,
        "exit_code": exit_code,
    }
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_report(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
