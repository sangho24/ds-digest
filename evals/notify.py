"""품질 게이트 리포트 → 사람이 읽을 알림 문구.

왜 별도 스크립트인가:
    워크플로 YAML 안에 heredoc으로 파이썬을 인라인하면 블록 스칼라 들여쓰기가
    깨져 YAML 자체가 파싱 불가가 된다(실제로 그렇게 만들었다가 되돌렸다).
    무엇보다 인라인 스크립트는 테스트할 수 없다.

왜 알림이 필요한가:
    GitHub의 기본 실패 메일은 "워크플로가 실패했다"만 알려준다. 매주 오는 그
    메일은 곧 소음이 되고, 아무도 로그를 열지 않게 된다. 게이트가 빨간불이면
    **무엇이** 문제인지까지 같은 자리에서 보여야 신호로 남는다.

사용:
    python -m evals.notify evals_report.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def format_report(report: dict[str, Any]) -> str:
    """리포트 dict → Telegram 본문(HTML 없이 평문)."""
    source = report.get("input") or {}
    lines = [
        f"계측 {source.get('item_count', '?')}건 "
        f"({source.get('start_date') or '-'} ~ {source.get('end_date') or '-'})",
        f"입력: {source.get('path', '?')}",
        "",
    ]

    for row in report.get("blocking_violations") or []:
        lines.append(
            f"❌ {row['label']}: {row['actual']} "
            f"(기준 {row['operator']} {row['threshold']})"
        )

    # 회귀도 severity를 따른다(evals/run.py compare_baseline). status가 없는 옛
    # 리포트는 회귀가 전부 게이트를 막던 시절 것이므로 차단으로 본다.
    regressions = report.get("regressions") or []
    for row in regressions:
        if row.get("status", "FAIL") == "FAIL":
            lines.append(
                f"📉 {row['label']}: {row['current']} (baseline {row['baseline']})"
            )

    # WARN은 게이트를 떨어뜨리지 않지만 같이 보여준다 — 지금 조치할 필요는 없어도
    # 추세를 놓치면 나중에 FAIL로 넘어간 뒤에야 알게 된다.
    for row in report.get("violations") or []:
        if row.get("status") == "WARN":
            lines.append(f"⚠️ {row['label']}: {row['actual']} (게이트 무관)")
    for row in regressions:
        if row.get("status") == "WARN":
            lines.append(
                f"↘️ {row['label']}: {row['current']} "
                f"(baseline {row['baseline']}, 회귀지만 게이트 무관)"
            )

    # recent 회귀 비교를 건너뛰었다면 "회귀 없음"과 구분되게 알린다.
    if report.get("baseline_recent_skipped"):
        lines.append(f"ℹ️ recent 지표 baseline 비교 생략: {report['baseline_recent_skipped']}")

    if len(lines) == 3:
        lines.append("위반 항목을 찾지 못했습니다 — 실행 자체가 실패했을 수 있습니다.")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("사용: python -m evals.notify <report.json>", file=sys.stderr)
        return 2
    try:
        report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # 알림이 실패해도 게이트 결과 자체는 이미 정해졌다. 최소한의 문구는 낸다.
        print(f"리포트를 읽지 못했습니다: {error}")
        return 0
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
