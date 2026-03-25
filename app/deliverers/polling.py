"""
Telegram 인라인 버튼 콜백 폴링 모듈 (Method A: getUpdates)

FastAPI 앱 시작 시 백그라운드 태스크로 실행.
- 2초 간격으로 getUpdates 호출
- like / dislike / keyword 콜백 처리 → feedback 저장
- 키워드 입력 대기 상태: in-memory dict {chat_id: item_url}
"""
import asyncio
import httpx
import structlog

from app.models import FeedbackPayload
from app.feedback import process_feedback

logger = structlog.get_logger()

# 키워드 입력 대기 중인 사용자 {chat_id: item_url}
_awaiting_keyword: dict[int, str] = {}
_last_update_id: int = 0


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _answer_callback(
    client: httpx.AsyncClient, token: str, cq_id: str, text: str
) -> None:
    try:
        await client.post(_api_url(token, "answerCallbackQuery"), json={
            "callback_query_id": cq_id,
            "text": text,
            "show_alert": False,
        })
    except Exception as e:
        logger.warning("answer_callback_failed", error=str(e))


async def _send_text(
    client: httpx.AsyncClient, token: str, chat_id: int, text: str
) -> None:
    try:
        await client.post(_api_url(token, "sendMessage"), json={
            "chat_id": chat_id,
            "text": text,
        })
    except Exception as e:
        logger.warning("send_text_failed", error=str(e))


async def _handle_update(
    client: httpx.AsyncClient, token: str, update: dict
) -> None:
    global _last_update_id
    _last_update_id = max(_last_update_id, update.get("update_id", 0))

    # ── 인라인 버튼 콜백 ────────────────────────────────────────────────────
    if cq := update.get("callback_query"):
        cq_id = cq["id"]
        chat_id: int = cq["message"]["chat"]["id"]
        data: str = cq.get("data", "")

        try:
            action, item_url = data.split("|", 1)
        except ValueError:
            return

        if action in ("like", "dislike"):
            process_feedback(FeedbackPayload(item_url=item_url, action=action))
            reply = "👍 반영했어요!" if action == "like" else "👎 다음엔 더 좋은 걸 찾아볼게요"
            await _answer_callback(client, token, cq_id, reply)
            logger.info("telegram_feedback", action=action, url=item_url[:60])

        elif action == "keyword":
            _awaiting_keyword[chat_id] = item_url
            await _answer_callback(client, token, cq_id, "키워드를 채팅으로 입력해주세요 📝")

    # ── 일반 텍스트 메시지 — 키워드 대기 상태 확인 ──────────────────────────
    elif msg := update.get("message"):
        chat_id = msg["chat"]["id"]
        text: str = msg.get("text", "").strip()

        if not text or text.startswith("/"):
            return

        if chat_id in _awaiting_keyword:
            item_url = _awaiting_keyword.pop(chat_id)
            process_feedback(FeedbackPayload(
                item_url=item_url,
                action="keyword_request",
                keyword=text,
            ))
            await _send_text(client, token, chat_id, f"✅ '{text}' 키워드가 등록됐어요!")
            logger.info("telegram_keyword_saved", keyword=text)


async def poll_once(client: httpx.AsyncClient, token: str) -> None:
    """getUpdates 1회 호출 후 업데이트 처리."""
    global _last_update_id
    try:
        resp = await client.get(
            _api_url(token, "getUpdates"),
            params={
                "offset": _last_update_id + 1,
                "timeout": 20,
                "allowed_updates": ["callback_query", "message"],
            },
            timeout=30,
        )
        data = resp.json()
        if data.get("ok"):
            for update in data.get("result", []):
                await _handle_update(client, token, update)
    except Exception as e:
        logger.warning("telegram_poll_error", error=str(e))


async def start_polling() -> None:
    """
    백그라운드 폴링 루프. FastAPI lifespan에서 asyncio.create_task()로 실행.
    TELEGRAM_BOT_TOKEN이 없으면 즉시 종료.
    """
    from app.config import get_settings
    token = get_settings().telegram_bot_token
    if not token:
        logger.info("telegram_polling_skipped", reason="TELEGRAM_BOT_TOKEN 미설정")
        return

    logger.info("telegram_polling_started")
    async with httpx.AsyncClient() as client:
        while True:
            await poll_once(client, token)
            await asyncio.sleep(2)
