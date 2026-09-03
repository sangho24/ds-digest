"""릴리스 추적 수집기 - HF Hub API 와 공식 블로그 피드.

판정 로직은 `app.releases` 에 있다. 여기는 네트워크만 담당하고 실패는 조직
단위로 흡수한다. 한 조직의 API 가 죽었다고 나머지 열다섯을 놓치면 안 된다.

로컬 망에서는 huggingface.co 가 SNI 차단이라 이 모듈은 로컬에서 실행되지
않는다(2026-09-03 실측). GitHub Actions 러너에서 돈다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import structlog

from app.releases import BlogObservation, HFObservation, Org, Watchlist, observation_from_hf

logger = structlog.get_logger()

HF_API = "https://huggingface.co/api/models"
# 전이 판정에 필요한 필드. 기본 응답에는 cardData·siblings·gated 가 없다.
HF_EXPAND = ("cardData", "createdAt", "lastModified", "tags", "gated", "private", "siblings")
UA = "ds-digest-release-watch/1.0 (+https://github.com/sangho24/ds-digest)"


async def _hf_list(client: httpx.AsyncClient, hf_org: str, sort: str, limit: int) -> list[dict]:
    params = [("author", hf_org), ("sort", sort), ("direction", "-1"), ("limit", str(limit))]
    params += [("expand[]", f) for f in HF_EXPAND]
    resp = await client.get(HF_API, params=params)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"예상 밖 응답 형식: {type(data).__name__}")
    return data


async def fetch_hf_org(
    client: httpx.AsyncClient, org: Org, limit: int = 100, delay: float = 0.5
) -> list[HFObservation]:
    """조직 하나의 관측. 두 정렬을 합친다.

    한 정렬로는 신규와 수정을 둘 다 볼 수 없다. createdAt 정렬은 새 리포를,
    lastModified 정렬은 기존 리포의 변화(라이선스·arXiv 태그 추가)를 잡는다.
    리포가 많은 조직에서는 두 집합이 거의 겹치지 않는다(프로브 실측).
    """
    if not org.hf_org:
        return []
    merged: dict[str, dict] = {}
    for sort in ("createdAt", "lastModified"):
        rows = await _hf_list(client, org.hf_org, sort, limit)
        for m in rows:
            rid = m.get("id") or m.get("modelId")
            if rid:
                merged[rid] = m
        await asyncio.sleep(delay)

    observations = []
    for rid, m in merged.items():
        name = rid.split("/", 1)[-1]
        if not org.matches(name):
            continue
        obs = observation_from_hf(org.key, m)
        if obs:
            observations.append(obs)
    return observations


async def fetch_blog(client: httpx.AsyncClient, org: Org, hours: int) -> list[BlogObservation]:
    if not org.blog:
        return []
    resp = await client.get(org.blog, follow_redirects=True)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    posts = []
    for entry in feed.entries[:30]:
        link = getattr(entry, "link", None)
        title = getattr(entry, "title", "") or ""
        if not link:
            continue
        published = None
        parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if parsed:
            published = datetime(*parsed[:6], tzinfo=timezone.utc)
            if published < cutoff:
                continue
        posts.append(BlogObservation(org=org.key, title=title.strip(), url=link, published_at=published))
    return posts


async def collect_releases(
    watchlist: Watchlist, blog_hours: int = 168, limit: int = 100
) -> tuple[list[HFObservation], list[BlogObservation], list[str]]:
    """전 조직 수집. 반환: (HF 관측, 블로그 글, 조직별 오류 메시지).

    오류를 예외로 올리지 않고 목록으로 돌려주는 이유: 호출부가 "일부 실패"를
    알림으로 보내되 나머지 결과로 정상 진행해야 한다.
    """
    observations: list[HFObservation] = []
    posts: list[BlogObservation] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}) as client:
        for org in watchlist.orgs:
            try:
                got = await fetch_hf_org(client, org, limit=limit)
                observations.extend(got)
                logger.info("release_hf_collected", org=org.key, count=len(got))
            except Exception as e:  # noqa: BLE001 - 조직 단위로 흡수
                errors.append(f"{org.key} hf: {type(e).__name__}: {str(e)[:120]}")
                logger.warning("release_hf_failed", org=org.key, error=str(e)[:200])
            try:
                got_posts = await fetch_blog(client, org, blog_hours)
                posts.extend(got_posts)
                if org.blog:
                    logger.info("release_blog_collected", org=org.key, count=len(got_posts))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{org.key} blog: {type(e).__name__}: {str(e)[:120]}")
                logger.warning("release_blog_failed", org=org.key, error=str(e)[:200])

    return observations, posts, errors
