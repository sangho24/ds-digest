"""
뉴스레터 렌더링 + 발송
Jinja2로 HTML 이메일 생성 → app/mailer.py(Resend 또는 SMTP)로 발송
"""
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import structlog
from datetime import date
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from app import mailer
from app.config import get_settings
from app.contract import ARCHIVE_BASE, today_kst
from app.models import DigestItem
from app.subscribers import scrub_error

logger = structlog.get_logger()

TEMPLATE_DIR = Path(__file__).parent / "templates"

# 구독자 메일 안의 링크가 여는 페이지(docs/feedback.html, GitHub Pages).
# 그 페이지가 feedback-config.json 의 endpoint 로 POST 한다.
FEEDBACK_PAGE = f"{ARCHIVE_BASE}/feedback.html"
# 구독자 메일의 원문 링크가 거치는 페이지(docs/go.html). 클릭을 기록하고 원문으로 보낸다.
CLICK_PAGE = f"{ARCHIVE_BASE}/go.html"
# 원클릭 수신거부(List-Unsubscribe-Post)가 직접 POST 하는 Edge Function 경로.
FEEDBACK_FUNCTION_PATH = "/functions/v1/digest-feedback"


@dataclass(frozen=True)
class RecipientLinks:
    """수신자 한 명의 링크를 만드는 데 필요한 값. 넘기면 메일에 피드백·수신거부 링크가 붙는다."""

    token: str
    date_iso: str


def feedback_link(
    token: str, action: str, item_url: str | None = None, date_iso: str | None = None
) -> str:
    """피드백 페이지 주소. action 은 want | skip | unsubscribe. u, d 는 있을 때만 붙는다."""
    link = f"{FEEDBACK_PAGE}?t={quote(token, safe='')}&a={action}"
    if item_url:
        link += f"&u={quote(item_url, safe='')}"
    if date_iso:
        link += f"&d={date_iso}"
    return link


def click_link(token: str, item_url: str, date_iso: str | None = None) -> str:
    """원문 이동 페이지 주소. go.html 이 클릭을 기록한 뒤 u 의 원문으로 보낸다."""
    link = f"{CLICK_PAGE}?t={quote(token, safe='')}&u={quote(item_url, safe='')}"
    if date_iso:
        link += f"&d={date_iso}"
    return link


def feedback_endpoint() -> str | None:
    """Edge Function 주소. SUPABASE_URL 이 없으면 None."""
    base = get_settings().supabase_url.strip().rstrip("/")
    return f"{base}{FEEDBACK_FUNCTION_PATH}" if base else None


def unsubscribe_headers(token: str) -> dict[str, str]:
    """원클릭 수신거부 헤더(RFC 8058). 주소는 https 하나만 넣는다(mailto 없음).

    Gmail·Yahoo 는 List-Unsubscribe 와 List-Unsubscribe-Post 가 함께 있어야 원클릭으로
    취급하고, 그 주소로 본문 `List-Unsubscribe=One-Click` 을 POST 한다. GitHub Pages 는
    POST 를 받지 못하므로 페이지가 아니라 Edge Function 을 직접 가리킨다.
    """
    endpoint = feedback_endpoint()
    if not endpoint or not endpoint.startswith("https://"):
        return {}
    return {
        "List-Unsubscribe": f"<{endpoint}?t={quote(token, safe='')}&a=unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def _template_env() -> Environment:
    """웹·이메일 렌더가 공유하는 Jinja 환경. 두 템플릿에 같은 필터·이스케이프
    규칙이 걸리도록 한 곳에서 만든다(현재는 필터 없음, autoescape 없음)."""
    return Environment(loader=FileSystemLoader(TEMPLATE_DIR))


def kst_date_label(iso_date: str) -> str:
    """"YYYY-MM-DD" 를 머리말 표기("2026년 09월 04일")로 바꾼다."""
    return date.fromisoformat(iso_date).strftime("%Y년 %m월 %d일")


def _default_date_str(date_str: str | None) -> str:
    """머리말 날짜. 넘기지 않으면 **KST 기준 오늘**이다.

    `date.today()` 를 쓰면 안 된다. cron 이 KST 04:37 로 옮겨져 실제 실행이
    UTC 전날 21~22시라, UTC 러너에서는 머리말·subject 가 하루 전 날짜로 나가고
    아카이브 링크(today_kst 기반)만 당일이 된다(검증에서 재현됨, 2026-09-04).
    """
    return date_str or kst_date_label(today_kst())


def email_subject(item_count: int, iso_date: str | None = None) -> str:
    """메일 제목. 날짜는 머리말과 같은 KST 기준으로 맞춘다."""
    d = date.fromisoformat(iso_date or today_kst())
    return f"[DS Digest {d.strftime('%m/%d')}] 오늘의 큐레이션 {item_count}건"


def render_digest_web(items: list[DigestItem], date_str: str | None = None) -> str:
    """다이제스트를 웹(GitHub Pages 아카이브 docs/)용 HTML 로 렌더링한다.

    CSS 변수·flex·<details>·웹폰트를 쓰는 digest.html 을 그대로 쓴다.
    브라우저에서만 열리는 지면이라 제약이 없다.
    """
    template = _template_env().get_template("digest.html")
    return template.render(
        items=items,
        date_str=_default_date_str(date_str),
        feedback_base_url=_get_feedback_url(),
    )


def render_digest_email(
    items: list[DigestItem],
    date_str: str | None = None,
    archive_url: str | None = None,
    recipient: RecipientLinks | None = None,
) -> str:
    """다이제스트를 이메일 발송용 HTML 로 렌더링한다.

    웹 템플릿과 분리한 이유: 메일 클라이언트(Gmail·네이버·Outlook·iOS Mail)는
    CSS 변수를 버리고 <style> 을 무시하거나 일부만 적용하며 flex/gap/<details> 를
    지원하지 않고 외부 폰트를 차단해서, 웹용 digest.html 을 그대로 보내면 글자
    배열과 디자인이 전부 깨진다(2026-09-04 네이버 메일 실측). 그래서 이메일은
    테이블 레이아웃과 인라인 스타일만 쓰는 digest_email.html 로 따로 렌더하고,
    웹 아카이브는 제약 없는 digest.html 을 계속 쓴다.

    archive_url 을 넘기지 않으면 오늘(KST) 호의 GitHub Pages 주소를 쓴다.
    daily_digest.py 가 docs/{today_kst}.html 로 저장하는 경로와 같은 규칙이다.

    recipient 를 넘기면 아이템마다 "읽고 싶다 / 아니다" 링크와 하단 수신거부 링크가
    붙는다(구독자 발송). 넘기지 않으면 링크 없는 기존 모습 그대로다.

    recipient 가 있고 설정 TRACK_CLICKS 가 켜져 있으면 아이템 제목의 원문 링크도
    클릭 기록 페이지(go.html) 주소로 바뀐다. recipient 가 없는 렌더(EMAIL_TO 기존 경로)는
    늘 원문 주소 그대로다.
    """
    feedback_links: dict[str, dict[str, str]] | None = None
    unsubscribe_url: str | None = None
    item_links: dict[str, str] | None = None
    if recipient is not None:
        if get_settings().track_clicks:
            item_links = {
                item.raw.url: click_link(recipient.token, item.raw.url, recipient.date_iso)
                for item in items
            }
        feedback_links = {
            item.raw.url: {
                action: feedback_link(recipient.token, action, item.raw.url, recipient.date_iso)
                for action in ("want", "skip")
            }
            for item in items
        }
        unsubscribe_url = feedback_link(recipient.token, "unsubscribe")
    template = _template_env().get_template("digest_email.html")
    html = template.render(
        items=items,
        date_str=_default_date_str(date_str),
        archive_url=archive_url or f"{ARCHIVE_BASE}/{today_kst()}.html",
        archive_index_url=f"{ARCHIVE_BASE}/",
        feedback_links=feedback_links,
        unsubscribe_url=unsubscribe_url,
        item_links=item_links,
    )
    return _compact_email_html(html)


def _compact_email_html(html: str) -> str:
    """줄 앞 들여쓰기와 빈 줄을 걷어낸다.

    Gmail 은 본문이 약 102KB 를 넘으면 뒷부분을 잘라 "전체 메시지 보기" 로 숨긴다.
    테이블 중첩 템플릿은 들여쓰기만 10KB 가 넘게 나오므로(실측 5건 96.7KB 중
    12KB) 발송 전에 걷어낸다. HTML 은 줄 앞 공백을 의미 있게 보지 않으니
    (<pre> 를 쓰지 않는다) 표시는 바뀌지 않는다.
    """
    html = re.sub(r"\n[ \t]+", "\n", html)
    return re.sub(r"\n{2,}", "\n", html)


def _get_feedback_url() -> str:
    """피드백 엔드포인트 URL — config의 BASE_URL에서 가져옴."""
    return f"{get_settings().base_url}/api/feedback"


async def send_digest(
    items: list[DigestItem],
    date_iso: str | None = None,
    subject_prefix: str = "",
    owners: Mapping[str, str] | None = None,
    owner_delivered: set[str] | None = None,
    owner_items: Mapping[str, list[DigestItem]] | None = None,
) -> bool:
    """렌더링된 뉴스레터를 이메일로 발송한다.

    date_iso 를 넘기지 않으면 KST 오늘이다. 넘기면("YYYY-MM-DD") 그 날짜로
    머리말·subject·아카이브 링크를 만든다(저장된 레코드 재발송용, resend_email.py).
    subject_prefix 는 subject 맨 앞에 그대로 붙는다(예: "[재발송] ").

    수신자마다 따로 한 통씩 보낸다. 한 통에 전원을 담으면 To 헤더로 서로의
    주소가 노출되고, 한 주소가 거절되면(Resend 무료 플랜의 onboarding@resend.dev
    발신은 가입자 본인 주소만 허용) 그 통 전체가 실패해 본인도 못 받는다.

    반환값은 기존 계약 그대로 bool 이다: 한 명 이상 성공하면 True, 전원 실패면
    False. 호출부(daily_digest.py)는 이 값으로 채널 성공 여부와 _mark_sent 를
    판단하므로, 부분 실패는 별도 경고 로그(email_partial_failure)로만 남긴다.

    owners 는 {소문자 이메일: token} 이다. EMAIL_TO 주소가 is_owner 구독자와 같으면
    그 한 통만 피드백·수신거부 링크가 든 구독자용 메일로 바꿔 보낸다. 같은 사람이
    기존 메일과 구독자 메일을 두 통 받지 않게 하려는 것이고, 따로 빼서 나중에 보내지
    않는 이유는 이 함수의 성공 여부(= 채널 성공, _mark_sent)가 실제 발송을 그대로
    반영해야 하기 때문이다. 그렇게 보낸 주소는 owner_delivered 에 담아 구독자 단계가
    다시 보내지 않게 한다. owners 가 비어 있으면 동작은 예전과 완전히 같다.

    owner_items 는 {소문자 이메일: 그 주소의 메일 본문에만 쓸 목록} 이다. is_owner 구독자의
    style_note 로 다듬은 요약(app/restyle.py)을 그 한 통에만 쓰려는 것이고, owners 에 있는
    주소에만 적용된다. 아이템 구성(url, 순서)은 items 와 같아야 한다. 운영자의 글쓰기
    지시(personal_note)로 다듬은 목록은 EMAIL_TO 전원에게 가므로 items 로 넘긴다.
    """
    settings = get_settings()

    # 머리말·제목·아카이브 링크가 같은 날짜를 가리키도록 KST 날짜를 한 번만 잡는다.
    today_iso = date_iso or today_kst()
    date_label = kst_date_label(today_iso)
    archive_url = f"{ARCHIVE_BASE}/{today_iso}.html"
    html = render_digest_email(items, date_str=date_label, archive_url=archive_url)
    item_count = len(items)

    if settings.dry_run:
        logger.info("dry_run_skip_email", to=settings.email_to, items=item_count)
        return True

    # resend 는 API 키, smtp 는 호스트가 없으면 보낼 수 없다.
    problem = mailer.configuration_problem(settings)
    if problem is not None:
        logger.warning(problem, msg="이메일 발송 스킵")
        return False

    # EMAIL_TO는 쉼표 구분 복수 수신자 허용
    recipients = [addr.strip() for addr in settings.email_to.split(",") if addr.strip()]
    if not recipients:
        logger.warning("email_recipients_empty", msg="EMAIL_TO 가 비어 이메일 발송 스킵")
        return False

    subject = subject_prefix + email_subject(item_count, today_iso)

    # Resend 한도는 초당 10건이라 수십 명 이하 직렬 발송에는 지연이 필요 없다.
    failed: list[str] = []
    for addr in recipients:
        body, headers = html, None
        owner_token = (owners or {}).get(addr.lower())
        # is_owner 구독자의 주소는 구독자 정보다. 공개 로그에 남기지 않는다(주소 대신 표식).
        # 그 밖의 EMAIL_TO 수신자는 기존 운영자 경로 그대로다.
        is_subscriber = owner_token is not None
        shown = "(구독자)" if is_subscriber else addr
        if owner_token:
            try:
                body = render_digest_email(
                    (owner_items or {}).get(addr.lower(), items),
                    date_str=date_label,
                    archive_url=archive_url,
                    recipient=RecipientLinks(token=owner_token, date_iso=today_iso),
                )
                headers = unsubscribe_headers(owner_token)
            except Exception as e:
                # 링크 붙이기에 실패해도 운영자 메일은 기존 모습으로 나가야 한다.
                logger.warning("owner_email_links_failed", error=scrub_error(e, addr))
                body, headers, owner_token = html, None, None
        result = mailer.send(addr, subject, body, headers)
        if result.ok:
            logger.info("email_sent", to=shown, items=item_count)
            if owner_token and owner_delivered is not None:
                owner_delivered.add(addr.lower())
        else:
            # 한 명의 실패가 다음 수신자 발송을 막지 않는다.
            failed.append(shown)
            logger.error(
                "email_send_failed",
                to=shown,
                error=scrub_error(result.error, addr) if is_subscriber else result.error,
            )

    sent_count = len(recipients) - len(failed)
    if failed and sent_count:
        logger.warning(
            "email_partial_failure",
            failed=failed,
            sent=f"{sent_count}/{len(recipients)}",
        )
    return sent_count > 0
