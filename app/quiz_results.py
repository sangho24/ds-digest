"""퀴즈 응답 기록 — 이 시스템의 유일한 ground truth.

왜 이게 특별한가:
    나머지 신호는 전부 취향이다. 👍/👎는 "좋았다"는 자기보고고, 반응률은 관심의
    대리 지표일 뿐이다. 어느 것도 **맞고 틀림이 없다**.

    퀴즈는 다르다. 정답 인덱스가 콘텐츠에서 파생된 라벨이므로, 사용자의 선택은
    채점 가능하다. 설계 문서 §7.5가 짚은 대로 "퀴즈가 정답 라벨을 만든다" —
    개념별 습득도(§4.2 ⑥)를 계산할 수 있는 유일한 입력이다.

    지금까지는 스포일러로 정답을 보여주고 사용자가 맞혔는지는 버렸다.

왜 오답 인덱스까지 남기는가:
    "맞음/틀림" 2진 신호보다 **어떤 오답을 골랐는지**가 정보가 많다. 같은 개념을
    반복해서 특정 방향으로 틀리면 그건 모르는 게 아니라 잘못 알고 있는 것이고,
    필요한 후속 콘텐츠가 다르다.

왜 배치 폴링이라 즉시 채점을 못 하는가:
    GitHub Actions에서 하루 한 번 getUpdates를 돌리므로, 버튼을 눌러도
    answerCallbackQuery 토스트는 최대 24시간 뒤에나 나가고 그때는 콜백이 이미
    만료돼 있다. 그래서 정답 공개는 계속 <tg-spoiler>가 맡고, 버튼은 **선택을
    기록**하는 역할만 한다. 스포일러를 열기 전에 고른 답이 잡히므로 자가신고보다
    라벨 품질이 높다.

저장 위치:
    data/quiz_results.jsonl (append-only, 커밋됨)

    러너가 ephemeral이라 커밋되지 않는 경로는 매 런 사라진다. Supabase에만
    두면 크레덴셜이 끊기는 순간 라벨이 통째로 증발한다 — 정본은 리포에 둔다.
    (data/records/와 같은 원칙: "로컬 JSON이 정본")

    append-only이므로 같은 문항에 답을 바꾸면 두 줄이 남는다. 읽는 쪽이
    (item_id, question_index)별 **마지막** 줄을 취한다 — load_results가 그렇게 한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from app.preferences import ItemFacts, build_item_index

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT / "data" / "quiz_results.jsonl"
KST = ZoneInfo("Asia/Seoul")

# callback_data 접두사. 64바이트 한도 안에서 `quiz|{12자 id}|{문항}|{선택}` = 약 22바이트.
CALLBACK_PREFIX = "quiz"


def encode_callback(item_id: str, question_index: int, choice_index: int) -> str:
    return f"{CALLBACK_PREFIX}|{item_id}|{question_index}|{choice_index}"


def parse_callback(data: str) -> tuple[str, int, int] | None:
    """`quiz|{item_id}|{q}|{choice}` → (item_id, q, choice). 형식이 틀리면 None.

    사용자가 아니라 Telegram이 주는 값이지만, 봇 토큰만 알면 누구나 임의의
    callback_data를 보낼 수 있으므로 방어적으로 판다.
    """
    parts = data.split("|")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        return None
    item_id = parts[1].strip()
    if not item_id:
        return None
    try:
        question_index = int(parts[2])
        choice_index = int(parts[3])
    except ValueError:
        return None
    if question_index < 0 or choice_index < 0:
        return None
    return item_id, question_index, choice_index


def record_answer(
    item_id: str,
    question_index: int,
    choice_index: int,
    index: dict[str, ItemFacts] | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    """응답 한 건을 채점해 JSONL에 덧붙인다. 정답을 못 찾으면 None.

    채점 불가(정본에 없는 아이템, 범위 밖 문항)일 때 기록하지 않는 이유: 정답
    없이 남긴 줄은 나중에 채점할 방법이 없어 라벨이 아니라 잡음이 된다.
    """
    lookup = index if index is not None else build_item_index()
    facts = lookup.get(item_id)
    if facts is None or question_index >= len(facts.quiz_answers):
        logger.warning(
            "quiz_answer_unresolved",
            item_id=item_id,
            question_index=question_index,
            reason="정본에서 정답을 찾지 못함",
        )
        return None

    answer_index = facts.quiz_answers[question_index]
    result = {
        "item_id": item_id,
        "url": facts.url,
        "question_index": question_index,
        "choice_index": choice_index,
        "answer_index": answer_index,
        "correct": choice_index == answer_index,
        # 습득도(§4.2 ⑥)의 집계 키. 개념이 본체이고 태그는 보조다 — 태그는
        # 1회성 고유명사라 같은 값으로 두 번 틀릴 일이 거의 없어 표본이 안 쌓인다.
        "concepts": list(facts.concepts),
        "tags": list(facts.tags),
        "answered_at": datetime.now(KST).isoformat(),
    }

    target = path or RESULTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False) + "\n")

    logger.info(
        "quiz_answer_recorded",
        item_id=item_id,
        question_index=question_index,
        correct=result["correct"],
    )
    # 파일에는 라벨만 남기고, 호출부에는 되돌려줄 문항 원문까지 얹어 준다.
    question = facts.quiz[question_index] if question_index < len(facts.quiz) else {}
    options = [str(o) for o in (question.get("options") or [])]

    def option(i: int) -> str:
        return options[i] if 0 <= i < len(options) else ""

    return {
        **result,
        "title": facts.title,
        "question": str(question.get("question") or ""),
        "choice_text": option(choice_index),
        "answer_text": option(answer_index),
        "explanation": str(question.get("explanation") or ""),
    }


CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _circled(index: int) -> str:
    return CIRCLED[index] if 0 <= index < len(CIRCLED) else str(index + 1)


def format_quiz_recap(details: list[dict[str, Any]], limit: int = 1900) -> list[str]:
    """어제 퀴즈 채점 결과를 사람이 읽을 메시지 묶음으로 만든다.

    Discord Poll은 투표 결과만 보여주고 **정답을 알려주지 않는다.** 스포일러를
    붙여두긴 했지만 눌러야 보이고, 투표한 뒤엔 다시 찾아가야 한다. 다음 날
    아침에 한 번에 되돌려주는 것이 학습 루프의 확인 지점이 된다(사용자 요청
    2026-09-03). 한 메시지 길이 상한을 넘으면 문항 경계에서 나눈다.
    """
    if not details:
        return []
    correct = sum(1 for d in details if d.get("correct"))
    header = f"🧠 **어제 퀴즈 결과 {correct}/{len(details)}**"

    blocks: list[str] = []
    for d in details:
        mark = "✅" if d.get("correct") else "❌"
        title = str(d.get("title") or d.get("item_id") or "")[:50]
        lines = [f"{mark} **{title}**"]
        if d.get("question"):
            lines.append(f"Q. {d['question']}")
        try:
            mine = f"{_circled(int(d.get('choice_index')))} {d.get('choice_text', '')}".strip()
            answer = f"{_circled(int(d.get('answer_index')))} {d.get('answer_text', '')}".strip()
        except (TypeError, ValueError):
            mine, answer = "", ""
        if d.get("correct"):
            lines.append(f"내 답 {mine}")
        else:
            lines.append(f"내 답 {mine} → 정답 {answer}")
        if d.get("explanation"):
            lines.append(f"-# {d['explanation']}")
        blocks.append("\n".join(lines))

    messages: list[str] = []
    current = header
    for block in blocks:
        if len(current) + 2 + len(block) > limit and current != header:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    messages.append(current)
    return messages


def load_results(path: Path | None = None) -> list[dict[str, Any]]:
    """기록을 읽어 (item_id, question_index)별 마지막 응답만 남긴다.

    append-only라 답을 바꾸면 줄이 여러 개 쌓인다. 최종 판단은 마지막 것이다.
    깨진 줄은 건너뛴다 — 한 줄 때문에 라벨 전체를 잃으면 안 된다.
    """
    target = path or RESULTS_PATH
    if not target.exists():
        return []

    latest: dict[tuple[str, int], dict[str, Any]] = {}
    with target.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            key = (str(row.get("item_id")), int(row.get("question_index", -1)))
            latest[key] = row

    return list(latest.values())


def _accuracy(field: str, path: Path | None = None) -> dict[str, dict[str, int | float]]:
    buckets: dict[str, list[bool]] = {}
    for row in load_results(path):
        for key in row.get(field) or []:
            buckets.setdefault(str(key).strip().lower(), []).append(
                bool(row.get("correct"))
            )
    return {
        key: {
            "answered": len(marks),
            "correct": sum(marks),
            "accuracy": round(sum(marks) / len(marks), 4),
        }
        for key, marks in sorted(buckets.items())
        if marks
    }


def accuracy_by_concept(path: Path | None = None) -> dict[str, dict[str, int | float]]:
    """개념별 정답률 = 습득도(§4.2 ⑥).

    개념은 어휘로 해소돼 재사용되므로 같은 키에 표본이 누적된다. 태그로는
    이게 안 됐다 — `glm-5.3-flash`로 두 번 틀릴 일이 없기 때문이다.
    """
    return _accuracy("concepts", path)


def accuracy_by_tag(path: Path | None = None) -> dict[str, dict[str, int | float]]:
    """태그별 정답률. 개념 어휘가 얇은 초기의 보조 지표."""
    return _accuracy("tags", path)


# 습득도가 낮다고 판단하기 위한 최소 응답 수. 1~2건으로 "약한 개념"을 정하면
# 우연한 오답 하나가 다음 며칠의 큐레이션을 끌고 간다.
MIN_ANSWERS_FOR_MASTERY = 3

# 이 정답률 미만이면 복습이 필요한 개념으로 본다.
WEAK_CONCEPT_ACCURACY = 0.6


def weak_concepts(
    path: Path | None = None,
    min_answers: int = MIN_ANSWERS_FOR_MASTERY,
    threshold: float = WEAK_CONCEPT_ACCURACY,
) -> set[str]:
    """복습이 필요한 개념 = 표본이 충분한데 정답률이 낮은 것 (§4.2 ⑦).

    이 집합이 선정 단계로 되먹여지면서 루프가 닫힌다: 퀴즈를 틀린 개념의
    후속 콘텐츠가 다음 다이제스트에서 우선순위를 얻는다.
    """
    return {
        concept
        for concept, stat in accuracy_by_concept(path).items()
        if int(stat["answered"]) >= min_answers
        and float(stat["accuracy"]) < threshold
    }
