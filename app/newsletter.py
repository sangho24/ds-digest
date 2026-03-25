"""
뉴스레터 렌더링 + 발송
Jinja2로 HTML 이메일 생성 → Resend로 발송
"""
import resend
import structlog
from datetime import date
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from app.config import get_settings
from app.models import DigestItem

logger = structlog.get_logger()

TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_digest_email(items: list[DigestItem], date_str: str | None = None) -> str:
    """다이제스트 아이템들을 HTML 뉴스레터로 렌더링"""
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("digest.html")

    return template.render(
        items=items,
        date_str=date_str or date.today().strftime("%Y년 %m월 %d일"),
        feedback_base_url=_get_feedback_url(),
    )


def _get_feedback_url() -> str:
    """피드백 엔드포인트 URL — config의 BASE_URL에서 가져옴."""
    return f"{get_settings().base_url}/api/feedback"


async def send_digest(items: list[DigestItem]) -> bool:
    """렌더링된 뉴스레터를 이메일로 발송"""
    settings = get_settings()

    resend.api_key = settings.resend_api_key
    html = render_digest_email(items)
    today = date.today().strftime("%m/%d")
    item_count = len(items)

    if settings.dry_run:
        logger.info("dry_run_skip_email", to=settings.email_to, items=item_count)
        return True

    if not settings.resend_api_key:
        logger.warning("resend_api_key_missing", msg="이메일 발송 스킵")
        return False

    # EMAIL_TO는 쉼표 구분 복수 수신자 허용
    recipients = [addr.strip() for addr in settings.email_to.split(",") if addr.strip()]

    try:
        resend.Emails.send({
            "from": settings.email_from,
            "to": recipients,
            "subject": f"[DS Digest {today}] 오늘의 큐레이션 {item_count}건",
            "html": html,
        })
        logger.info("email_sent", to=recipients, items=item_count)
        return True

    except Exception as e:
        logger.error("email_send_failed", error=str(e))
        return False
