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
"""

from __future__ import annotations

from typing import Any

# 사람이 쓴 지시와 구분하는 표지. app.directives 가 이 표지를 보고 LLM 경로에서
# 아예 빼내 권고(standing_note)로만 잇는다. 두 곳이 갈라지지 않게 한 곳에서 가져온다.
from app.directives import DASHBOARD_PREFIX as PREFIX  # noqa: E402
TTL_DAYS = 7

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
    ("schema_rigidity.production_ideas_count.fixed_ratio", ">"): "적용 아이디어 개수가 매번 똑같다. 내용에 맞게 개수를 달리하라.",
    ("schema_rigidity.quiz_count.fixed_ratio", ">"): "퀴즈 개수가 매번 똑같다. 내용에 맞게 개수를 달리하라.",
    ("summary_stats.one_line_summary.mean", ">"): "한 줄 요약이 길어지고 있다. 30자 이내로 더 압축하라.",
    ("summary_stats.one_line_summary.mean", "<"): "한 줄 요약이 지나치게 짧아 상투적이다. 30자 이내에서 핵심을 담아라.",
}


def build_advice(report: dict[str, Any]) -> list[str]:
    """리포트에서 프롬프트로 고칠 수 있는 위반만 골라 권고 문구로 바꾼다.

    게이트를 막는 FAIL 뿐 아니라 WARN 도 대상으로 삼는다. 요약 길이처럼 WARN 인
    지표는 게이트를 떨어뜨리지 않으므로, 여기서 다루지 않으면 추세가 기울어도
    아무 일도 일어나지 않는다.
    """
    seen: set[tuple[str, str]] = set()
    advice: list[str] = []
    for row in report.get("violations") or []:
        key = (str(row.get("metric") or ""), str(row.get("operator") or ""))
        text = ADVICE.get(key)
        if not text or key in seen:
            continue
        seen.add(key)
        advice.append(f"{PREFIX} {text}")
    return advice


def new_advice(report: dict[str, Any], existing: list[dict[str, Any]]) -> list[str]:
    """아직 살아 있는 같은 권고는 다시 쓰지 않는다.

    매주 같은 줄이 쌓이면 해석 프롬프트에 같은 말이 여러 번 들어가고, 원문 상한
    (MAX_RAW_MESSAGES)을 자동 생성물이 밀어내 사람이 쓴 지시가 잘려 나간다.
    """
    live = {str(row.get("text") or "").strip() for row in existing}
    return [text for text in build_advice(report) if text not in live]


def main(argv: list[str]) -> int:
    """리포트를 읽어 새 권고만 원문 파일에 남긴다."""
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
    texts = new_advice(report, load_raw(limit=None))
    for text in texts:
        capture(text, ttl_days=TTL_DAYS)
        print(f"권고 기록: {text}")
    if not texts:
        print("새로 남길 권고가 없습니다.")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
