"""
Telegram Bot 발송 모듈
httpx로 Bot API 직접 호출 — python-telegram-bot 불필요
"""
import html
import httpx
import structlog
from datetime import date

from app.config import get_settings
from app.contract import today_kst
from app.preferences import item_id
from app.quiz_results import encode_callback as encode_quiz_callback
from app.models import DigestItem

logger = structlog.get_logger()

_MAX_MSG_LEN = 4096


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


# ──────────────────────────────────────────────
# 메시지 포맷
# ──────────────────────────────────────────────

def _format_header(items: list[DigestItem]) -> str:
    # KST 기준. UTC 러너에서 `date.today()` 는 하루 전 날짜가 된다(이메일과 같은 뿌리).
    today = date.fromisoformat(today_kst())
    yt = sum(1 for i in items if i.raw.source_type.value == "youtube")
    rss = len(items) - yt
    breakdown = []
    if yt: breakdown.append(f"📹 {yt}")
    if rss: breakdown.append(f"📰 {rss}")
    breakdown_str = "  " + " · ".join(breakdown) if breakdown else ""
    return (
        f"📬 <b>DS Digest  {today.month}월 {today.day}일</b>\n"
        f"오늘의 큐레이션 {len(items)}건{breakdown_str}\n"
        # 자유 텍스트 지시 경로가 어디에도 안내돼 있지 않아 사실상 없는 기능이었다.
        # /keyword보다 표현력이 넓으므로 이쪽을 앞에 둔다.
        f"<i>💡 그냥 한국말로 말해주세요 — \"논문보다 실무 사례 위주로\", "
        f"\"쿠버네티스는 줄여줘\" 같은 지시가 다음 날 반영됩니다. /help</i>"
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
    if a.positioning:
        lines.append(f"📍 {html.escape(a.positioning)}")

    if a.key_points:
        lines += ["📌 <b>핵심 포인트</b>"]
        for kp in a.key_points:
            ts = f"<code>{kp.timestamp}</code> " if kp.timestamp else ""
            lines.append(f"  • {ts}{html.escape(kp.point)}")

    if a.production_ideas:
        lines += ["", "💡 <b>현업 적용</b>"]
        for idea in a.production_ideas:
            lines.append(f"  • {html.escape(idea)}")

    if a.tags:
        lines += ["", "  ".join(f"#{html.escape(t.replace(' ', '_'))}" for t in a.tags)]

    lines += ["", f'🔗 <a href="{raw.url}">원본 보기</a>']
    return "\n".join(lines)


_ALPHA = ["A", "B", "C", "D"]

# Telegram 인라인 키보드 상한(버튼 100개). 문항당 최대 4개이므로 25문항이 한계다.
# 실측은 하루 8~9문항이라 여유가 크지만, 상한을 넘기면 키보드째 거부당하므로
# 넘치는 문항은 버튼 없이 읽기 전용으로 남긴다(스포일러는 그대로 동작).
_MAX_QUIZ_BUTTONS = 100


def _format_quiz(items: list[DigestItem]) -> str | None:
    """퀴즈를 하나의 메시지로 묶기.
    정답은 <tg-spoiler>로 감싸서 탭하면 보이는 스포일러 형태로 표시.
    4096자 초과 시 None 반환(스킵).

    문항에 전역 번호(Q1, Q2...)를 매긴다. 응답 버튼이 메시지 하단에 한 줄씩
    붙는데, 번호가 없으면 어느 버튼이 어느 문항인지 알 수 없다.
    """
    lines = ["🧠 <b>오늘의 퀴즈</b>  <i>(아래 버튼으로 답을 고르고, 정답은 탭해서 확인)</i>", ""]

    number = 0
    for item in items:
        for q in item.analysis.quiz:
            number += 1
            lines.append(f"<b>Q{number}. {html.escape(q.question)}</b>")
            for i, opt in enumerate(q.options):
                lines.append(f"  {_ALPHA[i]}. {html.escape(opt)}")
            answer_text = f"✅ {_ALPHA[q.answer_index]}) {html.escape(q.options[q.answer_index])} — {html.escape(q.explanation)}"
            lines.append(f"  <tg-spoiler>{answer_text}</tg-spoiler>")
            lines.append("")

    if number == 0:
        return None
    text = "\n".join(lines)
    return text if len(text) <= _MAX_MSG_LEN else None


def _quiz_keyboard(items: list[DigestItem]) -> dict | None:
    """문항별 선지 버튼. 행 하나가 문항 하나이고, `_format_quiz`의 번호와 맞는다.

    콜백은 선택을 **기록만** 한다. 폴링이 하루 1회 배치라 answerCallbackQuery
    토스트가 제때 나갈 수 없기 때문이다(콜백이 이미 만료된다). 정답 공개는
    계속 스포일러가 맡는다. 그래도 스포일러를 열기 전에 고른 답이 잡히므로
    "맞았다/틀렸다" 자가신고보다 라벨이 정확하다.
    """
    rows: list[list[dict]] = []
    number = 0
    button_count = 0

    for item in items:
        target = item_id(item.raw.url)
        for question_index, q in enumerate(item.analysis.quiz):
            number += 1
            if button_count + len(q.options) > _MAX_QUIZ_BUTTONS:
                break
            rows.append([
                {
                    "text": f"Q{number}{_ALPHA[choice]}",
                    "callback_data": encode_quiz_callback(target, question_index, choice),
                }
                for choice in range(len(q.options))
            ])
            button_count += len(q.options)

    return {"inline_keyboard": rows} if rows else None


def _item_keyboard(item_url: str) -> dict:
    """👍/👎 인라인 키보드.

    callback_data에는 URL이 아니라 12자 item_id를 싣는다. Telegram의
    callback_data 한도는 **64바이트**인데, 실측 153건 중 39건(25.5%)의
    `like|{url}`이 이를 넘겼다. 한도를 넘으면 Telegram이 BUTTON_DATA_INVALID로
    응답하고 `_send_message`가 False를 반환하는데, 키보드는 아이템 메시지에
    붙어 있으므로 **그 아이템 메시지가 통째로 발송되지 않았다**. 즉 긴 URL을 가진
    아이템은 사용자에게 도달조차 못 했고, 당연히 피드백도 받을 수 없었다.

    id는 app.contract.item_id와 같은 값이라 공개 계약 JSON과 좌표계가 같다.
    수신측(deliverers/polling.py)이 정본을 통해 URL로 되돌린다.
    """
    token = item_id(item_url)
    return {
        "inline_keyboard": [[
            {"text": "👍", "callback_data": f"like|{token}"},
            {"text": "👎", "callback_data": f"dislike|{token}"},
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
            await _send_message(
                client, token, chat_id, quiz_text,
                reply_markup=_quiz_keyboard(items),
            )

    logger.info("telegram_digest_sent", items=len(items), chat_id=chat_id)
    return True
