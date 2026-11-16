"""
Supabase DB 연동 모듈
- seen_urls: 중복 발송 방지
- feedback: 👍/👎/키워드 이력 저장
- user_profile: 사용자 선호도 영속화
- subscribers / sent_items / feedback_events: 지인 초대제 구독자 발송(service role 키)

MVP(로컬 JSON)에서 Supabase로 전환된 구현입니다.
"""
import structlog
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from collections.abc import Sequence
from typing import Any

from supabase import create_client, Client

from app.config import get_settings
from app.subscribers import scrub_error
from app.models import UserProfile, FeedbackPayload, DigestItem

logger = structlog.get_logger()


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_key)


@lru_cache(maxsize=1)
def get_supabase_service() -> Client:
    """구독자 테이블용 클라이언트.

    subscribers, feedback_events, sent_items 는 RLS 로 anon 접근이 막혀 있어 service
    role 키가 필요하다. SUPABASE_SERVICE_KEY 가 없으면 기존 SUPABASE_KEY 로 폴백한다
    (그 키가 anon 이면 조회는 오류 없이 0행으로 돌아와 구독자 발송이 조용히 꺼진다).
    """
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_key_effective)


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
        query = (
            get_supabase()
            .table("seen_urls")
            .select("url")
            .in_("url", normalized)
        )
        # SEEN_RECENT_DAYS > 0이면 최근 N일 안에 발송된 것만 중복으로 친다.
        # 행을 지우지 않고 조회 범위만 좁히므로 되돌릴 것이 없다.
        recent_days = get_settings().seen_recent_days
        if recent_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_days)).isoformat()
            query = query.gte("seen_at", cutoff)
            logger.info("seen_urls_window_narrowed", recent_days=recent_days)
        result = query.execute()
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


# ──────────────────────────────────────────────
# digest_records (구조화 다이제스트 정본의 best-effort 미러)
# 정본은 로컬 JSON(app/records.py)이며, 이 미러 실패는 무해하다.
# ──────────────────────────────────────────────

async def save_digest_records_to_db(digest_items: list[DigestItem], date_str: str) -> None:
    """구조화 다이제스트 레코드를 Supabase digest_records에 미러링한다(best-effort).

    - dry_run이면 즉시 skip(다른 함수들의 게이트 패턴과 동일).
    - Supabase 미설정/테이블 없음/네트워크 실패 등 모든 예외를 catch해
      logger.warning으로만 남기고 절대 raise하지 않는다 — 로컬 JSON이 정본이므로
      미러 실패는 파이프라인에 영향이 없어야 한다.
    """
    if get_settings().dry_run:
        return
    try:
        rows = []
        for item in digest_items:
            rows.append({
                "digest_date": date_str,
                "url": item.raw.url,
                "source_key": item.raw.source_key or None,
                "source_label": item.raw.source_label or None,
                "title": item.raw.title,
                "relevance_score": item.analysis.relevance_score,
                # domain은 최대 2개 리스트 — 대표값 1개만 컬럼에 남기고
                # 전체 분석은 analysis jsonb에 보존한다.
                "domain": (item.analysis.domain[0] if item.analysis.domain else None),
                "content_type": item.analysis.content_type,
                # jsonb 컬럼 — enum/중첩까지 JSON 안전하게 직렬화한다.
                "analysis": item.analysis.model_dump(mode="json"),
            })
        if not rows:
            return
        get_supabase().table("digest_records").insert(rows).execute()
        logger.info("digest_records_mirrored", count=len(rows), date=date_str)
    except Exception as e:
        logger.warning("save_digest_records_to_db_failed", date=date_str, error=str(e))


# ──────────────────────────────────────────────
# subscribers / sent_items / feedback_events (지인 초대제 구독자 발송)
# 스키마와 RLS 는 supabase/ 의 마이그레이션이 정본이다. 여기는 읽고 쓰기만 한다.
# 관례는 위와 같다: 예외를 올리지 않고 경고 로그만 남긴다. 다만 "0행"과 "조회 실패"를
# 호출부가 구분해야 하는 조회는 실패 시 None 을 돌려준다.
# 이메일 주소와 token 은 로그에 남기지 않는다(구독자 id 앞 8자만). 오류 문구는
# scrub_error 를 거친다(주소가 섞여 있으면 문구를 통째로 버린다).
# ──────────────────────────────────────────────

# PostgREST 기본 max-rows. 한 번에 이보다 많이 받을 수 없으므로 최신순으로 이만큼만 읽는다.
_MAX_ROWS = 1000


def fetch_subscribers(active_only: bool = False) -> list[dict[str, Any]] | None:
    """구독자 행 목록. 조회 실패는 None(0명과 구분된다)."""
    try:
        query = get_supabase_service().table("subscribers").select("*")
        if active_only:
            query = query.eq("status", "active")
        result = query.order("created_at").execute()
        return list(result.data or [])
    except Exception as e:
        logger.warning("subscribers_fetch_failed", error=scrub_error(e))
        return None


def insert_subscriber(row: dict[str, Any]) -> dict[str, Any] | None:
    """구독자 한 명을 넣고 저장된 행을 돌려준다. 실패는 None."""
    try:
        result = get_supabase_service().table("subscribers").insert(row).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        # PostgREST 오류 문구에 입력값(이메일)이 섞여 나올 수 있어 종류만 남긴다.
        logger.warning("subscriber_insert_failed", error=type(e).__name__)
        return None


def update_subscriber(subscriber_id: str, fields: dict[str, Any]) -> bool:
    """구독자 한 명의 컬럼을 고친다. 성공 여부를 돌려준다."""
    try:
        get_supabase_service().table("subscribers").update(fields).eq(
            "id", subscriber_id
        ).execute()
        return True
    except Exception as e:
        logger.warning(
            "subscriber_update_failed", subscriber=subscriber_id[:8], error=scrub_error(e)
        )
        return False


def fetch_sent_item_urls(subscriber_id: str, urls: list[str]) -> set[str] | None:
    """urls 중 이 구독자가 이미 받은 것. 조회 실패는 None.

    전체 이력이 아니라 오늘 후보에 든 url 만 묻는다. 이력은 구독자마다 계속 늘지만
    후보는 하루 수십 건이라 행 수 상한(_MAX_ROWS)에 걸리지 않는다.
    """
    if not urls:
        return set()
    try:
        result = (
            get_supabase_service()
            .table("sent_items")
            .select("item_url")
            .eq("subscriber_id", subscriber_id)
            .in_("item_url", urls)
            .execute()
        )
        return {row["item_url"] for row in result.data or []}
    except Exception as e:
        logger.warning(
            "sent_items_fetch_failed", subscriber=subscriber_id[:8], error=scrub_error(e)
        )
        return None


def has_sent_on_date(subscriber_id: str, digest_date: str) -> bool | None:
    """이 구독자에게 그 날짜 호가 이미 나갔는가. 조회 실패는 None.

    같은 날 재실행(워크플로 재시도, 수동 재발송)이 두 번째 메일을 만들지 않게 하는
    확인이다. sent_items 는 이미 받은 url 을 다음 선정에서 빼므로, 이 확인이 없으면
    재실행은 같은 메일이 아니라 "남은 후보로 만든 다른 메일"을 한 통 더 보낸다.
    """
    try:
        result = (
            get_supabase_service()
            .table("sent_items")
            .select("item_url")
            .eq("subscriber_id", subscriber_id)
            .eq("digest_date", digest_date)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as e:
        logger.warning(
            "sent_items_date_check_failed", subscriber=subscriber_id[:8], error=scrub_error(e)
        )
        return None


def record_sent_items(
    subscriber_id: str,
    urls: list[str],
    digest_date: str,
    arms: Sequence[str | None] | None = None,
) -> bool:
    """발송에 성공한 아이템을 sent_items 에 남긴다. pk(subscriber_id, item_url) upsert.

    urls 는 지면 순서다. position 은 그 순서(1부터)이고, arms 는 urls 와 같은 길이의
    비교군('personal' | 'baseline' | 'shared' | None)이다. arms 를 주지 않으면 arm 컬럼은
    건드리지 않는다.

    arm, position 컬럼은 나중에 추가된 것이라 원격 테이블에 아직 없을 수 있다. 그 경우
    upsert 가 통째로 실패하므로, 두 컬럼을 뺀 행으로 한 번 더 시도한다. 발송 기록이
    없으면 내일 같은 아이템이 다시 가므로 측정용 컬럼 때문에 기록을 잃으면 안 된다.
    """
    if not urls:
        return True
    base_rows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, url in enumerate(urls):
        if url in seen:
            continue
        seen.add(url)
        base = {"subscriber_id": subscriber_id, "item_url": url, "digest_date": digest_date}
        base_rows.append(base)
        extra: dict[str, Any] = {"position": index + 1}
        if arms is not None:
            extra["arm"] = arms[index] if index < len(arms) else None
        rows.append({**base, **extra})

    def _upsert(payload: list[dict[str, Any]]) -> None:
        get_supabase_service().table("sent_items").upsert(
            payload, on_conflict="subscriber_id,item_url"
        ).execute()

    try:
        _upsert(rows)
        return True
    except Exception as e:
        # 가장 흔한 원인은 마이그레이션 적용 전(컬럼 없음)이다. 원인을 가리지 않고 한 번만
        # 기존 모양으로 다시 시도한다. 네트워크 오류였다면 아래도 실패해 False 로 끝난다.
        logger.warning(
            "sent_items_arm_columns_rejected",
            subscriber=subscriber_id[:8],
            count=len(rows),
            error=scrub_error(e),
            msg="arm, position 없이 다시 기록한다. supabase 마이그레이션 적용 여부를 확인하라",
        )
    try:
        _upsert(base_rows)
        return True
    except Exception as e:
        logger.warning(
            "sent_items_record_failed",
            subscriber=subscriber_id[:8],
            count=len(base_rows),
            error=scrub_error(e),
        )
        return False


def fetch_feedback_events(subscriber_ids: list[str]) -> list[dict[str, Any]] | None:
    """구독자들의 want/skip 표시를 오래된 것부터 돌려준다. 조회 실패는 None.

    최신 _MAX_ROWS 건만 읽는다. 같은 (subscriber_id, item_url) 은 created_at 이 늦은
    쪽이 이기므로, 호출부는 이 순서대로 덮어쓰면 된다.
    """
    if not subscriber_ids:
        return []
    try:
        result = (
            get_supabase_service()
            .table("feedback_events")
            .select("subscriber_id,item_url,action,digest_date,created_at")
            .in_("subscriber_id", subscriber_ids)
            .order("created_at", desc=True)
            .limit(_MAX_ROWS)
            .execute()
        )
        return list(reversed(result.data or []))
    except Exception as e:
        logger.warning("feedback_events_fetch_failed", error=scrub_error(e))
        return None


def delete_subscriber(subscriber_id: str) -> bool:
    """구독자 한 명을 지운다. sent_items, feedback_events, click_events 는 외래키
    on delete cascade 로 함께 지워진다(supabase/ 의 마이그레이션이 정본).

    지워진 행이 돌아오지 않으면(대상 없음, 권한 없음) False 다.
    """
    try:
        result = (
            get_supabase_service().table("subscribers").delete().eq("id", subscriber_id).execute()
        )
        return bool(result.data)
    except Exception as e:
        # 오류 문구에 행 내용(이메일)이 섞여 나올 수 있어 종류만 남긴다.
        logger.warning(
            "subscriber_delete_failed", subscriber=subscriber_id[:8], error=type(e).__name__
        )
        return False


# ──────────────────────────────────────────────
# 개인화 효과 보고서(evals/effect.py)용 전체 조회
# 발송·선정 경로는 쓰지 않는다. 행 수 상한(_MAX_ROWS)을 넘어도 전부 읽도록 쪽 단위로 읽는다.
# ──────────────────────────────────────────────

# 무한 루프 방지용 쪽 수 상한(= 최대 20만 행). 지인 10명 규모에서는 닿지 않는다.
_MAX_PAGES = 200


def _fetch_all_rows(
    table: str,
    columns: str,
    order_by: Sequence[str],
    since_column: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """테이블 전체를 쪽 단위로 읽는다. 실패는 예외로 올린다(호출부가 None 으로 바꾼다)."""
    rows: list[dict[str, Any]] = []
    for page in range(_MAX_PAGES):
        query = get_supabase_service().table(table).select(columns)
        if since and since_column:
            query = query.gte(since_column, since)
        # 쪽 경계가 흔들리지 않게 유일한 순서를 준다.
        for column in order_by:
            query = query.order(column)
        start = page * _MAX_ROWS
        batch = list(query.range(start, start + _MAX_ROWS - 1).execute().data or [])
        rows.extend(batch)
        if len(batch) < _MAX_ROWS:
            break
    return rows


def fetch_sent_items_for_report(since: str | None = None) -> list[dict[str, Any]] | None:
    """sent_items 전체(since 가 있으면 digest_date 가 그 날짜 이후). 조회 실패는 None.

    arm, position 컬럼이 아직 없는 테이블이면 두 컬럼 없이 다시 읽는다(전부 미배정으로 보인다).
    """
    order = ("subscriber_id", "item_url")
    try:
        return _fetch_all_rows(
            "sent_items", "subscriber_id,item_url,digest_date,arm,position", order,
            "digest_date", since,
        )
    except Exception as e:
        logger.warning("sent_items_report_fetch_retry", error=scrub_error(e))
    try:
        return _fetch_all_rows(
            "sent_items", "subscriber_id,item_url,digest_date", order, "digest_date", since
        )
    except Exception as e:
        logger.warning("sent_items_report_fetch_failed", error=scrub_error(e))
        return None


def fetch_feedback_events_for_report() -> list[dict[str, Any]] | None:
    """feedback_events 전체(오래된 것부터). 조회 실패는 None.

    기간으로 자르지 않는다. 관심도 모델의 활성 여부는 전체 표시 수로 정해지기 때문이다.
    """
    try:
        return _fetch_all_rows(
            "feedback_events", "subscriber_id,item_url,action,digest_date,created_at", ("id",)
        )
    except Exception as e:
        logger.warning("feedback_events_report_fetch_failed", error=scrub_error(e))
        return None


def fetch_click_events_for_report() -> list[dict[str, Any]] | None:
    """click_events 전체(오래된 것부터). 테이블이 없거나 조회에 실패하면 None."""
    try:
        return _fetch_all_rows(
            "click_events", "subscriber_id,item_url,digest_date,created_at", ("id",)
        )
    except Exception as e:
        logger.warning("click_events_report_fetch_failed", error=scrub_error(e))
        return None
