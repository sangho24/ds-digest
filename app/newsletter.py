"""
뉴스레터 렌더링 + 발송
Jinja2로 HTML 이메일 생성 → Resend로 발송
"""
import re

import resend
import structlog
from datetime import date
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from app.config import get_settings
from app.contract import ARCHIVE_BASE, today_kst
from app.models import DigestItem

logger = structlog.get_logger()

TEMPLATE_DIR = Path(__file__).parent / "templates"


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
) -> str:
    """다이제스트를 이메일(Resend 발송)용 HTML 로 렌더링한다.

    웹 템플릿과 분리한 이유: 메일 클라이언트(Gmail·네이버·Outlook·iOS Mail)는
    CSS 변수를 버리고 <style> 을 무시하거나 일부만 적용하며 flex/gap/<details> 를
    지원하지 않고 외부 폰트를 차단해서, 웹용 digest.html 을 그대로 보내면 글자
    배열과 디자인이 전부 깨진다(2026-09-04 네이버 메일 실측). 그래서 이메일은
    테이블 레이아웃과 인라인 스타일만 쓰는 digest_email.html 로 따로 렌더하고,
    웹 아카이브는 제약 없는 digest.html 을 계속 쓴다.

    archive_url 을 넘기지 않으면 오늘(KST) 호의 GitHub Pages 주소를 쓴다.
    daily_digest.py 가 docs/{today_kst}.html 로 저장하는 경로와 같은 규칙이다.
    """
    template = _template_env().get_template("digest_email.html")
    html = template.render(
        items=items,
        date_str=_default_date_str(date_str),
        archive_url=archive_url or f"{ARCHIVE_BASE}/{today_kst()}.html",
        archive_index_url=f"{ARCHIVE_BASE}/",
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


async def send_digest(items: list[DigestItem]) -> bool:
    """렌더링된 뉴스레터를 이메일로 발송한다.

    수신자마다 따로 한 통씩 보낸다. 한 통에 전원을 담으면 To 헤더로 서로의
    주소가 노출되고, 한 주소가 거절되면(Resend 무료 플랜의 onboarding@resend.dev
    발신은 가입자 본인 주소만 허용) 그 통 전체가 실패해 본인도 못 받는다.

    반환값은 기존 계약 그대로 bool 이다: 한 명 이상 성공하면 True, 전원 실패면
    False. 호출부(daily_digest.py)는 이 값으로 채널 성공 여부와 _mark_sent 를
    판단하므로, 부분 실패는 별도 경고 로그(email_partial_failure)로만 남긴다.
    """
    settings = get_settings()

    resend.api_key = settings.resend_api_key
    # 머리말·제목·아카이브 링크가 같은 날짜를 가리키도록 KST 날짜를 한 번만 잡는다.
    today_iso = today_kst()
    html = render_digest_email(
        items,
        date_str=kst_date_label(today_iso),
        archive_url=f"{ARCHIVE_BASE}/{today_iso}.html",
    )
    item_count = len(items)

    if settings.dry_run:
        logger.info("dry_run_skip_email", to=settings.email_to, items=item_count)
        return True

    if not settings.resend_api_key:
        logger.warning("resend_api_key_missing", msg="이메일 발송 스킵")
        return False

    # EMAIL_TO는 쉼표 구분 복수 수신자 허용
    recipients = [addr.strip() for addr in settings.email_to.split(",") if addr.strip()]
    if not recipients:
        logger.warning("email_recipients_empty", msg="EMAIL_TO 가 비어 이메일 발송 스킵")
        return False

    subject = email_subject(item_count, today_iso)

    # Resend 한도는 초당 10건이라 수십 명 이하 직렬 발송에는 지연이 필요 없다.
    failed: list[str] = []
    for addr in recipients:
        try:
            resend.Emails.send({
                "from": settings.email_from,
                "to": [addr],
                "subject": subject,
                "html": html,
            })
            logger.info("email_sent", to=addr, items=item_count)
        except Exception as e:
            # 한 명의 실패가 다음 수신자 발송을 막지 않는다.
            failed.append(addr)
            logger.error("email_send_failed", to=addr, error=str(e)[:200])

    sent_count = len(recipients) - len(failed)
    if failed and sent_count:
        logger.warning(
            "email_partial_failure",
            failed=failed,
            sent=f"{sent_count}/{len(recipients)}",
        )
    return sent_count > 0
