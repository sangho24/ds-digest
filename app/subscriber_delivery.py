"""구독자별 선정과 발송.

운영자(기존 단일 사용자) 흐름이 끝난 뒤에 덧붙는 단계다. 분석(LLM)은 다시 하지 않고,
그날 분석된 후보 풀(app/pool.py)에서 구독자마다 select_for_user 로 선정만 다시 한다.

원칙:
    - 구독자 한 명의 실패가 다른 구독자를 막지 않는다. 이 모듈의 함수는 예외를 올리지
      않고 결과(DeliverySummary)로 돌려준다. 호출부(daily_digest.py)는 그 위에 한 번 더
      try 를 둘러 운영자 흐름과 워크플로 성공 여부를 지킨다.
    - is_owner 구독자는 운영자 본인이다. 선정을 새로 계산하지 않고 운영자 흐름에서 이미
      뽑힌 목록을 그대로 쓴다(Discord 로 받은 것과 메일이 달라지면 안 된다).
    - 그 밖의 구독자는 빈 기본 프로필, 지시 없음(directive None)으로 선정한다. 개인화는
      그 구독자가 메일에서 누른 want/skip(feedback_events)만으로 한다.
    - 이메일 주소(가린 형태 포함), 이름, token 은 로그와 알림에 남기지 않는다. 저장소가
      공개라 워크플로 로그는 누구에게나 보인다. 식별은 구독자 id 앞 8자로만 한다.

비교군(arm) 배정 (개인화 효과 측정, PROGRESS §57. 보고는 evals/effect.py):
    같은 후보로 두 번 고른다. 개인화 선정 P(관심도 조정 포함, 지금까지의 방식)와 기준
    선정 B(관심도 조정만 끈 것)다. 받는 아이템마다 비교군을 sent_items.arm 에 남긴다.
      - 관심도 모델이 꺼져 있거나(표시 20건 미만) 두 목록이 같으면 전부 "shared".
      - 다르면 P 에만 있는 것(개인화 순위가 낮은 것부터)과 B 에만 있는 것(기준 순위가
        높은 것부터)을 순서대로 짝짓는다. 앞에서부터 EFFECT_HOLDOUT_SLOTS 개(기본 1)의
        짝이 실험 대상이다.
      - 실험 대상 짝마다 결정적 난수로 한쪽만 보낸다. 확률 EFFECT_HOLDOUT_RATE(기본 0.5)
        로 기준 쪽 아이템이 나가고("baseline"), 아니면 개인화 쪽 아이템이 나간다
        ("personal"). 난수는 sha256(구독자 id, 날짜, 짝 번호)라 같은 구독자와 날짜로 다시
        계산하면 결과가 같다(재발송 안전). 정원은 그대로다.
      - 양쪽에 다 있는 것은 "shared". 실험 대상이 아닌 P 전용 아이템은 그대로 보내되
        arm 을 None 으로 둔다. personal 과 baseline 이 같은 짝에서 무작위로 갈린 표본만
        되게 하려는 것이다(두 목록이 한 건만 다른 날에도 양쪽 표본이 절반씩 쌓인다).
      - 순위는 선정 점수(기준은 relevance_score, 개인화는 거기에 조정값을 더한 것)이고
        동점은 후보 풀의 순서(분석된 순서)로 가른다.
      - 지면 순서는 기존 규칙(표시 점수 내림차순)이라 arm 이 메일에서 드러나지 않는다.
      - is_owner 는 목록을 바꾸지 않으므로 무작위 배정이 없다. 양쪽에 다 있는 것만
        "shared" 를 붙이고 나머지는 None 이다(운영자는 baseline 을 받지 않아, personal 로
        세면 사람 간 차이가 비교에 섞인다). 운영자 목록을 같은 후보로 재현할 수 없으면
        전부 None 이다.
    배정 계산이 실패해도 발송은 한다(arm 만 None 으로 남는다).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from app import mailer
from app.config import get_settings
from app.contract import ARCHIVE_BASE
from app.models import DigestItem, UserProfile
from app.newsletter import (
    RecipientLinks,
    email_subject,
    feedback_endpoint,
    kst_date_label,
    render_digest_email,
    unsubscribe_headers,
)
from app.pool import ScoredCandidate
from app.subscribers import Subscriber, scrub_error

if TYPE_CHECKING:
    from app.directives import Directive

logger = structlog.get_logger()

ARM_PERSONAL = "personal"
ARM_BASELINE = "baseline"
ARM_SHARED = "shared"

FEEDBACK_CONFIG_NAME = "feedback-config.json"

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_NO_ITEMS = "no_items"
STATUS_ALREADY = "already_sent"
STATUS_DRY_RUN = "dry_run"


@dataclass(frozen=True)
class SubscriberOutcome:
    """구독자 한 명의 처리 결과. 주소 대신 id 앞 8자를 담는다."""

    subscriber: str
    status: str
    items: int = 0
    error: str = ""


@dataclass
class DeliverySummary:
    outcomes: list[SubscriberOutcome] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def failed(self) -> int:
        return self.count(STATUS_FAILED)

    def as_dict(self) -> dict[str, int]:
        """런 결과(dict)와 로그에 싣는 집계."""
        return {
            "total": len(self.outcomes),
            "sent": self.count(STATUS_SENT),
            "failed": self.failed,
            "already_sent": self.count(STATUS_ALREADY),
            "no_items": self.count(STATUS_NO_ITEMS),
            "dry_run": self.count(STATUS_DRY_RUN),
        }


def write_feedback_config(docs_dir: Path) -> Path | None:
    """docs/feedback-config.json 을 쓴다. SUPABASE_URL 이 없으면 쓰지 않는다.

    docs/feedback.html 이 이 파일에서 Edge Function 주소를 읽는다. 공개 페이지에 실리는
    파일이라 주소만 담고 키는 절대 넣지 않는다.
    """
    endpoint = feedback_endpoint()
    if not endpoint:
        return None
    path = docs_dir / FEEDBACK_CONFIG_NAME
    path.write_text(
        json.dumps({"endpoint": endpoint}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


@dataclass(frozen=True)
class Placement:
    """발송 목록의 한 자리. arm 은 비교군이고, 정할 수 없으면 None 이다."""

    item: DigestItem
    arm: str | None = None


@dataclass(frozen=True)
class OwnerContext:
    """운영자 흐름이 선정에 쓴 맥락. is_owner 의 기준 선정을 같은 조건으로 계산하는 데 쓴다."""

    profile: UserProfile
    directive: Directive | None = None


def holdout_draw(subscriber_id: str, date_iso: str, pair_index: int) -> float:
    """실험 대상 짝 하나의 결정적 난수(0 이상 1 미만).

    sha256(구독자 id, 날짜, 짝 번호)의 앞 8바이트를 쓴다. 프로세스나 실행 횟수와 무관하게
    같은 입력이면 같은 값이라, 같은 날 다시 계산해도 배정이 바뀌지 않는다.
    """
    digest = hashlib.sha256(f"{subscriber_id}|{date_iso}|{pair_index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def assign_arms(
    personal: list[DigestItem],
    baseline: list[DigestItem],
    personal_score: Callable[[DigestItem], float],
    pool_order: Mapping[str, int],
    holdout_slots: int,
    holdout_rate: float,
    draw: Callable[[int], float],
) -> list[Placement]:
    """개인화 목록과 기준 목록을 한 발송 목록으로 합치고 자리마다 비교군을 붙인다.

    순수 함수다. personal, baseline 은 같은 후보에서 고른 두 선정이고, personal_score 는
    개인화 선정 점수(relevance_score + 조정값), pool_order 는 url 의 후보 풀 내 순서,
    draw 는 짝 번호를 받아 0 이상 1 미만의 난수를 주는 함수다(holdout_draw).
    규칙은 모듈 독스트링의 "비교군 배정"을 따른다.
    """
    personal_urls = {d.raw.url for d in personal}
    baseline_urls = {d.raw.url for d in baseline}
    if personal_urls == baseline_urls:
        return [Placement(d, ARM_SHARED) for d in personal]

    def order(d: DigestItem) -> int:
        return pool_order.get(d.raw.url, len(pool_order))

    # 개인화 순위가 낮은 것부터. 동점이면 풀에서 뒤에 있는 쪽이 더 낮다.
    personal_only = sorted(
        (d for d in personal if d.raw.url not in baseline_urls),
        key=lambda d: (personal_score(d), -order(d)),
    )
    # 기준 순위가 높은 것부터. 기준 선정 점수는 조정 없는 relevance_score 다.
    baseline_only = sorted(
        (d for d in baseline if d.raw.url not in personal_urls),
        key=lambda d: (-d.analysis.relevance_score, order(d)),
    )
    pairs = list(zip(personal_only, baseline_only, strict=False))[: max(0, holdout_slots)]

    # url → 비교군. 실험 대상 짝에서 실제로 나가는 쪽만 personal, baseline 이 된다.
    experiment: dict[str, str] = {}
    removed: set[str] = set()
    added: list[DigestItem] = []
    for index, (personal_item, baseline_item) in enumerate(pairs):
        if draw(index) < holdout_rate:
            removed.add(personal_item.raw.url)
            added.append(baseline_item)
            experiment[baseline_item.raw.url] = ARM_BASELINE
        else:
            experiment[personal_item.raw.url] = ARM_PERSONAL

    def arm_of(d: DigestItem) -> str | None:
        if d.raw.url in experiment:
            return experiment[d.raw.url]
        # 실험 밖의 P 전용 아이템은 None 이다(무작위로 갈린 표본이 아니다).
        return ARM_SHARED if d.raw.url in baseline_urls else None

    placements = [Placement(d, arm_of(d)) for d in personal if d.raw.url not in removed]
    if not added:
        # 바꿔 넣은 것이 없으면 개인화 목록 그대로다(순서도 건드리지 않는다).
        return placements
    placements.extend(Placement(d, ARM_BASELINE) for d in added)
    # 지면 순서는 표시 점수순. 동점을 풀 순서로 갈라 arm 과 무관하게 만든다
    # (끝에 붙인 기준 자리가 동점 묶음의 맨 뒤로 몰리면 arm 이 순서로 드러난다).
    placements.sort(key=lambda p: (-p.item.analysis.relevance_score, order(p.item)))
    return placements


def _plan_for_owner(
    owner_items: list[DigestItem] | None,
    scored: list[ScoredCandidate],
    owner_context: OwnerContext | None,
) -> list[Placement]:
    """is_owner 의 발송 목록(운영자 선정 그대로)에 비교군 표시만 붙인다.

    같은 후보에서 운영자 조건(프로필, 지시)으로 개인화 선정을 다시 계산해 넘어온 목록과
    같은지 먼저 본다. 다르면(후보에 없는 url, 재발송이라 그날의 지시를 모름 등) 기준
    선정과 비교하는 것이 의미가 없으므로 arm 을 None 으로 둔다. 같으면 기준 선정에도
    있는 것만 "shared" 이고, 개인화 선정에만 있는 것은 None 이다. 운영자는 기준 쪽
    아이템을 받지 않으므로 personal 로 세지 않는다(무작위 배정이 아니다).
    목록과 순서는 어떤 경우에도 그대로다.
    """
    items = list(owner_items or [])
    unknown = [Placement(d, None) for d in items]
    if not items or not scored:
        return unknown
    try:
        from app.analyzer import select_for_user
        from app.subscribers import OWNER_USER_ID

        owner_urls = {d.raw.url for d in items}
        if not owner_urls <= {c.item.raw.url for c in scored}:
            return unknown
        profile = owner_context.profile if owner_context else UserProfile(user_id=OWNER_USER_ID)
        directive = owner_context.directive if owner_context else None
        personal = select_for_user(scored, profile, directive, user_id=OWNER_USER_ID)
        if {d.raw.url for d in personal.selected} != owner_urls:
            return unknown
        if not personal.adjustments:
            return [Placement(d, ARM_SHARED) for d in items]
        baseline = select_for_user(
            scored, profile, directive, user_id=OWNER_USER_ID, apply_interest=False
        )
        baseline_urls = {d.raw.url for d in baseline.selected}
        return [Placement(d, ARM_SHARED if d.raw.url in baseline_urls else None) for d in items]
    except Exception as e:
        logger.warning(
            "owner_arm_assignment_failed", error=f"{type(e).__name__}: {scrub_error(e)}"
        )
        return unknown


def plan_for_subscriber(
    subscriber: Subscriber,
    scored: list[ScoredCandidate],
    owner_items: list[DigestItem] | None,
    owner_context: OwnerContext | None = None,
    date_iso: str = "",
) -> list[Placement]:
    """구독자 한 명이 받을 아이템과 자리별 비교군.

    date_iso 는 무작위 배정의 씨앗에 들어간다(구독자, 날짜, 짝 번호). 같은 값으로 다시
    부르면 같은 배정이 나온다.

    is_owner 는 운영자 흐름의 선정(owner_items)을 그대로 받는다. 나머지는 이미 받은
    url(sent_items)을 뺀 후보에서 다시 선정한다. sent_items 조회가 실패하면 빼지 않고
    진행한다(fail-open: 중복보다 미발송이 더 나쁘다는 app/db.py 의 관례).
    """
    if subscriber.is_owner:
        return _plan_for_owner(owner_items, scored, owner_context)
    if not scored:
        return []

    from app.analyzer import select_for_user
    from app.db import fetch_sent_item_urls

    already = fetch_sent_item_urls(subscriber.id, [c.item.raw.url for c in scored])
    if already is None:
        logger.warning("subscriber_sent_items_unknown", subscriber=subscriber.short_id)
        already = set()
    remaining = [c for c in scored if c.item.raw.url not in already]
    profile = UserProfile(user_id=subscriber.user_id)
    personal = select_for_user(remaining, profile, directive=None, user_id=subscriber.user_id)
    # 조정이 하나도 걸리지 않았으면(모델 비활성 포함) 기준 선정과 같을 수밖에 없다.
    if not personal.adjustments:
        return [Placement(d, ARM_SHARED) for d in personal.selected]

    try:
        baseline = select_for_user(
            remaining, profile, directive=None, user_id=subscriber.user_id, apply_interest=False
        )
        adjustments = personal.adjustments
        settings = get_settings()

        def personal_score(d: DigestItem) -> float:
            adjustment = adjustments.get(id(d))
            return d.analysis.relevance_score + (adjustment[0] if adjustment else 0.0)

        return assign_arms(
            personal.selected,
            baseline.selected,
            personal_score,
            {c.item.raw.url: i for i, c in enumerate(remaining)},
            settings.effect_holdout_slots,
            settings.effect_holdout_rate,
            lambda index: holdout_draw(subscriber.id, date_iso, index),
        )
    except Exception as e:
        # 측정 장치의 실패가 발송을 막지 않는다. 개인화 목록을 그대로 보내고 arm 만 비운다.
        logger.warning(
            "subscriber_arm_assignment_failed",
            subscriber=subscriber.short_id,
            error=f"{type(e).__name__}: {scrub_error(e)}",
        )
        return [Placement(d, None) for d in personal.selected]


def select_for_subscriber(
    subscriber: Subscriber,
    scored: list[ScoredCandidate],
    owner_items: list[DigestItem] | None,
    owner_context: OwnerContext | None = None,
    date_iso: str = "",
) -> list[DigestItem]:
    """구독자 한 명이 받을 아이템(지면 순서). 비교군이 필요하면 plan_for_subscriber 를 쓴다."""
    return [
        p.item
        for p in plan_for_subscriber(subscriber, scored, owner_items, owner_context, date_iso)
    ]


def render_for_subscriber(
    subscriber: Subscriber, items: list[DigestItem], date_iso: str
) -> tuple[str, str, dict[str, str]]:
    """(제목, HTML, 헤더). 아이템마다 피드백 링크, 하단과 헤더에 수신거부가 붙는다."""
    html = render_digest_email(
        items,
        date_str=kst_date_label(date_iso),
        archive_url=f"{ARCHIVE_BASE}/{date_iso}.html",
        recipient=RecipientLinks(token=subscriber.token, date_iso=date_iso),
    )
    return email_subject(len(items), date_iso), html, unsubscribe_headers(subscriber.token)


def _deliver_one(
    subscriber: Subscriber,
    scored: list[ScoredCandidate],
    date_iso: str,
    owner_items: list[DigestItem] | None,
    already_delivered: set[str],
    dry_run_dir: Path | None,
    owner_context: OwnerContext | None = None,
) -> SubscriberOutcome:
    from app.db import has_sent_on_date, record_sent_items

    dry_run = dry_run_dir is not None
    pre_delivered = subscriber.email in already_delivered
    # 같은 날짜 호가 이미 나갔으면 다시 보내지 않는다(재실행 안전장치).
    # 확인이 실패하면(None) 보내는 쪽을 택한다.
    if not pre_delivered and has_sent_on_date(subscriber.id, date_iso):
        return SubscriberOutcome(subscriber.short_id, STATUS_ALREADY)

    placements = plan_for_subscriber(subscriber, scored, owner_items, owner_context, date_iso)
    if not placements:
        return SubscriberOutcome(subscriber.short_id, STATUS_NO_ITEMS)
    items = [p.item for p in placements]
    urls = [item.raw.url for item in items]
    # 전부 미정이면 arm 컬럼을 건드리지 않는다(None 으로 덮어쓰지 않는다).
    arms: list[str | None] | None = [p.arm for p in placements]
    if arms is not None and all(arm is None for arm in arms):
        arms = None
    # 건수만 남긴다. 어느 아이템이 어느 비교군인지는 sent_items 에만 있다.
    logger.info(
        "subscriber_arms",
        subscriber=subscriber.short_id,
        **Counter(arm or "unassigned" for arm in (arms or [None] * len(items))),
    )

    if pre_delivered:
        # 운영자 메일 단계(send_digest)가 이미 링크 든 메일을 보냈다. 기록만 남긴다.
        record_sent_items(subscriber.id, urls, date_iso, arms=arms)
        return SubscriberOutcome(subscriber.short_id, STATUS_ALREADY, items=len(items))

    subject, html, headers = render_for_subscriber(subscriber, items, date_iso)

    if dry_run:
        assert dry_run_dir is not None
        dry_run_dir.mkdir(parents=True, exist_ok=True)
        (dry_run_dir / f"{subscriber.short_id}.html").write_text(html, encoding="utf-8")
        return SubscriberOutcome(subscriber.short_id, STATUS_DRY_RUN, items=len(items))

    result = mailer.send(subscriber.email, subject, html, headers)
    if not result.ok:
        # 발송 경로의 오류 문구에 수신 주소가 섞여 나올 수 있다. 아는 주소를 지우고도
        # '@' 가 남으면 문구를 통째로 버린다(공개 로그에 주소 조각을 남기지 않는다).
        error = scrub_error(result.error, subscriber.email)
        return SubscriberOutcome(subscriber.short_id, STATUS_FAILED, len(items), error)

    # 기록 실패는 발송 성공을 뒤집지 않는다. 다만 내일 같은 아이템이 다시 갈 수 있다.
    if not record_sent_items(subscriber.id, urls, date_iso, arms=arms):
        logger.warning("subscriber_sent_items_not_recorded", subscriber=subscriber.short_id)
    return SubscriberOutcome(subscriber.short_id, STATUS_SENT, items=len(items))


def deliver_to_subscribers(
    subscribers: Iterable[Subscriber],
    scored: list[ScoredCandidate],
    date_iso: str,
    owner_items: list[DigestItem] | None = None,
    already_delivered: Iterable[str] = (),
    dry_run_dir: Path | None = None,
    owner_context: OwnerContext | None = None,
) -> DeliverySummary:
    """활성 구독자마다 선정, 렌더, 발송, sent_items 기록을 한다. 예외를 올리지 않는다.

    scored 는 그날의 후보 풀(분석된 순서)이다. owner_items 는 운영자 흐름에서 이미 선정된
    목록이고, already_delivered 는 운영자 메일 단계가 이미 링크 든 메일을 보낸 주소다.
    dry_run_dir 를 주면 발송과 DB 기록 없이 수신자별 HTML 만 그 아래에 쓴다.
    owner_context 는 운영자 흐름이 선정에 쓴 프로필과 지시다. is_owner 의 비교군 표시에만
    쓰이고, 없으면 빈 프로필과 지시 없음으로 재현을 시도한다(안 맞으면 arm 은 None).
    """
    delivered = {addr.lower() for addr in already_delivered}
    summary = DeliverySummary()
    for subscriber in subscribers:
        try:
            outcome = _deliver_one(
                subscriber, scored, date_iso, owner_items, delivered, dry_run_dir, owner_context
            )
        except Exception as e:
            # 한 구독자의 실패가 다음 구독자를 막지 않는다.
            outcome = SubscriberOutcome(
                subscriber.short_id,
                STATUS_FAILED,
                error=f"{type(e).__name__}: {scrub_error(e, subscriber.email)}",
            )
        summary.outcomes.append(outcome)
        log = logger.warning if outcome.status == STATUS_FAILED else logger.info
        log(
            "subscriber_delivery",
            subscriber=outcome.subscriber,
            status=outcome.status,
            items=outcome.items,
            error=outcome.error or None,
        )
    logger.info("subscriber_delivery_done", date=date_iso, **summary.as_dict())
    return summary
