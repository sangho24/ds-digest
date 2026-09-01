"""
Telegram 인라인 버튼 콜백 폴링 모듈 (getUpdates)

FastAPI 앱 시작 시 백그라운드 태스크로 실행 (실시간 모드).
GitHub Actions 파이프라인 시작 시 poll_once() 1회 호출 (배치 모드).

처리 항목:
- like / dislike 콜백 → feedback 저장
- quiz 콜백 → 채점 후 data/quiz_results.jsonl 기록
- /keyword <텍스트> 명령어 → keyword_request 저장
- 그 외 자유 텍스트 → 자연어 지시로 data/directives.jsonl 축적
"""
import asyncio
import httpx
import structlog

from app.models import FeedbackPayload
from app.feedback import process_feedback
from app.directives import capture as capture_directive
from app.preferences import resolve_feedback_target
from app.quiz_results import parse_callback as parse_quiz_callback, record_answer

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

    # ── 인라인 버튼 콜백 (quiz / like / dislike) ──────────────────────────
    if cq := update.get("callback_query"):
        cq_id = cq["id"]
        data: str = cq.get("data", "")

        # ── 퀴즈 응답 ──────────────────────────────────────────────────────
        # 이 시스템의 유일한 ground truth다(§7.5). 취향 신호와 달리 맞고 틀림이
        # 있어서 개념별 습득도를 계산할 수 있다.
        if parsed := parse_quiz_callback(data):
            target, question_index, choice_index = parsed
            result = record_answer(target, question_index, choice_index)
            if result is not None:
                summary["quiz_answers"] += 1
                if result["correct"]:
                    summary["quiz_correct"] += 1
                # 배치 폴링이라 대개 만료된 뒤라 토스트는 안 뜬다. 실시간 서버
                # 모드에서만 의미가 있으므로 best-effort로만 보낸다.
                await _answer_callback(
                    client, token, cq_id,
                    "⭕ 정답!" if result["correct"] else "❌ 오답",
                )
            return

        try:
            # `target`은 아이템 식별자다. 봇 토큰(`token`) 파라미터를 가리지
            # 않도록 이름을 분리한다 — 가리면 _answer_callback이 아이템 id를
            # 봇 토큰 자리에 넣어 호출한다.
            action, target = data.split("|", 1)
        except ValueError:
            return

        if action in ("like", "dislike"):
            # callback_data는 64바이트 한도 때문에 12자 item_id를 싣는다.
            # 프로필에는 URL로 쌓아야 나중에 정본과 대조할 수 있으므로 되돌린다.
            # (한도 도입 이전의 버튼은 URL을 그대로 실었는데, resolve가 미지의
            #  토큰을 그대로 통과시키므로 그 시절 콜백도 계속 처리된다.)
            item_url = resolve_feedback_target(target)
            process_feedback(FeedbackPayload(item_url=item_url, action=action))
            if action == "like":
                summary["likes"] += 1
            else:
                summary["dislikes"] += 1
            # 실시간 서버 모드에서만 토스트가 즉각 전달됨
            reply = "👍 반영됐어요!" if action == "like" else "👎 알겠어요!"
            await _answer_callback(client, token, cq_id, reply)
            logger.info(
                "telegram_feedback", action=action, target=target, url=item_url[:60]
            )

    # ── 일반 텍스트 메시지 — /keyword 명령어 + 자연어 지시 ─────────────────
    elif msg := update.get("message"):
        text: str = msg.get("text", "").strip()

        if not text:
            return

        if text.lower().startswith(("/help", "/start")):
            summary["help_requested"] = True
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
            return

        # 그 외 자유 텍스트는 자연어 지시로 쌓는다.
        # 예전엔 여기서 그냥 버렸고, 업데이트를 acknowledge까지 해서 텔레그램
        # 서버에서도 지워졌다 — "논문 말고 실무 사례 위주로" 같은 말이 흔적 없이
        # 증발했다. 해석은 다음 런 시작 시 한 번에 한다(app/directives.py).
        if text.startswith("/"):
            return  # 알 수 없는 명령어는 지시가 아니다
        if capture_directive(text):
            summary["directives"].append(text[:60])


async def poll_once(client: httpx.AsyncClient, token: str) -> dict:
    """
    getUpdates 1회 호출 후 업데이트 처리.
    처리 결과 summary 반환:
        {"likes": N, "dislikes": N, "keywords": [...],
         "quiz_answers": N, "quiz_correct": N, "directives": [...]}

    처리 후 acknowledge 호출로 동일 업데이트 재처리 방지.
    """
    global _last_update_id
    summary: dict = {
        "likes": 0,
        "dislikes": 0,
        "keywords": [],
        "quiz_answers": 0,
        "quiz_correct": 0,
        "directives": [],
        "help_requested": False,
    }
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
