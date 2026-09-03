"""Discord 피드백 수거 — REST 배치 폴링.

Telegram의 getUpdates와 같은 자리를 맡는다. 파이프라인 시작 시 1회 호출한다.

두 갈래로 읽는 이유:
    1) **새 사람 메시지(자연어 지시)** — 커서 이후만 읽으면 된다. 한 번 읽은
       메시지가 나중에 바뀌지 않기 때문이다. `?after={커서}`로 새것만 가져오고
       커서를 전진시킨다.

    2) **최근 봇 메시지의 피드백** — 커서 방식을 쓸 수 없다. 어제 보낸 아이템에
       오늘 👍가 눌릴 수 있는데, 커서가 이미 지나가 있으면 영영 못 본다.
       그래서 최근 N건을 매번 다시 읽는다. 재처리는 무해하다 — 프로필 append는
       이미 중복을 막고, 퀴즈 기록도 같은 답이면 건너뛴다.

리액션 카운트 해석:
    발송할 때 봇이 👍👎를 미리 달아둔다(사용자가 한 번만 누르면 되도록).
    따라서 **카운트 2 이상**이 사용자가 눌렀다는 뜻이다. 단일 사용자 시스템이라
    누가 눌렀는지까지 확인할 필요는 없다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.config import get_settings
from app.deliverers.discord import (
    API,
    DISLIKE_EMOJI,
    LIKE_EMOJI,
    _headers,
    load_message_map,
)
from app.directives import capture as capture_directive
from app.feedback import process_feedback
from app.models import FeedbackPayload
from app.quiz_results import load_results, record_answer

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent.parent
CURSOR_PATH = ROOT / "data" / "discord_cursor.json"

# 피드백을 다시 읽을 최근 메시지 수. 하루 발송량이 헤더 1 + 아이템 5 + 퀴즈 8
# 남짓이므로 100이면 최근 일주일가량을 덮는다.
RECENT_LIMIT = 100


def _load_cursor() -> str | None:
    if not CURSOR_PATH.exists():
        return None
    try:
        return json.loads(CURSOR_PATH.read_text(encoding="utf-8")).get("last_message_id")
    except (OSError, json.JSONDecodeError):
        return None


def _save_cursor(message_id: str) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(
        json.dumps({"last_message_id": str(message_id)}, ensure_ascii=False),
        encoding="utf-8",
    )


async def _get_messages(client: httpx.AsyncClient, **params: Any) -> list[dict]:
    settings = get_settings()
    try:
        resp = await client.get(
            f"{API}/channels/{settings.discord_channel_id}/messages",
            headers=_headers(),
            params={"limit": RECENT_LIMIT, **params},
        )
        if resp.status_code >= 400:
            logger.warning("discord_read_failed", status=resp.status_code, body=resp.text[:200])
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("discord_read_error", error=str(e))
        return []


def _reaction_count(message: dict, emoji: str) -> int:
    for reaction in message.get("reactions") or []:
        if ((reaction.get("emoji") or {}).get("name")) == emoji:
            try:
                return int(reaction.get("count") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


async def _collect_reactions(messages: list[dict], mapping: dict, summary: dict) -> None:
    """아이템 메시지의 👍/👎를 프로필에 반영한다."""
    for msg in messages:
        entry = mapping.get(str(msg.get("id")))
        if not entry or entry.get("kind") != "item":
            continue
        url = entry.get("url")
        if not url:
            continue

        # 봇이 미리 단 1개를 뺀다.
        if _reaction_count(msg, LIKE_EMOJI) > 1:
            process_feedback(FeedbackPayload(item_url=url, action="like"))
            summary["likes"] += 1
        if _reaction_count(msg, DISLIKE_EMOJI) > 1:
            process_feedback(FeedbackPayload(item_url=url, action="dislike"))
            summary["dislikes"] += 1


async def _collect_poll_votes(
    client: httpx.AsyncClient, messages: list[dict], mapping: dict, summary: dict
) -> None:
    """퀴즈 Poll의 투표를 채점해 기록한다.

    메시지 객체에 실려 오는 `poll.results.answer_counts`만 쓴다. 단일 사용자
    시스템이라 "몇 표"가 곧 "그 사람이 골랐나"이고, 투표자 목록을 따로 조회할
    이유가 없다(호출 수도 답변 수만큼 늘어난다).
    """
    settings = get_settings()
    already = {
        (str(r.get("item_id")), int(r.get("question_index", -1)), int(r.get("choice_index", -1)))
        for r in load_results()
    }

    for msg in messages:
        entry = mapping.get(str(msg.get("id")))
        if not entry or entry.get("kind") != "quiz":
            continue
        results = (msg.get("poll") or {}).get("results") or {}
        counts = results.get("answer_counts") or []

        for row in counts:
            try:
                votes = int(row.get("count") or 0)
                answer_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            if votes <= 0:
                continue

            # Discord answer_id는 1부터 시작한다 — 우리 선지 인덱스는 0부터다.
            choice_index = answer_id - 1
            key = (str(entry.get("item_id")), int(entry.get("question_index", -1)), choice_index)
            if key in already:
                continue

            result = record_answer(
                str(entry["item_id"]), int(entry["question_index"]), choice_index
            )
            if result is not None:
                already.add(key)
                summary["quiz_answers"] += 1
                if result["correct"]:
                    summary["quiz_correct"] += 1
                # 다음 날 아침 "어제 퀴즈 결과"로 되돌려준다.
                summary.setdefault("quiz_details", []).append(result)


async def poll_once(client: httpx.AsyncClient) -> dict:
    """Discord 채널을 1회 읽어 피드백·지시를 수거한다."""
    settings = get_settings()
    summary: dict = {
        "likes": 0,
        "dislikes": 0,
        "keywords": [],
        "quiz_answers": 0,
        "quiz_correct": 0,
        "directives": [],
        "help_requested": False,
    }
    if not settings.discord_bot_token or not settings.discord_channel_id:
        return summary

    mapping = load_message_map()

    # (1) 최근 메시지 — 리액션·투표는 나중에 바뀌므로 매번 다시 읽는다.
    recent = await _get_messages(client)
    await _collect_reactions(recent, mapping, summary)
    await _collect_poll_votes(client, recent, mapping, summary)

    # (2) 새 사람 메시지 — 커서 이후만.
    cursor = _load_cursor()
    fresh = await _get_messages(client, after=cursor) if cursor else recent
    newest = cursor

    # Discord는 최신순으로 준다. 오래된 것부터 처리해야 지시 순서가 보존된다.
    for msg in sorted(fresh, key=lambda m: int(m.get("id", 0))):
        message_id = str(msg.get("id"))
        newest = message_id if newest is None or int(message_id) > int(newest) else newest

        if (msg.get("author") or {}).get("bot"):
            continue
        text = str(msg.get("content") or "").strip()
        if not text:
            continue

        if text.lower().startswith(("/help", "/start")):
            summary["help_requested"] = True
            continue

        if text.lower().startswith("/keyword"):
            keyword = text[len("/keyword"):].strip()
            if keyword:
                process_feedback(
                    FeedbackPayload(action="keyword_request", keyword=keyword)
                )
                summary["keywords"].append(keyword)
            continue

        if text.startswith("/"):
            continue  # 알 수 없는 명령어는 지시가 아니다

        if capture_directive(text):
            summary["directives"].append(text[:60])

    if newest:
        _save_cursor(newest)

    logger.info(
        "discord_poll_done",
        **{k: v for k, v in summary.items() if v and k != "quiz_details"},
    )
    return summary
