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
        # 개념별 습득도(§4.2 ⑥)를 계산하려면 문항이 어떤 개념에 속하는지가
        # 필요하다. 개념 추출이 아직 없으므로 태그를 그 대리로 남긴다.
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
    return result


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


def accuracy_by_tag(path: Path | None = None) -> dict[str, dict[str, int | float]]:
    """태그별 정답률. 개념 추출이 들어오면 이 함수의 키만 개념으로 바꾸면 된다.

    습득도(§4.2 ⑥→⑦)의 첫 근사다: 정답률이 낮은 태그가 곧 복습이 필요한 영역이다.
    """
    buckets: dict[str, list[bool]] = {}
    for row in load_results(path):
        for tag in row.get("tags") or []:
            buckets.setdefault(str(tag), []).append(bool(row.get("correct")))

    return {
        tag: {
            "answered": len(marks),
            "correct": sum(marks),
            "accuracy": round(sum(marks) / len(marks), 4),
        }
        for tag, marks in sorted(buckets.items())
        if marks
    }
