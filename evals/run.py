"""과거 산출물 품질 계측 실행기.

실행 예:
    py evals/run.py
    py evals/run.py --json
    py evals/run.py --since 2026-06-01 --baseline
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
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
    from .thresholds import (
        BASELINE_COMPARISONS,
        MIN_ITEMS_FOR_RECENT_GATE,
        RECENT_WINDOW_DAYS,
        THRESHOLDS,
    )
    from .build_items import build_items
    from .source_funnel import source_funnel
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
    from thresholds import (  # type: ignore[no-redef]
        BASELINE_COMPARISONS,
        MIN_ITEMS_FOR_RECENT_GATE,
        RECENT_WINDOW_DAYS,
        THRESHOLDS,
    )
    from build_items import build_items  # type: ignore[no-redef]
    from source_funnel import source_funnel  # type: ignore[no-redef]


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
        # 발송 아이템만으로는 알 수 없는 것 — 수집은 되는데 한 번도
        # 발송되지 않는 소스. 별도 기록(data/source_stats.jsonl)에서 온다.
        "source_funnel": source_funnel(),
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


def evaluate_thresholds(
    metrics: dict[str, Any],
    recent_metrics: dict[str, Any] | None = None,
    recent_item_count: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """모든 임계값의 판정 행과 위반 목록을 반환한다.

    반환되는 violations에는 WARN과 FAIL이 모두 담긴다. **게이트를 떨어뜨리는 것은
    FAIL뿐이다** — 판정은 main()의 blocking_violations가 한다. 둘을 구분하지 않으면
    severity 필드가 라벨 장식으로만 남고, WARN 하나가 영구히 잡 전체를 빨간불로
    묶어 "실패"가 신호가 아니라 소음이 된다.

    `scope: "recent"` 규칙은 **최근 창의 값으로** 판정한다. 전체 구간으로 재면
    이미 고친 버그가 영원히 FAIL로 남기 때문이다(thresholds 모듈 설명 참조).
    표본이 모자라면 FAIL을 WARN으로 낮춘다 — 근거가 없을 때의 답은 "문제 없음"이
    아니라 "판단할 근거가 없음"이다(§31과 같은 원칙).

    recent_metrics를 주지 않으면 전과 똑같이 전체 구간으로만 판정한다.
    """
    thin = (
        recent_metrics is not None
        and recent_item_count is not None
        and recent_item_count < MIN_ITEMS_FOR_RECENT_GATE
    )

    rows: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for rule in THRESHOLDS:
        scope = rule.get("scope", "lifetime")
        source = recent_metrics if (scope == "recent" and recent_metrics is not None) else metrics
        actual = _get_path(source, rule["path"])
        violated = _violates(actual, rule["operator"], rule["value"])

        severity = rule["severity"]
        if violated and scope == "recent" and thin and severity == "FAIL":
            severity = "WARN"

        row = {
            "metric": rule["path"],
            "label": rule["label"],
            "status": severity if violated else "PASS",
            "actual": actual,
            "operator": rule["operator"],
            "threshold": rule["value"],
            "scope": scope,
            # 같은 지표의 전체 구간 값. 최근만 나쁜지 원래 나쁜지 구분해준다.
            "lifetime_actual": _get_path(metrics, rule["path"]),
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
    window, recent_n = source.get("recent_window_days"), source.get("recent_item_count")
    if window:
        gate = "판정 사용" if source.get("recent_gate_active") else "표본 부족 — FAIL 보류"
        print(f"  현재 동작 지표(recent)는 최근 {window}일 {recent_n}건으로 판정 · {gate}")
    print()

    def _actual(row: dict[str, Any]) -> str:
        """recent 지표는 전체 구간 값을 함께 보여준다.

        최근만 나쁜 것인지 원래 나빴던 것인지가 한 줄에서 갈린다 — 이 구분이
        없으면 "고쳤는데 왜 아직 빨간불인가"를 매번 손으로 다시 재게 된다.
        """
        value = row["actual"]
        lifetime = row.get("lifetime_actual")
        if row.get("scope") != "recent" or lifetime is None or lifetime == value:
            return str(value)
        return f"{value}  (전체 {lifetime})"

    threshold_rows = [
        [
            row["status"],
            row["label"],
            _actual(row),
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
    warn_count = len(report["violations"]) - len(report["blocking_violations"])
    print(
        f"판정: {'위반 있음 (exit 1)' if report['exit_code'] else '통과 (exit 0)'} "
        f"- FAIL {len(report['blocking_violations'])}개, "
        f"WARN {warn_count}개(게이트 무관), 회귀 {len(report['regressions'])}개"
    )


def resolve_items() -> tuple[list[dict[str, Any]], str]:
    """계측 입력과 그 출처를 결정한다.

    정적 스냅샷(evals/data/archive_items.json)이 있으면 그것을 쓴다 — baseline이
    계측된 것과 같은 창이라 회귀 비교가 사과 대 사과가 된다.

    없으면 커밋된 구조화 정본(data/records/)에서 만든다. `evals/data/`는
    .gitignore 대상이라 CI에는 절대 존재하지 않는다. 이 폴백이 없던 탓에 Weekly
    Evals가 만들어진 이래 매주 exit 2로 실패했고, 품질 게이트가 한 번도 동작한
    적이 없었다. 입력이 없으면 실패하는 게 아니라, 있는 데이터로 재는 게 맞다.

    어느 쪽을 썼는지는 리포트 input.path에 그대로 실어 보낸다 — 계측 창이 다르면
    숫자의 의미도 다르므로 읽는 쪽이 알아야 한다.
    """
    if DATA_PATH.exists():
        root = EVALS_DIR.parent
        # 리포 밖 경로(테스트의 tmp_path 등)에서도 죽지 않게 방어적으로 줄인다.
        try:
            label = str(DATA_PATH.relative_to(root))
        except ValueError:
            label = str(DATA_PATH)
        return _load_items(DATA_PATH), label

    items = build_items()
    if not items:
        raise ValueError(
            "계측할 입력이 없습니다: "
            f"{DATA_PATH} 도 없고 data/records/ 에도 레코드가 없습니다"
        )
    return items, "data/records/ (파생)"


def _load_items(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"입력 파일은 객체의 JSON 배열이어야 합니다: {path}")
    return data


def _item_date(item: dict[str, Any]) -> date | None:
    """아이템의 산출 날짜. 없거나 깨졌으면 None."""
    try:
        return date.fromisoformat(str(item.get("date"))[:10])
    except (TypeError, ValueError):
        return None


def _filter_since(items: list[dict[str, Any]], since: date | None) -> list[dict[str, Any]]:
    if since is None:
        return items
    return [
        item
        for item in items
        if (item_date := _item_date(item)) is not None and item_date >= since
    ]


def _date_range(items: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates: list[date] = []
    for item in items:
        try:
            dates.append(date.fromisoformat(str(item.get("date"))[:10]))
        except (TypeError, ValueError):
            continue
    return (min(dates).isoformat(), max(dates).isoformat()) if dates else (None, None)


def _recent_since(items: list[dict[str, Any]], days: int) -> date | None:
    """최근 창의 시작일. 마지막 산출일을 기준으로 잡는다.

    오늘 날짜가 아니라 **데이터의 마지막 날**을 기준으로 하는 이유: 파이프라인이
    며칠 멈춰 있어도 창이 통째로 비어 "표본 부족"으로 넘어가 버리면, 정작 그
    멈춤을 알려야 할 지표가 조용해진다.
    """
    dates = [d for d in (_item_date(item) for item in items) if d is not None]
    if not dates:
        return None
    return max(dates) - timedelta(days=days - 1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="과거 콘텐츠 산출물 품질을 자동 계측합니다.")
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON으로 출력")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="해당 날짜부터의 아이템만 포함")
    parser.add_argument(
        "--window",
        type=int,
        default=RECENT_WINDOW_DAYS,
        metavar="DAYS",
        help=f"현재 동작 지표(scope=recent)의 관측 창, 기본 {RECENT_WINDOW_DAYS}일",
    )
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
        source_items, input_path = resolve_items()
        items = _filter_since(source_items, args.since)
        metrics = calculate_metrics(items)
        # 현재 동작 지표는 최근 창으로 따로 잰다. 전체 구간으로 재면 이미 고친
        # 버그가 영원히 FAIL로 남는다(evals/thresholds.py 모듈 설명).
        recent_items = _filter_since(items, _recent_since(items, args.window))
        recent_metrics = calculate_metrics(recent_items) if recent_items else None
        threshold_rows, violations = evaluate_thresholds(
            metrics, recent_metrics, len(recent_items)
        )
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
    # WARN은 보고하되 게이트를 떨어뜨리지 않는다. 예: "30일 이상 미등장 소스"는
    # 소스 구성을 손볼 신호이지 그날의 산출물이 나쁘다는 뜻이 아니다.
    blocking = [row for row in violations if row["status"] == "FAIL"]
    exit_code = 1 if blocking or regressions else 0
    report = {
        "input": {
            "path": input_path,
            "item_count": len(items),
            "start_date": start_date,
            "end_date": end_date,
            "since": args.since.isoformat() if args.since else None,
            "recent_window_days": args.window,
            "recent_item_count": len(recent_items),
            "recent_gate_active": len(recent_items) >= MIN_ITEMS_FOR_RECENT_GATE,
        },
        "metrics": metrics,
        "threshold_results": threshold_rows,
        "violations": violations,
        "blocking_violations": blocking,
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
