"""
Telegram Bot 발송 모듈
httpx로 Bot API 직접 호출 — python-telegram-bot 불필요
"""
import html
import httpx
import structlog
from datetime import date

from app.config import get_settings
from app.models import DigestItem

logger = structlog.get_logger()

_MAX_MSG_LEN = 4096


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


# ──────────────────────────────────────────────
# 메시지 포맷
# ──────────────────────────────────────────────

def _format_header(items: list[DigestItem]) -> str:
    today = date.today()
    yt = sum(1 for i in items if i.raw.source_type.value == "youtube")
    rss = len(items) - yt
    breakdown = []
    if yt: breakdown.append(f"📹 {yt}")
    if rss: breakdown.append(f"📰 {rss}")
    breakdown_str = "  " + " · ".join(breakdown) if breakdown else ""
    return (
        f"📬 <b>DS Digest  {today.month}월 {today.day}일</b>\n"
        f"오늘의 큐레이션 {len(items)}건{breakdown_str}"
    )


def _format_item(item: DigestItem) -> str:
    """DigestItem → Telegram HTML 메시지 (4096자 이하 목표)"""
    a = item.analysis
    raw = item.raw

    src_icon = "📹" if raw.source_type.value == "youtube" else "📰"
    lines = [
        f"{src_icon} <b>{html.escape(raw.title)}</b>",
        f"<i>{html.escape(raw.source_name)} · 관련도 {a.relevance_score}/10</i>",
        "",
        f"<blockquote>{html.escape(a.one_line_summary)}</blockquote>",
    ]

    if a.key_points:
        lines += ["📌 <b>핵심 포인트</b>"]
        for kp in a.key_points:
            ts = f"<code>{kp.timestamp}</code> " if kp.timestamp else ""
            lines.append(f"  • {ts}{html.escape(kp.point)}")

    if a.production_ideas:
        lines += ["", "💡 <b>현업 적용</b>"]
        for idea in a.production_ideas:
            lines.append(f"  • {html.escape(idea)}")

    lines += ["", f'🔗 <a href="{raw.url}">원본 보기</a>']
    return "\n".join(lines)


def _format_quiz(items: list[DigestItem]) -> str | None:
    """퀴즈를 하나의 메시지로 묶기. 4096자 초과 시 None 반환(스킵)."""
    lines = ["🧠 <b>오늘의 퀴즈</b>", ""]
    alpha = ["A", "B", "C", "D"]

    has_quiz = False
    for item in items:
        for q in item.analysis.quiz:
            has_quiz = True
            lines.append(f"<b>Q. {html.escape(q.question)}</b>")
            for i, opt in enumerate(q.options):
                lines.append(f"  {alpha[i]}. {html.escape(opt)}")
            lines.append(f"  ✅ 정답: {alpha[q.answer_index]} — {html.escape(q.explanation)}")
            lines.append("")

    if not has_quiz:
        return None
    text = "\n".join(lines)
    return text if len(text) <= _MAX_MSG_LEN else None


def _item_keyboard(item_url: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "👍", "callback_data": f"like|{item_url}"},
            {"text": "👎", "callback_data": f"dislike|{item_url}"},
            {"text": "📝 키워드", "callback_data": f"keyword|{item_url}"},
        ]]
    }


def _split_message(text: str) -> list[str]:
    """4096자 초과 메시지를 줄 단위로 분리"""
    if len(text) <= _MAX_MSG_LEN:
        return [text]
    chunks, current = [], []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > _MAX_MSG_LEN and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


# ──────────────────────────────────────────────
# API 호출
# ──────────────────────────────────────────────

async def _send_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = await client.post(_api_url(token, "sendMessage"), json=payload)
        data = resp.json()
        if not data.get("ok"):
            logger.error("telegram_send_failed",
                         description=data.get("description"),
                         preview=text[:80])
            return False
        return True
    except Exception as e:
        logger.error("telegram_request_failed", error=str(e))
        return False


# ──────────────────────────────────────────────
# 공개 인터페이스
# ──────────────────────────────────────────────

async def send_telegram_digest(items: list[DigestItem]) -> bool:
    """
    다이제스트를 Telegram으로 발송.
    헤더 1건 + 아이템별 메시지 + 퀴즈 묶음 순서로 전송.
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    if not token or not chat_id:
        logger.warning("telegram_not_configured", hint="TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 설정 필요")
        return False

    if settings.dry_run:
        logger.info("dry_run_skip_telegram", items=len(items))
        return True

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. 헤더
        if not await _send_message(client, token, chat_id, _format_header(items)):
            return False

        # 2. 아이템별
        for item in items:
            chunks = _split_message(_format_item(item))
            for i, chunk in enumerate(chunks):
                # 인라인 키보드는 마지막 청크에만
                keyboard = _item_keyboard(item.raw.url) if i == len(chunks) - 1 else None
                await _send_message(client, token, chat_id, chunk, reply_markup=keyboard)

        # 3. 퀴즈 (선택사항)
        if quiz_text := _format_quiz(items):
            await _send_message(client, token, chat_id, quiz_text)

    logger.info("telegram_digest_sent", items=len(items), chat_id=chat_id)
    return True
