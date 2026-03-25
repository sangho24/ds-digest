"""
Supabase DB 연동 모듈
- seen_urls: 중복 발송 방지
- feedback: 👍/👎/키워드 이력 저장
- user_profile: 사용자 선호도 영속화

MVP(로컬 JSON)에서 Supabase로 전환된 구현입니다.
"""
import structlog
from datetime import datetime, timezone
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

def is_seen(url: str) -> bool:
    """
    해당 URL이 이미 발송된 적 있는지 확인.
    Supabase 연결 실패 시 False 반환(fail-open) — 가끔 중복 허용이 미발송보다 낫다.
    DRY_RUN=true 시 항상 False 반환 (모든 아이템 통과).
    """
    from app.config import get_settings
    if get_settings().dry_run:
        return False
    try:
        result = (
            get_supabase()
            .table("seen_urls")
            .select("url")
            .eq("url", url)
            .limit(1)
            .execute()
        )
        return len(result.data) > 0
    except Exception as e:
        logger.warning("seen_urls_check_failed", url=url[:80], error=str(e))
        return False


def mark_seen(url: str) -> None:
    """발송 완료된 URL을 seen_urls에 기록."""
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
