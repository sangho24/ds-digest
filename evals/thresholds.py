"""품질 경보 및 baseline 회귀 판정 규칙.

## scope — 어느 구간을 재는가

계측 창이 무한히 자라면 **이미 고친 버그가 영원히 FAIL로 남는다.** 실측:

    YouTube 타임스탬프   전체 42일 1/55  ·  수정(2026-09-01) 이후 1/1
    관련도 IQR           전체 42일 1.0   ·  상대 랭킹 도입 이후 2.0

둘 다 고쳐졌는데 게이트는 2026-08-17부터 매주 빨간불이었다. 42일 평균이
고장나 있던 구간에 지배되기 때문이고, 이 창은 앞으로도 계속 커지므로
**저절로 회복되지 않는다.** 계측기가 고장난 것이지 파이프라인이 고장난 게
아니다.

그래서 지표를 두 갈래로 나눈다.

    recent    지금 파이프라인이 제대로 도는가 (점수 분포·타임스탬프·스키마)
              → 최근 RECENT_WINDOW_DAYS일만 보고 판정한다
    lifetime  쌓인 자산이 건강한가 (중복 재유입·장기 미등장 소스)
              → 정의상 긴 창이 필요하므로 전체를 본다

표본이 모자랄 때는 FAIL을 걸지 않는다. §31(수집 0건 판정)과 같은 원칙이다 —
근거가 모자랄 때의 답은 "문제 없음"이 아니라 "판단할 근거가 없음"이다.
"""

from __future__ import annotations

from typing import Any

# 현재 동작을 재는 지표의 관측 창. 하루 5건이므로 14일이면 약 70건이다.
RECENT_WINDOW_DAYS = 14
# 그 창에 이만큼도 안 쌓였으면 recent 지표로 FAIL을 걸지 않는다.
MIN_ITEMS_FOR_RECENT_GATE = 30


# current 주석은 evals/data/archive_items.json 전체를 run.py로 계측한 값이다.
THRESHOLDS: list[dict[str, Any]] = [
    # current: 0.848574
    {"scope": "recent", "path": "tag_entropy.normalized_entropy", "operator": "<", "value": 0.70, "severity": "WARN", "label": "태그 정규화 엔트로피"},
    # current: 0.88664 (MLOps 219/247)
    {"scope": "recent", "path": "tag_concentration.concentration", "operator": ">", "value": 0.40, "severity": "FAIL", "label": "최빈 태그 집중도"},
    # current: 1.0
    {"scope": "recent", "path": "score_distribution.iqr", "operator": "<", "value": 1.5, "severity": "FAIL", "label": "관련도 점수 IQR"},
    # current: 3
    {"scope": "recent", "path": "score_distribution.distinct_values", "operator": "<", "value": 5, "severity": "FAIL", "label": "관련도 고유값 수"},
    # current: 2개
    {"path": "source_reach.stale_source_count", "operator": ">", "value": 0, "severity": "WARN", "label": "30일 이상 미등장 소스"},
    # current: 0.198381 (49/247)
    {"path": "duplicate_rate.duplicate_url_rate", "operator": ">", "value": 0.05, "severity": "FAIL", "label": "중복 URL 비율"},
    # current: 0.008621 (1/116)
    {"scope": "recent", "path": "evidence_proxy.timestamp_rate", "operator": "<", "value": 0.70, "severity": "FAIL", "label": "YouTube 타임스탬프 비율"},
    # current: 1.0 (247/247가 2개)
    {"scope": "recent", "path": "schema_rigidity.production_ideas_count.fixed_ratio", "operator": ">", "value": 0.95, "severity": "FAIL", "label": "적용 아이디어 개수 고정률"},
    # current: 1.0 (247/247가 2개)
    {"scope": "recent", "path": "schema_rigidity.quiz_count.fixed_ratio", "operator": ">", "value": 0.95, "severity": "FAIL", "label": "퀴즈 개수 고정률"},
    # current: 32.380567자
    {"scope": "recent", "path": "summary_stats.one_line_summary.mean", "operator": "<", "value": 20, "severity": "WARN", "label": "한 줄 요약 평균 길이"},
    # 같은 path에 행이 둘이다(하한 20, 상한 30). evaluate_thresholds는 규칙마다 행을
    # 따로 만들므로 공존할 수 있다. 상한은 분석 프롬프트(app/analyzer.py)의
    # "한 줄 요약 (한국어, 30자 이내)" 지시를 그대로 옮긴 것이다. 평균이 30을 넘으면
    # 지시가 체계적으로 무시되고 있다는 뜻이다(평균은 개별 초과를 가리는 너그러운
    # 대리값이라, 이 선을 넘었다면 이미 절반 가까이가 초과다).
    # 길이는 단조로운 좋고/나쁨이 아니므로 baseline 회귀 비교에서는 뺐다(아래 참조).
    {"scope": "recent", "path": "summary_stats.one_line_summary.mean", "operator": ">", "value": 30, "severity": "WARN", "label": "한 줄 요약 평균 길이(프롬프트 상한 30자)"},
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
    # FAIL은 관측 기간이 충분할 때만 건다(app.source_stats.MIN_DAYS_FOR_SILENCE).
    # 퍼널 2일치로 판정했더니 주간·월간 발행 블로그 12곳이 "침묵"으로 잡혀
    # 게이트가 막혔다 — 수집기가 깨진 게 아니라 그 주에 글이 안 올라온 것이다.
    {"path": "source_funnel.confirmed_silent_count", "operator": ">", "value": 0, "severity": "FAIL", "label": "장기간 수집 0건인 소스 계열"},
    {"path": "source_funnel.silent_family_count", "operator": ">", "value": 0, "severity": "WARN", "label": "이번 기간 수집이 0건인 소스 계열"},
]


# ── baseline 회귀 비교 ──────────────────────────────────────
#
# direction은 값이 어느 쪽으로 움직일 때 품질 회귀인지 나타낸다.
#
# **scope와 severity는 여기 두지 않는다.** 같은 path의 THRESHOLDS 행에서 가져온다
# (evals/run.py `_threshold_policy`). 두 곳에 적으면 언젠가 어긋난다. 실제로
# 어긋나 있었다: 요약 길이는 THRESHOLDS에서 WARN인데 회귀는 무조건 FAIL로 게이트를
# 막았고, recent 지표도 회귀 비교만은 전체 구간 값으로 쟀다.
#
# 허용폭: `max(|baseline| x tolerance, min_delta)` 만큼 나빠지는 것까지는 회귀가
# 아니다. 허용오차 0이던 시절엔 조금만 흔들려도 FAIL이었다. recent 창은 약 55건,
# 그중 YouTube는 약 15건이라 표본 잡음만으로 매주 값이 움직인다.
#   tolerance  상대 허용오차. 기본 BASELINE_DEFAULT_TOLERANCE(10%).
#   min_delta  절대 허용폭의 하한. baseline이 0에 가까우면 상대 10%가 사실상 0이
#              되어 아이템 하나만 바뀌어도 회귀가 된다. 그래서 표본 잡음 크기
#              (대략 표준오차의 2배)를 바닥으로 깐다.
#
# 근거로 쓴 실측(2026-08-10~09-15, 14일 창을 하루씩 밀며 계측):
#   최빈 태그 집중도      0.136~0.255   한 아이템 = 약 0.018, 2SE(p=0.15, n=55) = 0.096
#   YouTube 타임스탬프    한 아이템 = 약 0.067, 2SE(p=0.76, n=15) = 0.22
#   고정률(퀴즈·아이디어) 2SE(p=0.5~0.85, n=55) = 0.10~0.13
#   관련도 IQR            정수 점수라 0.25 단위로 움직이고 1.25~2.0을 오갔다
BASELINE_DEFAULT_TOLERANCE = 0.10

BASELINE_COMPARISONS: list[dict[str, Any]] = [
    # 0.97~0.99에서 움직인다. 상대 10%(약 0.1)면 잡음보다 충분히 넓다.
    {"path": "tag_entropy.normalized_entropy", "direction": "lower", "label": "태그 정규화 엔트로피"},
    {"path": "tag_concentration.concentration", "direction": "higher", "min_delta": 0.10, "label": "최빈 태그 집중도"},
    {"path": "score_distribution.iqr", "direction": "lower", "min_delta": 0.5, "label": "관련도 점수 IQR"},
    # 정수라 한 칸(1)은 잡음으로 본다.
    {"path": "score_distribution.distinct_values", "direction": "lower", "min_delta": 1, "label": "관련도 고유값 수"},
    {"path": "source_reach.stale_source_count", "direction": "higher", "min_delta": 1, "label": "장기 미등장 소스 수"},
    # lifetime 비율. 0.009 근처라 상대 10%는 0.001(중복 한 건 미만)이다.
    # FAIL 기준 0.05의 절반 아래에서 조기 경보가 되도록 0.02를 바닥으로 둔다.
    {"path": "duplicate_rate.duplicate_url_rate", "direction": "higher", "min_delta": 0.02, "label": "중복 URL 비율"},
    # 분모가 YouTube 아이템(약 15건)뿐이라 다른 비율보다 잡음이 두 배 크다.
    {"path": "evidence_proxy.timestamp_rate", "direction": "lower", "min_delta": 0.20, "label": "YouTube 타임스탬프 비율"},
    {"path": "schema_rigidity.production_ideas_count.fixed_ratio", "direction": "higher", "min_delta": 0.12, "label": "적용 아이디어 개수 고정률"},
    {"path": "schema_rigidity.quiz_count.fixed_ratio", "direction": "higher", "min_delta": 0.10, "label": "퀴즈 개수 고정률"},
    # summary_stats.one_line_summary.mean은 2026-09-15에 뺐다. 방향이 틀린 비교였다.
    # 프롬프트는 "30자 이내"를 지시하는데 baseline 32.38자는 지시 위반 상태였고,
    # 28.56자로 짧아진 것은 개선이다. 그런데 "짧아지면 회귀"로 비교해 2026-09-07,
    # 09-14 게이트를 막았다. 길이는 한쪽 방향이 늘 좋은 지표가 아니므로 회귀 비교
    # 대신 THRESHOLDS의 하한(< 20)과 상한(> 30) 두 WARN으로만 본다.
]
