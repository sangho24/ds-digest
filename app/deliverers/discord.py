"""Discord 발송 — 게이트웨이 없이 REST만으로 도는 배치 모델.

왜 게이트웨이가 필요 없나:
    "리액션을 읽으려면 게이트웨이가 필요하다"는 말은 **실시간 이벤트** 얘기다.
    현재 상태 조회는 전부 REST로 된다.

        GET /channels/{id}/messages          메시지 객체에 reactions 배열(카운트 포함)
        GET /channels/{id}/polls/{msg}/answers/{aid}   답변별 투표자 목록

    파이프라인이 GitHub Actions에서 하루 한 번 도는 배치이므로, Telegram의
    getUpdates와 정확히 같은 모델로 맞아떨어진다. 상시 프로세스가 필요 없다.

메시지 ↔ 아이템 연결:
    어느 메시지가 어느 아이템·문항인지 알아야 피드백을 귀속시킬 수 있다.
    본문에 id를 박으면 사용자 눈에 지저분하게 보이므로, 발송 시점에 매핑을
    `data/discord_messages.jsonl`에 남긴다. 이 리포의 다른 상태(퀴즈 라벨·지시·
    개념 어휘)와 같은 방식이고, 러너가 ephemeral이라 커밋돼야 살아남는다.

퀴즈는 왜 네이티브 Poll인가:
    Discord Poll은 투표 UI가 붙고 결과가 그 자리에서 보인다. 리액션보다 오답
    선택을 정확히 잡을 수 있고, `Get Answer Voters`로 누가 무엇을 골랐는지
    REST로 되읽을 수 있다.

    제약: 선지 최대 55자, 질문 최대 300자, 답변 최대 10개. 초과분은 잘라서 보낸다.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.config import get_settings
from app.mathtext import to_readable
from app.models import DigestItem
from app.preferences import item_id

logger = structlog.get_logger()

API = "https://discord.com/api/v10"
ROOT = Path(__file__).resolve().parent.parent.parent
MESSAGE_MAP_PATH = ROOT / "data" / "discord_messages.jsonl"
KST = ZoneInfo("Asia/Seoul")

# Discord 제약. 넘기면 400이 나므로 보내기 전에 자른다.
MAX_CONTENT = 2000
MAX_POLL_QUESTION = 300
MAX_POLL_ANSWER = 55
MAX_POLL_ANSWERS = 10

LIKE_EMOJI = "👍"
DISLIKE_EMOJI = "👎"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {get_settings().discord_bot_token}",
        "Content-Type": "application/json",
    }


def _truncate(text: str, limit: int) -> str:
    # LaTeX 흔적을 먼저 정리한다. Discord는 수식을 렌더링하지 않아서
    # `_{μ,d}^{(α)}` 같은 표기가 그대로 화면에 찍힌다(실측 2026-09-02 퀴즈).
    # 자르기 전에 해야 변환이 잘린 중괄호에 걸려 깨지지 않는다.
    text = to_readable(str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def record_message(kind: str, message_id: str, **fields: Any) -> None:
    """메시지 ↔ 아이템 매핑을 남긴다. 피드백 귀속의 유일한 근거다."""
    entry = {
        "kind": kind,
        "message_id": str(message_id),
        "recorded_at": datetime.now(KST).isoformat(),
        **fields,
    }
    MESSAGE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MESSAGE_MAP_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_message_map(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """{message_id: 매핑}. 같은 id가 여러 번 나오면 마지막 것을 쓴다."""
    target = path or MESSAGE_MAP_PATH
    if not target.exists():
        return {}

    mapping: dict[str, dict[str, Any]] = {}
    with target.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("message_id"):
                mapping[str(row["message_id"])] = row
    return mapping


async def _retry_after(resp: httpx.Response) -> float:
    """429 응답이 알려주는 대기 시간(초). 없으면 짧은 기본값."""
    try:
        value = float((resp.json() or {}).get("retry_after", 0))
    except Exception:
        value = 0.0
    # 헤더가 더 정확할 때가 있어 둘 중 큰 값을 쓴다.
    try:
        value = max(value, float(resp.headers.get("retry-after", 0)))
    except (TypeError, ValueError):
        pass
    return min(max(value, 0.5), 10.0)


async def _post(client: httpx.AsyncClient, payload: dict) -> dict | None:
    """채널에 메시지를 보내고 생성된 메시지 객체를 돌려준다. 실패는 None.

    429는 재시도한다. 다이제스트 한 번에 헤더 1 + 아이템 5 + 퀴즈 여러 건을
    연달아 쏘기 때문에 Discord의 채널당 레이트리밋에 정상적으로 걸린다 —
    실측 2026-09-02 발송에서 실제로 한 건이 429로 **조용히 사라졌다**.
    아이템 메시지가 사라지면 매핑도 안 남아서 그 아이템의 피드백은 영영
    귀속되지 않는다. 응답이 기다릴 시간(retry_after)을 알려주므로 그대로 쉰다.
    """
    settings = get_settings()
    for attempt in range(3):
        try:
            resp = await client.post(
                f"{API}/channels/{settings.discord_channel_id}/messages",
                headers=_headers(),
                json=payload,
            )
            if resp.status_code == 429 and attempt < 2:
                wait = await _retry_after(resp)
                logger.warning("discord_rate_limited", wait=wait, attempt=attempt + 1)
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 400:
                logger.error("discord_send_failed", status=resp.status_code, body=resp.text[:300])
                return None
            return resp.json()
        except Exception as e:
            logger.error("discord_request_failed", error=str(e))
            return None
    return None


async def _react(client: httpx.AsyncClient, message_id: str, emoji: str) -> None:
    """봇이 먼저 리액션을 달아둔다 — 사용자가 한 번만 누르면 되도록.

    카운트가 2 이상이면 사용자가 눌렀다는 뜻이다(봇 자신의 1을 뺀다).
    """
    settings = get_settings()
    from urllib.parse import quote

    for attempt in range(3):
        try:
            resp = await client.put(
                f"{API}/channels/{settings.discord_channel_id}/messages/"
                f"{message_id}/reactions/{quote(emoji)}/@me",
                headers=_headers(),
            )
            # 리액션도 같은 레이트리밋을 공유한다. 여기서 조용히 실패하면
            # 사용자가 직접 이모지를 찾아 눌러야 하고(카운트 1이 없으니),
            # 수거 쪽은 "카운트 2 이상"을 사용자 입력으로 보므로 신호가 어긋난다.
            if resp.status_code == 429 and attempt < 2:
                await asyncio.sleep(await _retry_after(resp))
                continue
            if resp.status_code >= 400:
                logger.warning("discord_react_failed", status=resp.status_code, emoji=emoji)
            return
        except Exception as e:
            logger.warning("discord_react_failed", error=str(e), emoji=emoji)
            return


def _format_header(items: list[DigestItem]) -> str:
    today = datetime.now(KST)
    yt = sum(1 for i in items if i.raw.source_type.value == "youtube")
    rss = len(items) - yt
    parts = []
    if yt:
        parts.append(f"📹 {yt}")
    if rss:
        parts.append(f"📰 {rss}")
    breakdown = "  " + " · ".join(parts) if parts else ""
    return (
        f"## 📬 DS Digest — {today.month}월 {today.day}일\n"
        f"오늘의 큐레이션 {len(items)}건{breakdown}\n"
        f"-# 💡 이 채널에 그냥 한국말로 말해주세요 — "
        f"\"논문보다 실무 사례 위주로\" 같은 지시가 다음 날 반영됩니다. `/help`"
    )


def _format_item(item: DigestItem, index: int) -> str:
    a, r = item.analysis, item.raw
    lines = [f"**{index}. {r.title}**", f"> {a.one_line_summary}"]
    # 논문이면 배경·위치를 요약 바로 아래에. 요약과 핵심만으로는 이 논문이 왜
    # 지금 여기 실렸는지가 안 보인다는 피드백(2026-09-03).
    if a.positioning:
        lines.append(f"> 📍 {a.positioning}")
    lines.append("")

    if a.key_points:
        for kp in a.key_points[:3]:
            stamp = f"`{kp.timestamp}` " if kp.timestamp else ""
            lines.append(f"· {stamp}{kp.point}")
        lines.append("")

    if a.production_ideas:
        lines.append("**적용 아이디어**")
        for idea in a.production_ideas[:2]:
            lines.append(f"· {idea}")
        lines.append("")

    meta = [r.source_label or r.source_name, f"관련도 {a.relevance_score}"]
    if a.concepts:
        meta.append(" / ".join(a.concepts))
    lines.append(f"-# {' · '.join(meta)}")
    lines.append(f"<{r.url}>")
    return _truncate("\n".join(lines), MAX_CONTENT)


CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _quiz_caption(item: DigestItem, q_index: int, number: int) -> str:
    """퀴즈 메시지 본문 — 정답과 해설을 스포일러로 붙인다.

    Discord Poll은 **어느 선지가 정답인지 알려주지 않는다.** 투표 결과만
    보여준다. 그래서 정답을 따로 붙이지 않으면 맞았는지 확인할 방법이 없고,
    학습용 퀴즈가 그냥 설문이 된다(Telegram판은 tg-spoiler로 같이 보냈다).

    스포일러로 감싸므로 눌러야 보인다 — 투표 전에 눈에 먼저 들어오지 않는다.
    """
    q = item.analysis.quiz[q_index]
    try:
        marker = CIRCLED[q.answer_index]
        answer = q.options[q.answer_index]
    except (IndexError, TypeError):
        return f"🧠 **퀴즈 {number}**"

    body = f"{marker} {answer}"
    if q.explanation:
        body += f" — {q.explanation}"
    # 스포일러 안에 || 가 들어가면 태그가 깨진다.
    body = body.replace("||", "│")
    head = f"🧠 **퀴즈 {number}**\n||정답: "
    # 본문만 자른다. 전체를 자르면 닫는 || 가 잘려 스포일러가 열리고 정답이
    # 그대로 보인다 — 숨기려던 것이 노출되는 쪽이 최악이다.
    body = _truncate(body, MAX_CONTENT - len(head) - 2)
    return f"{head}{body}||"


def _select_quiz(items: list[DigestItem], limit: int) -> list[tuple[DigestItem, int]]:
    """발송할 (아이템, 문항 인덱스) 목록. 아이템을 번갈아 채운다.

    문항을 아이템 순서대로 다 붙이면 앞쪽 아이템만 여러 문항을 갖고 뒤쪽은
    한 문항도 못 낸다. 번갈아 채우면 상한이 걸려도 모든 아이템이 최소 한
    문항은 낸다.

    상한이 필요한 이유: 아이템 5건 × 2문항이면 헤더 1 + 아이템 5 + 퀴즈 10 =
    16개 메시지를 연달아 쏜다. 채널이 퀴즈로 뒤덮이고, Discord 레이트리밋에도
    걸린다(실측 2026-09-02 발송에서 한 건이 429로 사라졌다).
    """
    if limit <= 0:
        return []
    picked: list[tuple[DigestItem, int]] = []
    depth = 0
    while len(picked) < limit:
        added = False
        for item in items:
            if depth < len(item.analysis.quiz):
                picked.append((item, depth))
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        depth += 1
    return picked


def _build_poll(item: DigestItem, q_index: int) -> dict | None:
    """문항 하나를 Discord 네이티브 Poll로 만든다. 선지가 부족하면 None."""
    quiz = item.analysis.quiz
    if q_index >= len(quiz):
        return None
    q = quiz[q_index]
    options = [o for o in q.options if str(o).strip()][:MAX_POLL_ANSWERS]
    if len(options) < 2:
        return None

    return {
        "question": {"text": _truncate(q.question, MAX_POLL_QUESTION)},
        "answers": [
            {"poll_media": {"text": _truncate(opt, MAX_POLL_ANSWER)}} for opt in options
        ],
        # 24시간이면 다음 배치 폴링 전에 아직 열려 있고, 결과도 계속 읽힌다.
        "duration": 24,
        "allow_multiselect": False,
    }


async def send_discord_digest(items: list[DigestItem]) -> bool:
    """다이제스트를 Discord로 발송하고 메시지 매핑을 남긴다."""
    settings = get_settings()
    if not settings.discord_bot_token or not settings.discord_channel_id:
        logger.warning(
            "discord_not_configured",
            hint="DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID 설정 필요",
        )
        return False

    if settings.dry_run:
        logger.info("dry_run_skip_discord", items=len(items))
        return True

    async with httpx.AsyncClient(timeout=20) as client:
        if not await _post(client, {"content": _format_header(items)}):
            return False

        for index, item in enumerate(items, 1):
            msg = await _post(client, {"content": _format_item(item, index)})
            if not msg:
                continue
            record_message("item", msg["id"], item_id=item_id(item.raw.url), url=item.raw.url)
            # 👍/👎를 미리 달아둔다. 사용자는 한 번만 누르면 된다.
            await _react(client, msg["id"], LIKE_EMOJI)
            await _react(client, msg["id"], DISLIKE_EMOJI)

        # 퀴즈 — 문항마다 네이티브 Poll 하나. 아이템을 번갈아 상한까지만.
        number = 0
        for item, q_index in _select_quiz(items, settings.max_quiz_per_digest):
            poll = _build_poll(item, q_index)
            if poll is None:
                continue
            number += 1
            msg = await _post(
                client,
                {"content": _quiz_caption(item, q_index, number), "poll": poll},
            )
            if not msg:
                continue
            record_message(
                "quiz",
                msg["id"],
                item_id=item_id(item.raw.url),
                question_index=q_index,
                answer_index=item.analysis.quiz[q_index].answer_index,
            )

    logger.info("discord_digest_sent", items=len(items), channel=settings.discord_channel_id)
    return True


async def send_discord_text(text: str) -> bool:
    """알림·요약용 단문 발송."""
    settings = get_settings()
    if not settings.discord_bot_token or not settings.discord_channel_id or settings.dry_run:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        return await _post(client, {"content": _truncate(text, MAX_CONTENT)}) is not None
