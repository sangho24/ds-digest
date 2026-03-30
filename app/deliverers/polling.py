"""
Telegram 인라인 버튼 콜백 폴링 모듈 (getUpdates)

FastAPI 앱 시작 시 백그라운드 태스크로 실행 (실시간 모드).
GitHub Actions 파이프라인 시작 시 poll_once() 1회 호출 (배치 모드).

처리 항목:
- like / dislike 콜백 → feedback 저장
- /keyword <텍스트> 명령어 → keyword_request 저장
"""
import asyncio
import httpx
import structlog

from app.models import FeedbackPayload
from app.feedback import process_feedback

logger = structlog.get_logger()

_last_update_id: int = 0


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _answer_callback(
    client: httpx.AsyncClient, token: str, cq_id: str, text: str
) -> None:
    """인라인 버튼 클릭에 토스트 응답 (실시간 서버 모드에서만 유효)."""
    try:
        await client.post(_api_url(token, "answerCallbackQuery"), json={
            "callback_query_id": cq_id,
            "text": text,
            "show_alert": False,
        })
    except Exception as e:
        logger.warning("answer_callback_failed", error=str(e))


async def _handle_update(
    client: httpx.AsyncClient, token: str, update: dict, summary: dict
) -> None:
    global _last_update_id
    _last_update_id = max(_last_update_id, update.get("update_id", 0))

    # ── 인라인 버튼 콜백 (like / dislike) ──────────────────────────────────
    if cq := update.get("callback_query"):
        cq_id = cq["id"]
        data: str = cq.get("data", "")

        try:
            action, item_url = data.split("|", 1)
        except ValueError:
            return

        if action in ("like", "dislike"):
            process_feedback(FeedbackPayload(item_url=item_url, action=action))
            if action == "like":
                summary["likes"] += 1
            else:
                summary["dislikes"] += 1
            # 실시간 서버 모드에서만 토스트가 즉각 전달됨
            reply = "👍 반영됐어요!" if action == "like" else "👎 알겠어요!"
            await _answer_callback(client, token, cq_id, reply)
            logger.info("telegram_feedback", action=action, url=item_url[:60])

    # ── 일반 텍스트 메시지 — /keyword 명령어 ───────────────────────────────
    elif msg := update.get("message"):
        text: str = msg.get("text", "").strip()

        if not text:
            return

        if text.lower().startswith("/keyword"):
            keyword = text[len("/keyword"):].strip()
            if keyword:
                process_feedback(FeedbackPayload(
                    action="keyword_request",
                    keyword=keyword,
                ))
                summary["keywords"].append(keyword)
                logger.info("telegram_keyword_saved", keyword=keyword)


async def poll_once(client: httpx.AsyncClient, token: str) -> dict:
    """
    getUpdates 1회 호출 후 업데이트 처리.
    처리 결과 summary 반환: {"likes": N, "dislikes": N, "keywords": [...]}

    처리 후 acknowledge 호출로 동일 업데이트 재처리 방지.
    """
    global _last_update_id
    summary: dict = {"likes": 0, "dislikes": 0, "keywords": []}
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
                await _handle_update(client, token, update, summary)

            # 처리된 업데이트 acknowledge — 다음 실행 시 재처리 방지
            if _last_update_id > 0:
                await client.get(
                    _api_url(token, "getUpdates"),
                    params={"offset": _last_update_id + 1, "timeout": 0},
                    timeout=10,
                )
    except Exception as e:
        logger.warning("telegram_poll_error", error=str(e))
    return summary


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
