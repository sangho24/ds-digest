"""품질 계기판 → 다음 날 큐레이션에 남기는 권고 한 줄.

왜 만드나:
    주간 게이트는 지금까지 재기만 했다. 값이 나빠져도 다이제스트는 그대로 나갔고,
    사람이 로그를 열어 고치기 전에는 아무것도 바뀌지 않았다.

왜 "권고"인가:
    지시는 두 갈래로 적용된다(app/directives.py). drop_sources 처럼 코드로 확정
    적용되는 쪽과, standing_note 처럼 프롬프트에 얹히는 쪽이다. 계기판이 자동으로
    남기는 줄은 **뒤쪽만** 건드려야 한다. 지표 한 번 튀었다고 소스가 통째로 빠지면
    그날 지면이 비고, 원인은 아무데도 안 적힌다.

    실제 방어는 `interpret()` 가 계기판 줄을 **LLM 에 넘기지 않고** 갈라내는 것이다.
    "문구에 출처 식별자를 안 넣으면 안전하다"는 설명은 틀렸다(검증에서 반증).
    허용 목록은 문구가 아니라 해석 프롬프트가 공급하므로, 모델이 규칙을 어기면
    목록에서 아무거나 골라 drop_sources 에 넣을 수 있다. 식별자를 안 쓰는 것은
    2차 방어일 뿐이고, 분리 로직을 지우면 그 즉시 뚫린다.

왜 프롬프트로 고칠 수 있는 것만 다루나:
    "YouTube 타임스탬프 비율이 낮다"는 전사 입력에 시간 정보가 없다는 뜻이라
    글쓰기 지시로 고쳐지지 않는다(PROGRESS §21). 중복 URL·소스 퍼널도 수집 쪽
    문제다. 그런 지표는 알림으로만 남기고 여기서는 건드리지 않는다.

TTL:
    7일. 다음 주 실행 전에 저절로 사라진다. 지표가 계속 나쁘면 다음 주에 다시
    쓰이고, 회복되면 조용히 없어진다. 만료 없는 자동 지시는 "왜 이런 다이제스트가
    오는지 아무도 모르는 상태"를 만든다.

TTL 과 주간 실행 간격의 경합:
    TTL 7일과 주간 실행 간격 7일이 같은 길이라, 이번 주 실행이 지난주보다 조금이라도
    빠르면 지난주 권고가 몇 분 더 살아 있는 상태에서 중복 판정을 받는다. GitHub cron
    발화 시각은 매주 수십 분에서 수 시간씩 흔들리므로(지난 6주 중앙 지연 204분) 이
    경합은 운에 달려 있었다. 실측 2026-10-26: 10-19 12:17:15 KST 권고의 만료가
    10-26 12:17:15 인데 권고 스텝이 12:14:43 에 돌아, 2분 32초 차이로 "이미 있음"이
    되어 건너뛰었고 곧바로 만료됐다. 경고는 계속 켜져 있는데 되먹임은 한 주 통째로
    빠졌다. 그래서 남은 수명이 RENEWAL_MARGIN 미만인 권고는 "곧 없어질 것"으로 보고
    다시 쓴다(갱신).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# 사람이 쓴 지시와 구분하는 표지. app.directives 가 이 표지를 보고 LLM 경로에서
# 아예 빼내 권고(standing_note)로만 잇는다. 두 곳이 갈라지지 않게 한 곳에서 가져온다.
from app.directives import KST, DASHBOARD_PREFIX as PREFIX  # noqa: E402

# 주간 실행 간격과 같다. 더 길면 회복된 뒤에도 권고가 남고, 더 짧으면 다음 주 실행 전에
# 끊긴다. 간격과 길이가 같아 생기는 경합은 RENEWAL_MARGIN 으로 흡수한다.
TTL_DAYS = 7

# 살아 있는 같은 권고라도 남은 수명이 이보다 짧으면 "이미 있음"으로 치지 않고 다시 쓴다.
# 주간 실행과 TTL 이 같은 길이라 발화 시각의 흔들림(수십 분~수 시간)만큼 경합하는데,
# 1일이면 그 흔들림은 덮는다. 3일로 둔 이유는 지난주 실행이 밀린 경우까지 덮기 위해서다.
# 월요일 잡이 실패해 수요일에 수동 재실행하면 권고 만료가 다음 수요일이 되고, 다음 월요일
# 정기 실행 시점에 남은 수명이 2일이라 1일 마진으로는 건너뛰어 5일 공백이 난다. 반대로
# 같은 날 수동 재실행은 남은 수명이 ~7일이라 여전히 중복으로 걸러진다. 갱신 뒤 두 행이
# 최대 3일 겹치는데, 그 사이 standing_note 는 app.directives.interpret 가 같은 문장을 한
# 번만 잇는다.
RENEWAL_MARGIN = timedelta(days=3)

# (지표 경로, 연산자) → 남길 권고. 프롬프트로 고칠 수 있는 것만 담는다.
# 출처 식별자(URL, 채널 ID)는 어떤 문구에도 넣지 않는다.
#
# 연산자를 키에 넣는 이유: 한 줄 요약 길이는 같은 지표에 임계값이 둘이다
# (20자 미만이면 상투적, 30자 초과면 프롬프트 상한 위반). 지표 이름만 보면
# 짧아져서 걸린 경우에도 "더 압축하라"가 나가 짧은 요약을 더 깎는 되먹임이 된다.
ADVICE: dict[tuple[str, str], str] = {
    ("tag_concentration.concentration", ">"): "최근 다이제스트가 한 주제로 쏠렸다. 내일은 서로 다른 분야를 고르게 섞어 고르라.",
    ("tag_entropy.normalized_entropy", "<"): "최근 주제 다양성이 줄었다. 내일은 분야가 겹치지 않게 고르라.",
    ("score_distribution.iqr", "<"): "관련도 점수가 한 값에 몰려 순위가 사실상 없다. 후보끼리 비교해 점수 차이를 분명히 벌려라.",
    ("score_distribution.distinct_values", "<"): "관련도 점수가 몇 값만 쓰이고 있다. 후보 간 차이를 더 세밀하게 매겨라.",
    # schema_rigidity.production_ideas_count.fixed_ratio 는 2026-10-14 에 뺐다. "개수를 달리하라"는
    # 모델이 따를 수 없는 지시다. 프롬프트가 "0~3개" 범위를 주면 모델은 근거가 충분한 한 상한을
    # 채운다(근거 있는 아이템 기준 전환 전 97%, 전환 후 92%, 최근 2주 98.5%). 프롬프트에
    # "3은 상한이지 목표가 아니다"라고 써 둔 지금도 그렇다. 이 지표로는 품질 저하 여부를 알 수
    # 없고, 근거 여부는 LLM 심판(evals/judge.py)이 잰다. 효과가 확인된 권고만 남긴다.
    # 지표 자체는 THRESHOLDS 에 WARN 으로 남아 보고만 된다.
    ("schema_rigidity.quiz_count.fixed_ratio", ">"): "퀴즈 개수가 매번 똑같다. 내용에 맞게 개수를 달리하라.",
    ("summary_stats.one_line_summary.mean", ">"): "한 줄 요약이 길어지고 있다. 30자 이내로 더 압축하라.",
    ("summary_stats.one_line_summary.mean", "<"): "한 줄 요약이 지나치게 짧아 상투적이다. 30자 이내에서 핵심을 담아라.",
}


def build_advice(report: dict[str, Any]) -> list[str]:
    """리포트에서 프롬프트로 고칠 수 있는 위반·회귀만 골라 권고 문구로 바꾼다.

    게이트를 막는 FAIL 뿐 아니라 WARN 도 대상으로 삼는다. 요약 길이처럼 WARN 인
    지표는 게이트를 떨어뜨리지 않으므로, 여기서 다루지 않으면 추세가 기울어도
    아무 일도 일어나지 않는다.

    임계값 위반(violations)뿐 아니라 baseline 회귀(regressions)도 읽는다. 둘은 같은
    지표가 나빠졌다는 신호를 다른 기준으로 잰 것이다. 위반만 읽던 동안 적용 아이디어
    고정률이 절대 임계값(> 0.95)은 안 넘은 채 회귀로만 4주 연속 게이트를 막았고
    (0.806→0.800→0.843), ADVICE 에 그 지표가 있는데도 권고가 한 번도 안 나갔다.
    blocking_regressions 는 regressions 의 부분집합이라 따로 읽지 않는다. 같은 지표가
    양쪽에 걸리면 seen 이 한 줄로 합친다.
    """
    seen: set[tuple[str, str]] = set()
    advice: list[str] = []
    rows = list(report.get("violations") or []) + list(report.get("regressions") or [])
    for row in rows:
        key = (str(row.get("metric") or ""), str(row.get("operator") or ""))
        text = ADVICE.get(key)
        if not text or key in seen:
            continue
        seen.add(key)
        advice.append(f"{PREFIX} {text}")
    return advice


def _remaining_life(row: dict[str, Any], now: datetime) -> timedelta | None:
    """행의 남은 수명. expires_at 이 없거나 못 읽으면 None(= 알 수 없음).

    load_raw 와 같은 보수적 해석이다. 만료 시각을 못 읽는 행을 "곧 죽는다"고 보면
    같은 권고가 매주 다시 쌓여 사람 지시를 밀어낸다. 모르면 살아 있는 것으로 본다.
    """
    try:
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if expires_at.tzinfo is None:
        # capture() 는 항상 tz 가 붙은 ISO 문자열을 쓴다. naive 값은 손으로 넣은 행이라
        # KST 로 본다. now 도 같은 규칙으로 맞춘다. tz 가 섞이면 뺄셈이 TypeError 로 죽는다.
        expires_at = expires_at.replace(tzinfo=KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    return expires_at - now


def classify_advice(
    report: dict[str, Any],
    existing: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[tuple[str, bool]]:
    """남길 권고를 (문구, 갱신 여부) 로 돌려준다.

    살아 있는 같은 권고는 다시 쓰지 않는다. 매주 같은 줄이 쌓이면 해석 프롬프트에
    같은 말이 여러 번 들어가고, 원문 상한(MAX_RAW_MESSAGES)을 자동 생성물이 밀어내
    사람이 쓴 지시가 잘려 나간다.

    단, 남은 수명이 RENEWAL_MARGIN 미만이면 "이미 있음"으로 치지 않고 다시 쓴다
    (갱신, 두 번째 값 True). 주간 실행과 TTL 이 같은 길이라 이번 실행이 지난주보다
    몇 분만 빨라도 지난주 권고가 아직 살아 있고, 건너뛰면 곧바로 만료되어 한 주
    통째로 되먹임이 빠진다(2026-10-26 실측, 2분 32초 차). 같은 날 수동 재실행은
    남은 수명이 ~7일이라 여전히 중복으로 걸러진다.

    expires_at 이 없거나 못 읽는 행은 load_raw 와 같은 기준으로 "살아 있음"으로
    본다. 기존 동작 유지.
    """
    reference = now or datetime.now(KST)
    # 같은 문구가 여러 행이면 가장 늦게 만료되는 행 기준으로 판정한다. 갱신 직후에는
    # 옛 행(곧 만료)과 새 행(~7일)이 같이 살아 있는데, 옛 행만 보고 또 갱신하면 안 된다.
    # None(알 수 없음)은 "충분히 살아 있음"으로 둔다.
    longest: dict[str, timedelta | None] = {}
    for row in existing:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        life = _remaining_life(row, reference)
        if text not in longest:
            longest[text] = life
        elif longest[text] is not None and (life is None or life > longest[text]):
            longest[text] = life

    result: list[tuple[str, bool]] = []
    for text in build_advice(report):
        if text not in longest:
            result.append((text, False))
            continue
        life = longest[text]
        if life is None or life >= RENEWAL_MARGIN:
            continue
        result.append((text, True))
    return result


def new_advice(
    report: dict[str, Any],
    existing: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[str]:
    """남길 권고 문구만 돌려준다(새 권고와 갱신을 구분하지 않음). classify_advice 참조."""
    return [text for text, _ in classify_advice(report, existing, now=now)]


def main(argv: list[str]) -> int:
    """리포트를 읽어 새 권고(와 곧 만료될 권고의 갱신)만 원문 파일에 남긴다."""
    import json
    import sys
    from pathlib import Path

    if len(argv) < 2:
        print("사용: python -m evals.directive <report.json>", file=sys.stderr)
        return 2
    try:
        report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"리포트를 읽지 못했습니다: {error}", file=sys.stderr)
        return 2

    from app.directives import capture, load_raw

    # 상한 없이 살아 있는 원문 전체와 비교한다. 최근 30건만 보면 창 밖으로
    # 밀린 권고가 없는 것처럼 보여 같은 줄이 다시 쌓인다.
    now = datetime.now(KST)
    entries = classify_advice(report, load_raw(now=now, limit=None), now=now)
    for text, is_renewal in entries:
        capture(text, ttl_days=TTL_DAYS, now=now)
        print(f"{'권고 갱신' if is_renewal else '권고 기록'}: {text}")
    if not entries:
        print("새로 남길 권고가 없습니다.")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
