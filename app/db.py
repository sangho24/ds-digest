"""
Supabase DB 연동 모듈
- seen_urls: 중복 발송 방지
- feedback: 👍/👎/키워드 이력 저장
- user_profile: 사용자 선호도 영속화

MVP(로컬 JSON)에서 Supabase로 전환된 구현입니다.
"""
import structlog
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from supabase import create_client, Client

from app.config import get_settings
from app.models import UserProfile, FeedbackPayload

logger = structlog.get_logger()


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_key)


# ──────────────────────────────────────────────
# seen_urls (중복 발송 방지)
# ──────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    """URL 정규화: 트레일링 슬래시 제거, 소문자 스킴+호스트."""
    url = url.strip()
    # 스킴 + 호스트 소문자화 (path는 대소문자 유지)
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if "/" in rest:
            host, path = rest.split("/", 1)
            url = f"{scheme.lower()}://{host.lower()}/{path}"
        else:
            url = f"{scheme.lower()}://{rest.lower()}"
    # 트레일링 슬래시 제거 (path가 있을 때만)
    if url.count("/") > 2:
        url = url.rstrip("/")
    return url


def fetch_seen_urls(urls: list[str]) -> set[str]:
    """
    주어진 URL 목록 중 이미 발송된 것을 한 번의 쿼리로 조회.
    Supabase 연결 실패 시 빈 set 반환(fail-open) — 중복보다 미발송이 더 나쁘다.
    DRY_RUN=true 시 항상 빈 set 반환.
    """
    from app.config import get_settings
    if get_settings().dry_run:
        return set()
    if not urls:
        return set()
    normalized = [_normalize_url(u) for u in urls]
    try:
        result = (
            get_supabase()
            .table("seen_urls")
            .select("url")
            .in_("url", normalized)
            .execute()
        )
        return {row["url"] for row in result.data}
    except Exception as e:
        logger.warning("seen_urls_bulk_fetch_failed", count=len(urls), error=str(e))
        return set()


def cleanup_seen_urls(days: int = 30) -> int:
    """
    days일보다 오래된 seen_urls 레코드 삭제.
    매일 파이프라인 시작 시 호출하여 DB 비대화 방지.
    반환: 삭제된 행 수 (실패 시 0)
    """
    from app.config import get_settings
    if get_settings().dry_run:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        result = (
            get_supabase()
            .table("seen_urls")
            .delete()
            .lt("seen_at", cutoff)
            .execute()
        )
        count = len(result.data) if result.data else 0
        if count:
            logger.info("seen_urls_cleanup", deleted=count, cutoff_days=days)
        return count
    except Exception as e:
        logger.warning("seen_urls_cleanup_failed", error=str(e))
        return 0


def is_seen(url: str) -> bool:
    """단일 URL 조회 — fetch_seen_urls 래퍼 (하위 호환)."""
    return _normalize_url(url) in fetch_seen_urls([url])


def mark_seen(url: str) -> None:
    """발송 완료된 URL을 seen_urls에 기록 (정규화 후 저장)."""
    url = _normalize_url(url)
    try:
        get_supabase().table("seen_urls").upsert(
            {"url": url, "seen_at": datetime.now(timezone.utc).isoformat()}
        ).execute()
    except Exception as e:
        logger.warning("mark_seen_failed", url=url[:80], error=str(e))


# ──────────────────────────────────────────────
# feedback
# ──────────────────────────────────────────────

def save_feedback_to_db(payload: FeedbackPayload) -> None:
    """피드백을 Supabase feedback 테이블에 저장."""
    try:
        get_supabase().table("feedback").insert({
            "user_id": payload.user_id,
            "item_url": payload.item_url,
            "action": payload.action,
            "keyword": payload.keyword,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning("save_feedback_failed", action=payload.action, error=str(e))


# ──────────────────────────────────────────────
# user_profile
# ──────────────────────────────────────────────

def load_profile_from_db(user_id: str = "default") -> UserProfile | None:
    """Supabase에서 사용자 프로필 로드. 없으면 None 반환."""
    try:
        result = (
            get_supabase()
            .table("user_profile")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        return UserProfile(
            user_id=row["user_id"],
            preferred_topics=row.get("preferred_topics") or [],
            liked_item_ids=row.get("liked_item_ids") or [],
            disliked_item_ids=row.get("disliked_item_ids") or [],
            keyword_requests=row.get("keyword_requests") or [],
        )
    except Exception as e:
        logger.warning("load_profile_failed", user_id=user_id, error=str(e))
        return None


def save_profile_to_db(profile: UserProfile) -> None:
    """사용자 프로필을 Supabase에 upsert."""
    try:
        get_supabase().table("user_profile").upsert({
            "user_id": profile.user_id,
            "preferred_topics": profile.preferred_topics,
            "liked_item_ids": profile.liked_item_ids,
            "disliked_item_ids": profile.disliked_item_ids,
            "keyword_requests": profile.keyword_requests,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning("save_profile_failed", user_id=profile.user_id, error=str(e))
