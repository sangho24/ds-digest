"""
피드백 처리 모듈
사용자 피드백을 받아 프로필을 업데이트한다.

Supabase 연결 가능 시: DB에 저장 + 로드
Supabase 미설정 / 연결 실패 시: 로컬 JSON 파일로 폴백
"""
import json
import structlog
from pathlib import Path
from datetime import datetime

from app.models import FeedbackPayload, UserProfile

logger = structlog.get_logger()

PROFILE_PATH = Path(__file__).parent.parent / "data" / "profile.json"


# ──────────────────────────────────────────────
# Profile 로드/저장 (Supabase 우선, 로컬 JSON 폴백)
# ──────────────────────────────────────────────

def load_profile(user_id: str = "default") -> UserProfile:
    """
    사용자 프로필 로드.
    Supabase 설정이 있으면 DB에서, 없거나 실패하면 로컬 JSON에서.
    """
    from app.config import get_settings
    settings = get_settings()

    if settings.supabase_url and settings.supabase_key:
        from app.db import load_profile_from_db
        profile = load_profile_from_db(user_id)
        if profile is not None:
            logger.info("profile_loaded_from_supabase", user_id=user_id)
            return profile
        # DB에 프로필 없으면 기본값 생성 후 저장
        profile = UserProfile(user_id=user_id)
        save_profile(profile)
        return profile

    # 로컬 JSON 폴백
    return _load_local(user_id)


def save_profile(profile: UserProfile) -> None:
    """
    사용자 프로필 저장.
    Supabase 설정이 있으면 DB에, 항상 로컬 JSON에도 저장(백업 겸).
    """
    from app.config import get_settings
    settings = get_settings()

    if settings.supabase_url and settings.supabase_key:
        from app.db import save_profile_to_db
        save_profile_to_db(profile)
        logger.info("profile_saved_to_supabase", user_id=profile.user_id)

    _save_local(profile)


# ──────────────────────────────────────────────
# 피드백 처리
# ──────────────────────────────────────────────

def process_feedback(payload: FeedbackPayload) -> UserProfile:
    """피드백을 처리하여 프로필 업데이트 + DB 저장."""
    # Supabase에 피드백 이력 저장
    from app.config import get_settings
    settings = get_settings()
    if settings.supabase_url and settings.supabase_key:
        from app.db import save_feedback_to_db
        save_feedback_to_db(payload)

    profile = load_profile(payload.user_id)

    if payload.action == "like":
        if payload.item_url not in profile.liked_item_ids:
            profile.liked_item_ids.append(payload.item_url)
            logger.info("feedback_like", url=payload.item_url)

    elif payload.action == "dislike":
        if payload.item_url not in profile.disliked_item_ids:
            profile.disliked_item_ids.append(payload.item_url)
            logger.info("feedback_dislike", url=payload.item_url)

    elif payload.action == "keyword_request" and payload.keyword:
        keyword = payload.keyword.strip().lower()
        if keyword and keyword not in profile.keyword_requests:
            profile.keyword_requests.append(keyword)
            logger.info("feedback_keyword", keyword=keyword)

    profile.updated_at = datetime.now()
    save_profile(profile)
    return profile


# ──────────────────────────────────────────────
# 로컬 JSON 폴백 (Supabase 미설정 환경 / 백업용)
# ──────────────────────────────────────────────

def _load_local(user_id: str) -> UserProfile:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PROFILE_PATH.exists():
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return UserProfile(**data)
    return UserProfile(user_id=user_id)


def _save_local(profile: UserProfile) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    logger.debug("profile_saved_locally", path=str(PROFILE_PATH))
