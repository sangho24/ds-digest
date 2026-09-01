"""품질 경보 및 baseline 회귀 판정 규칙."""

from __future__ import annotations

from typing import Any


# current 주석은 evals/data/archive_items.json 전체를 run.py로 계측한 값이다.
THRESHOLDS: list[dict[str, Any]] = [
    # current: 0.848574
    {"path": "tag_entropy.normalized_entropy", "operator": "<", "value": 0.70, "severity": "WARN", "label": "태그 정규화 엔트로피"},
    # current: 0.88664 (MLOps 219/247)
    {"path": "tag_concentration.concentration", "operator": ">", "value": 0.40, "severity": "FAIL", "label": "최빈 태그 집중도"},
    # current: 1.0
    {"path": "score_distribution.iqr", "operator": "<", "value": 1.5, "severity": "FAIL", "label": "관련도 점수 IQR"},
    # current: 3
    {"path": "score_distribution.distinct_values", "operator": "<", "value": 5, "severity": "FAIL", "label": "관련도 고유값 수"},
    # current: 2개
    {"path": "source_reach.stale_source_count", "operator": ">", "value": 0, "severity": "WARN", "label": "30일 이상 미등장 소스"},
    # current: 0.198381 (49/247)
    {"path": "duplicate_rate.duplicate_url_rate", "operator": ">", "value": 0.05, "severity": "FAIL", "label": "중복 URL 비율"},
    # current: 0.008621 (1/116)
    {"path": "evidence_proxy.timestamp_rate", "operator": "<", "value": 0.70, "severity": "FAIL", "label": "YouTube 타임스탬프 비율"},
    # current: 1.0 (247/247가 2개)
    {"path": "schema_rigidity.production_ideas_count.fixed_ratio", "operator": ">", "value": 0.95, "severity": "FAIL", "label": "적용 아이디어 개수 고정률"},
    # current: 1.0 (247/247가 2개)
    {"path": "schema_rigidity.quiz_count.fixed_ratio", "operator": ">", "value": 0.95, "severity": "FAIL", "label": "퀴즈 개수 고정률"},
    # current: 32.380567자
    {"path": "summary_stats.one_line_summary.mean", "operator": "<", "value": 20, "severity": "WARN", "label": "한 줄 요약 평균 길이"},
    # 수집은 되는데 한 번도 발송되지 않는 소스. source_reach로는 구조적으로 볼 수
    # 없던 사각지대다(그 지표의 소스 목록이 발송 아이템에서 만들어지기 때문).
    # 실측: arXiv가 40일간 발송 0건인데 아무 경보도 없었다.
    # 기록이 없으면(퍼널 도입 전 구간) 0이 되어 통과한다.
    # 계열 기준으로 센다. arXiv 14개 카테고리 × 하루 5칸이라 키 기준으로는
    # 대부분의 카테고리가 매일 굶고, 상시 켜진 경보는 읽히지 않는다.
    # 알고 싶은 건 "cs.SI가 이번 주 미발송"이 아니라 "arXiv 계열이 통째로 미발송"이다.
    {"path": "source_funnel.starved_family_count", "operator": ">", "value": 0, "severity": "WARN", "label": "수집되나 미발송인 소스 계열"},
    # 설정돼 있는데 수집이 0건인 소스 = 수집기가 깨진 것. 실측으로 arXiv가
    # http→https 301 때문에 40일간 0건이었는데 아무 경보도 없었다.
    # 굶는 소스보다 나쁜 상태라 FAIL로 둔다.
    # 게이트를 막는 FAIL은 계열 기준으로만 건다. 논문 수가 적은 카테고리 하나가
    # 한 주 조용한 것과, 수집기가 깨져 arXiv 전체가 0건인 것은 전혀 다른 사건이다.
    # (후자가 실제로 5개월간 조용히 지속됐다 — http→https 리다이렉트.)
    {"path": "source_funnel.silent_family_count", "operator": ">", "value": 0, "severity": "FAIL", "label": "수집이 0건인 소스 계열"},
    {"path": "source_funnel.silent_count", "operator": ">", "value": 0, "severity": "WARN", "label": "수집이 0건인 소스"},
]


# direction은 값이 어느 쪽으로 움직일 때 품질 회귀인지 나타낸다.
BASELINE_COMPARISONS: list[dict[str, str]] = [
    {"path": "tag_entropy.normalized_entropy", "direction": "lower", "label": "태그 정규화 엔트로피"},
    {"path": "tag_concentration.concentration", "direction": "higher", "label": "최빈 태그 집중도"},
    {"path": "score_distribution.iqr", "direction": "lower", "label": "관련도 점수 IQR"},
    {"path": "score_distribution.distinct_values", "direction": "lower", "label": "관련도 고유값 수"},
    {"path": "source_reach.stale_source_count", "direction": "higher", "label": "장기 미등장 소스 수"},
    {"path": "duplicate_rate.duplicate_url_rate", "direction": "higher", "label": "중복 URL 비율"},
    {"path": "evidence_proxy.timestamp_rate", "direction": "lower", "label": "YouTube 타임스탬프 비율"},
    {"path": "schema_rigidity.production_ideas_count.fixed_ratio", "direction": "higher", "label": "적용 아이디어 개수 고정률"},
    {"path": "schema_rigidity.quiz_count.fixed_ratio", "direction": "higher", "label": "퀴즈 개수 고정률"},
    {"path": "summary_stats.one_line_summary.mean", "direction": "lower", "label": "한 줄 요약 평균 길이"},
]
