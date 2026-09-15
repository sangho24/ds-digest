"""과거 산출물 품질 계측 실행기.

실행 예:
    py evals/run.py
    py evals/run.py --json
    py evals/run.py --since 2026-06-01 --baseline
    py evals/run.py --write-baseline      # 현재 입력으로 evals/baseline.json 재생성
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
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
        BASELINE_DEFAULT_TOLERANCE,
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
        BASELINE_DEFAULT_TOLERANCE,
        MIN_ITEMS_FOR_RECENT_GATE,
        RECENT_WINDOW_DAYS,
        THRESHOLDS,
    )
    from build_items import build_items  # type: ignore[no-redef]
    from source_funnel import source_funnel  # type: ignore[no-redef]


EVALS_DIR = Path(__file__).resolve().parent
DATA_PATH = EVALS_DIR / "data" / "archive_items.json"
BASELINE_PATH = EVALS_DIR / "baseline.json"
# resolve_items가 data/records/에서 입력을 만들었을 때 붙이는 출처 표시. CI는 항상 이쪽이다.
DERIVED_INPUT_LABEL = "data/records/ (파생)"
BASELINE_FORMAT_VERSION = 2
# source_funnel이 실제로 계측됐다면 반드시 있는 키. app.source_stats import가 실패하면
# (예: structlog 없는 시스템 파이썬) evals/source_funnel.py가 이 키들이 없는 빈 dict를
# 돌려주고, 퍼널 임계값은 None으로 조용히 통과한다.
FUNNEL_REQUIRED_KEYS = ("starved_family_count", "confirmed_silent_count", "silent_family_count")


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


def _threshold_policy(path: str) -> tuple[str, str]:
    """baseline 비교 규칙이 따를 (scope, severity)를 같은 path의 THRESHOLDS 행에서 찾는다.

    비교 규칙에 따로 적지 않는 이유: 두 곳에 적으면 어긋난다. 실제로 요약 길이는
    THRESHOLDS에서 WARN인데 회귀는 무조건 FAIL이었고, 그 불일치가 게이트를 막았다.
    같은 path에 행이 여러 개면(예: 하한·상한) scope는 같아야 하고 severity는 가장
    무거운 쪽을 따른다.
    """
    rows = [rule for rule in THRESHOLDS if rule["path"] == path]
    if not rows:
        raise ValueError(f"baseline 비교 규칙 {path} 에 대응하는 THRESHOLDS 행이 없습니다")
    scopes = {rule.get("scope", "lifetime") for rule in rows}
    if len(scopes) != 1:
        raise ValueError(f"THRESHOLDS의 {path} 행들이 서로 다른 scope를 가집니다: {sorted(scopes)}")
    severity = "FAIL" if any(rule["severity"] == "FAIL" for rule in rows) else "WARN"
    return scopes.pop(), severity


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_baseline(
    metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
    recent_metrics: dict[str, Any] | None = None,
    baseline_recent_metrics: dict[str, Any] | None = None,
    recent_item_count: int | None = None,
) -> list[dict[str, Any]]:
    """방향성이 명확한 핵심 지표가 baseline보다 허용폭 이상 나빠졌는지 비교한다.

    - scope: 같은 path의 THRESHOLDS 행을 따른다. recent 지표는 **현재 recent 값과
      baseline의 recent 값끼리** 비교한다. 전체 구간 값끼리 비교하면 임계값 판정과
      다른 창을 보게 되고, 고장나 있던 과거가 영원히 섞인다(thresholds 모듈 설명).
      한쪽이라도 recent 값이 없으면(구 포맷 baseline 등) 그 규칙은 건너뛴다. 창이
      다른 숫자끼리의 비교는 판정이 아니라 우연이다.
    - 허용폭: `max(|baseline| x tolerance, min_delta)`. 허용오차 0이던 시절엔 표본
      잡음만으로 매주 흔들렸다. min_delta는 baseline이 0 근처일 때 상대비가 0으로
      쪼그라들어 아이템 하나에도 회귀가 되는 것을 막는다(나눗셈을 하지 않으므로
      0 baseline에서도 발산하지 않는다).
    - severity: 같은 path의 THRESHOLDS severity를 따른다. WARN 회귀는 보고만 한다.
      recent 창이 MIN_ITEMS_FOR_RECENT_GATE 미만이면 FAIL을 WARN으로 낮춘다
      (evaluate_thresholds와 같은 원칙).
    """
    thin = (
        recent_metrics is not None
        and recent_item_count is not None
        and recent_item_count < MIN_ITEMS_FOR_RECENT_GATE
    )
    regressions: list[dict[str, Any]] = []
    for rule in BASELINE_COMPARISONS:
        path = rule["path"]
        scope, severity = _threshold_policy(path)
        if scope == "recent":
            if recent_metrics is None or baseline_recent_metrics is None:
                continue
            current = _get_path(recent_metrics, path)
            baseline = _get_path(baseline_recent_metrics, path)
        else:
            current = _get_path(metrics, path)
            baseline = _get_path(baseline_metrics, path)
        if not _is_number(current) or not _is_number(baseline):
            continue

        tolerance = rule.get("tolerance", BASELINE_DEFAULT_TOLERANCE)
        min_delta = rule.get("min_delta", 0.0)
        allowed = max(abs(baseline) * tolerance, min_delta)
        if rule["direction"] == "lower":
            limit = baseline - allowed
            worse = current < limit
        else:
            limit = baseline + allowed
            worse = current > limit
        if not worse:
            continue

        if scope == "recent" and thin and severity == "FAIL":
            severity = "WARN"
        regressions.append(
            {
                "metric": path,
                "label": rule["label"],
                "status": severity,
                "scope": scope,
                "current": current,
                "baseline": baseline,
                "direction": rule["direction"],
                "tolerance": tolerance,
                "min_delta": min_delta,
                "allowed_delta": round(allowed, 6),
                "limit": round(limit, 6),
            }
        )
    return regressions


def load_baseline(
    document: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """baseline 문서 → (lifetime 지표, recent 지표 또는 None, 메타).

    세 포맷을 모두 읽는다.
      v2        {"metrics", "recent_metrics", "input", ...}  (--write-baseline 산출)
      구 리포트 {"metrics", "input", "threshold_results", ...}  (recent 지표 없음)
      평문      지표 dict 그 자체
    recent 지표가 없으면 None을 돌려주고, compare_baseline은 recent 규칙을 건너뛴다.
    """
    if not isinstance(document, dict):
        raise ValueError("baseline 문서는 JSON 객체여야 합니다")
    if isinstance(document.get("metrics"), dict):
        recent = document.get("recent_metrics")
        meta = {key: value for key, value in document.items() if key not in ("metrics", "recent_metrics")}
        return document["metrics"], recent if isinstance(recent, dict) else None, meta
    return document, None, {}


def baseline_write_blockers(
    input_path: str,
    metrics: dict[str, Any],
    recent_metrics: dict[str, Any] | None,
    recent_item_count: int,
) -> list[str]:
    """현재 입력이 baseline 자격이 없는 이유들. 비어 있어야 기록한다.

    2026-07-20 baseline은 수정 전 파이프라인(임계값 FAIL 8개)의 정적 스냅샷이었다.
    망가진 상태를 기준점으로 박제하면 "그보다 나빠졌는가"는 영원히 참이 되지 않고,
    반대로 우연히 나빴던 값보다 좋아진 변화는 방향을 잘못 잡은 규칙에서 회귀가 된다.
    그래서 다음을 모두 만족할 때만 기록한다.
      1. CI와 같은 입력(data/records/ 파생)이다. 다른 창의 숫자와 비교하지 않게.
      2. recent 창 표본이 게이트를 걸 만큼 있다.
      3. 모든 FAIL 임계값을 통과한다. 표본 부족 강등 없이 원래 severity로 본다.
      4. source_funnel이 실제로 계측됐다. import 실패로 비어 있으면 퍼널 FAIL 임계값이
         None으로 통과해 3번이 거짓 통과가 되고, CI(의존성 설치됨)와 다른 숫자가 박제된다.
    """
    reasons: list[str] = []
    for scope_name, source in (("lifetime", metrics), ("recent", recent_metrics)):
        if source is None:
            continue
        funnel = source.get("source_funnel")
        missing = [key for key in FUNNEL_REQUIRED_KEYS if not isinstance(funnel, dict) or key not in funnel]
        if missing:
            reasons.append(
                f"source_funnel({scope_name})이 비어 있습니다(누락 키: {', '.join(missing)}). "
                "app.source_stats import가 실패한 환경입니다. 의존성이 설치된 "
                ".venv/bin/python evals/run.py --write-baseline 으로 다시 실행하세요"
            )
    if input_path != DERIVED_INPUT_LABEL:
        reasons.append(
            f"입력이 {input_path} 입니다. CI는 {DERIVED_INPUT_LABEL} 로 계측하므로 "
            "baseline도 같은 입력이어야 합니다 (evals/data/archive_items.json을 치우고 다시 실행)"
        )
    if recent_metrics is None or recent_item_count < MIN_ITEMS_FOR_RECENT_GATE:
        reasons.append(
            f"최근 창 표본 {recent_item_count}건 < {MIN_ITEMS_FOR_RECENT_GATE}건: "
            "recent 지표를 기준점으로 삼을 근거가 없습니다"
        )
    # recent_item_count를 넘기지 않으면 thin 강등이 꺼져 FAIL이 FAIL 그대로 나온다.
    _, violations = evaluate_thresholds(metrics, recent_metrics)
    for row in violations:
        if row["status"] == "FAIL":
            reasons.append(
                f"FAIL 임계값 위반: {row['label']} {row['actual']} "
                f"({row['operator']} {row['threshold']}, {row['scope']})"
            )
    return reasons


def _git_state() -> tuple[str | None, bool | None]:
    """(HEAD 커밋 SHA, 추적 파일에 커밋 안 된 변경이 있는지). git이 없으면 (None, None)."""
    root = EVALS_DIR.parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    return sha or None, bool(status.strip())


def build_baseline_document(
    input_meta: dict[str, Any],
    metrics: dict[str, Any],
    recent_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """재현 가능한 baseline 문서. 손으로 편집하지 않고 --write-baseline으로만 만든다."""
    commit, dirty = _git_state()
    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "generated_at": date.today().isoformat(),
        "generated_by": "python evals/run.py --write-baseline",
        "git_commit": commit,
        "git_dirty": dirty,
        "input": input_meta,
        "metrics": metrics,
        "recent_metrics": recent_metrics,
    }


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
        print("baseline 대비 회귀 (WARN 회귀는 게이트 무관)")
        regression_rows = [
            [
                row["status"],
                row["label"],
                row.get("scope", "lifetime"),
                row["current"],
                row["baseline"],
                f"허용 {_format_value(row.get('allowed_delta'))} (한계 {_format_value(row.get('limit'))})",
            ]
            for row in report["regressions"]
        ]
        _print_table(["상태", "지표", "범위", "현재", "baseline", "허용폭"], regression_rows)
    elif report["baseline_compared"]:
        print()
        print("baseline 대비 나빠진 핵심 지표가 없습니다.")
    if report.get("baseline_recent_skipped"):
        print(f"recent 지표의 baseline 비교 생략: {report['baseline_recent_skipped']}")

    print()
    warn_count = len(report["violations"]) - len(report["blocking_violations"])
    blocking_regressions = len(report.get("blocking_regressions", report["regressions"]))
    print(
        f"판정: {'위반 있음 (exit 1)' if report['exit_code'] else '통과 (exit 0)'} "
        f"- FAIL {len(report['blocking_violations'])}개, "
        f"WARN {warn_count}개(게이트 무관), 회귀 {len(report['regressions'])}개"
        f"(그중 게이트 차단 {blocking_regressions}개)"
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
    return items, DERIVED_INPUT_LABEL


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
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "현재 입력의 지표로 evals/baseline.json을 다시 쓴다. data/records/ 파생 입력이 아니거나, "
            "recent 표본이 부족하거나, FAIL 임계값을 하나라도 위반하면 거부하고 exit 1"
        ),
    )
    args = parser.parse_args()
    if args.write_baseline and args.baseline:
        parser.error("--write-baseline과 --baseline은 함께 쓸 수 없습니다 (방금 쓴 기준점과 자기 자신을 비교하게 됨)")
    if args.write_baseline and args.since:
        parser.error("--write-baseline은 --since와 함께 쓸 수 없습니다 (CI는 전체 구간으로 비교하므로 기준점도 전체 구간이어야 함)")
    if args.since:
        try:
            args.since = date.fromisoformat(args.since)
        except ValueError:
            parser.error("--since는 YYYY-MM-DD 형식이어야 합니다.")
    return args


def _write_baseline(
    input_meta: dict[str, Any],
    metrics: dict[str, Any],
    recent_metrics: dict[str, Any] | None,
    recent_item_count: int,
) -> int:
    """가드를 통과하면 BASELINE_PATH에 기록하고 0, 거부하면 기존 파일을 건드리지 않고 1."""
    reasons = baseline_write_blockers(input_meta["path"], metrics, recent_metrics, recent_item_count)
    if reasons:
        print("baseline 재생성 거부: 현재 입력은 기준점 자격이 없습니다", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    document = build_baseline_document(input_meta, metrics, recent_metrics)
    BASELINE_PATH.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"baseline 기록: {BASELINE_PATH} - {input_meta['item_count']}건 "
        f"({input_meta['start_date']} ~ {input_meta['end_date']}), "
        f"recent {recent_item_count}건, 커밋 {document['git_commit'] or '-'}"
        f"{' (커밋 안 된 변경 있음)' if document['git_dirty'] else ''}"
    )
    return 0


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

        start_date, end_date = _date_range(items)
        recent_start, recent_end = _date_range(recent_items)
        input_meta = {
            "path": input_path,
            "item_count": len(items),
            "start_date": start_date,
            "end_date": end_date,
            "since": args.since.isoformat() if args.since else None,
            "recent_window_days": args.window,
            "recent_item_count": len(recent_items),
            "recent_start_date": recent_start,
            "recent_end_date": recent_end,
            "recent_gate_active": len(recent_items) >= MIN_ITEMS_FOR_RECENT_GATE,
        }

        if args.write_baseline:
            return _write_baseline(input_meta, metrics, recent_metrics, len(recent_items))

        regressions: list[dict[str, Any]] = []
        baseline_input: dict[str, Any] | None = None
        # recent 회귀 비교를 통째로 건너뛸 때 그 사실과 이유. 조용히 사라지면 "회귀 없음"과
        # 구분이 안 된다.
        baseline_recent_skipped: str | None = None
        if args.baseline:
            with BASELINE_PATH.open("r", encoding="utf-8") as file:
                baseline_metrics, baseline_recent, baseline_meta = load_baseline(json.load(file))
            baseline_input = baseline_meta.get("input")
            baseline_window = (baseline_input or {}).get("recent_window_days")
            if baseline_recent is None:
                baseline_recent_skipped = "baseline에 recent 지표가 없습니다(구 포맷). --write-baseline으로 재생성하세요"
            elif baseline_window is not None and baseline_window != args.window:
                # recent 창 길이가 다르면 같은 이름의 다른 숫자다. 비교하지 않는다.
                baseline_recent = None
                baseline_recent_skipped = (
                    f"recent 창 길이가 다릅니다(baseline {baseline_window}일, 현재 {args.window}일)"
                )
            regressions = compare_baseline(
                metrics, baseline_metrics, recent_metrics, baseline_recent, len(recent_items)
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2

    # WARN은 보고하되 게이트를 떨어뜨리지 않는다. 예: "30일 이상 미등장 소스"는
    # 소스 구성을 손볼 신호이지 그날의 산출물이 나쁘다는 뜻이 아니다.
    # 회귀도 같다. severity는 같은 path의 THRESHOLDS를 따르고 FAIL 회귀만 막는다.
    blocking = [row for row in violations if row["status"] == "FAIL"]
    blocking_regressions = [row for row in regressions if row["status"] == "FAIL"]
    exit_code = 1 if blocking or blocking_regressions else 0
    report = {
        "input": input_meta,
        "metrics": metrics,
        "threshold_results": threshold_rows,
        "violations": violations,
        "blocking_violations": blocking,
        "baseline_compared": bool(args.baseline),
        # 무엇과 비교했는지. 기준점의 창·건수·생성 커밋이 다르면 숫자의 의미도 다르다.
        "baseline_input": baseline_input,
        "baseline_recent_skipped": baseline_recent_skipped,
        "regressions": regressions,
        "blocking_regressions": blocking_regressions,
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
