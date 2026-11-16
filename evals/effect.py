"""구독자별 개인화의 실제 효과 보고서.

    python -m evals.effect report [--json] [--since YYYY-MM-DD] [--out PATH] [--anonymize]

무엇을 재는가:
    구독자 발송(app/subscriber_delivery.py)은 같은 후보로 개인화 선정과 기준 선정(관심도
    조정을 끈 것)을 둘 다 계산하고, 두 목록이 갈리는 자리(짝)에서 결정적 난수로 한쪽만
    보낸 뒤 비교군(sent_items.arm)을 남긴다.
      personal  실험 대상 짝에서 개인화 쪽 아이템이 나간 자리
      baseline  실험 대상 짝에서 기준 쪽 아이템이 나간 자리
      shared    두 선정에 모두 있던 아이템(모델이 꺼져 있거나 두 목록이 같은 날은 전부)
      미배정    arm 이 비어 있는 행. 무작위 배정을 거치지 않은 것이다: 실험 대상 밖의
                개인화 전용 아이템, 운영자(is_owner)의 개인화 전용 아이템, 컬럼 추가 전의
                기록, 운영자 목록을 재현하지 못한 날
    personal 과 baseline 은 같은 사람의 같은 날 같은 자리를 두고 무작위로 갈린 표본이라,
    둘의 반응 차이가 "관심도 조정이 고른 것이 조정 없이 고른 것보다 나은가"의 답이다.

지표 정의(전부 "발송 건" = sent_items 한 행 = 구독자 한 명이 받은 아이템 한 건 단위):
    반응률           = 표시(want 또는 skip)가 달린 발송 건 / 발송 건
    읽고 싶다 비율   = want 발송 건 / (want + skip 발송 건)
    클릭률           = 클릭된 발송 건 / 발송 건
    표시는 같은 구독자·url 의 feedback_events 중 created_at 이 가장 늦은 것만 본다(마음을
    바꾸면 나중 것이 현재 의사다). 클릭은 같은 구독자·url 에 몇 번이 있어도 1회다.
    발송 기록에 없는 구독자·url 의 표시와 클릭은 지표에 넣지 않고 건수만 따로 적는다.
    --since 는 sent_items.digest_date 가 그 날짜 이후인 발송 건만 남긴다.

구간과 판정:
    비율 하나의 95% 구간은 Wilson score 구간이다. 표본이 작거나 비율이 0, 1 근처여도
    구간이 [0, 1] 을 벗어나지 않아 지인 10명 규모에 맞는다.
    두 비율의 차이(personal - baseline)의 95% 구간은 Newcombe(1998)의 hybrid score 방식
    (논문의 method 10)이다. 두 Wilson 구간의 끝점을 제곱합으로 엮는다:
        하한 = d - sqrt((p1 - l1)^2 + (u2 - p2)^2)
        상한 = d + sqrt((u1 - p1)^2 + (p2 - l2)^2)
    판정은 지표마다 따로 낸다. 어느 한쪽 분모가 MIN_ARM_SAMPLES 미만이면 "판단 보류",
    구간이 0 을 포함하면 "차이 확인 안 됨", 구간 전체가 양수면 "개인화 효과 있음",
    전체가 음수면 "개인화가 오히려 나쁨". 머리 판정은 읽고 싶다 비율을 기준으로 하고
    클릭률 판정은 참고로 곁들인다(클릭에는 메일 보안 스캐너의 자동 방문이 섞인다).

지면 위치:
    기준 쪽 아이템은 표시 점수가 더 높아 메일 위쪽에 놓이기 쉽다. 위에 있을수록 눈에 띄므로
    두 비교군의 평균 position(1 이 맨 위)을 판정과 함께 찍고, 차이가 POSITION_GAP_WARNING
    이상이면 비교가 치우칠 수 있다는 경고를 붙인다. 보정은 하지 않는다.

--anonymize:
    공개 로그(GitHub Actions)용이다. 구독자별 표를 통째로 생략하고 전체, 비교군, 주차
    집계만 낸다. 사람별 수치는 id 만 가려도 누구인지 짐작할 수 있기 때문이다.

구조:
    collect() 가 Supabase 에서 읽어 EffectData 로 넘기고, compute_report() 가 그것만으로
    보고서(dict)를 만든다. compute_report 와 그 아래 함수는 네트워크도 파일도 쓰지 않는
    순수 함수다. 이메일 주소와 token 은 collect() 단계에서 버려 EffectData 에 들어가지 않는다.

Supabase 가 설정되지 않았거나 데이터가 없으면 무엇이 없는지 알리고 종료 코드 0 으로 끝난다.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 판정에 필요한 비교군별 최소 분모. 이보다 작으면 구간이 너무 넓어 어떤 결론도 내지 않는다.
MIN_ARM_SAMPLES = 30
# 정규분포 97.5% 분위수(양측 95%).
Z95 = 1.959963984540054

# 두 비교군의 평균 지면 위치가 이만큼 이상 벌어지면 경고한다.
POSITION_GAP_WARNING = 1.0
POSITION_WARNING = "지면 위치가 달라 비교가 치우칠 수 있다"
UNASSIGNED_NOTE = (
    "미배정은 무작위 배정을 거치지 않은 발송 건이다(실험 대상 밖의 개인화 전용 아이템, "
    "운영자의 개인화 전용 아이템, arm 기록 이전의 행). 판정에는 쓰지 않는다."
)
SUBSCRIBERS_OMITTED_NOTE = (
    "공개 로그용 실행(--anonymize)이라 구독자별 표를 생략했다. "
    "구독자별 표는 로컬에서 실행해 보라(python -m evals.effect report)."
)

ARM_PERSONAL = "personal"
ARM_BASELINE = "baseline"
ARM_SHARED = "shared"
ARM_UNASSIGNED = "unassigned"
ARM_ORDER = (ARM_PERSONAL, ARM_BASELINE, ARM_SHARED, ARM_UNASSIGNED)
ARM_LABELS = {
    ARM_PERSONAL: "personal",
    ARM_BASELINE: "baseline",
    ARM_SHARED: "shared",
    ARM_UNASSIGNED: "미배정",
}

VERDICT_POSITIVE = "개인화 효과 있음"
VERDICT_NEGATIVE = "개인화가 오히려 나쁨"
VERDICT_NONE = "차이 확인 안 됨"

CAVEATS = (
    "구독자 수가 적어 사람 간 편차가 구간보다 클 수 있다. 구간은 발송 건을 서로 독립인 "
    "표본으로 보고 계산했지만 실제로는 몇 사람의 반복 응답이라, 한두 명의 성향이 결과를 "
    "좌우할 수 있다. 구독자별 표와 함께 읽어라.",
    "클릭에는 메일 보안 스캐너의 자동 방문이 섞일 수 있다. 사람이 누르지 않은 링크가 "
    "클릭으로 잡히므로 클릭률은 실제보다 높게, 비교군 간 차이는 실제보다 작게 보일 수 있다.",
)

_KST = timezone(timedelta(hours=9))
_LEARNED = ("want", "skip")


# ──────────────────────────────────────────────
# 통계(순수 함수)
# ──────────────────────────────────────────────

def wilson_interval(successes: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    """이항 비율의 Wilson score 구간. total 이 0 이면 None."""
    if total <= 0:
        return None
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denominator
    # 끝점이 부동소수 오차로 0, 1 을 살짝 넘거나 못 미치는 것을 막는다.
    low = 0.0 if successes == 0 else max(0.0, center - half)
    high = 1.0 if successes == total else min(1.0, center + half)
    return low, high


def newcombe_diff_interval(
    successes1: int, total1: int, successes2: int, total2: int, z: float = Z95
) -> tuple[float, float] | None:
    """두 독립 비율의 차이(p1 - p2)의 구간. Newcombe(1998) hybrid score(method 10).

    각 비율의 Wilson 구간을 구한 뒤 끝점까지의 거리를 제곱합으로 합친다. 정규근사(Wald)와
    달리 표본이 작거나 비율이 0, 1 일 때도 구간이 [-1, 1] 안에 머문다. 어느 한쪽 분모가
    0 이면 None.
    """
    first = wilson_interval(successes1, total1, z)
    second = wilson_interval(successes2, total2, z)
    if first is None or second is None:
        return None
    p1, p2 = successes1 / total1, successes2 / total2
    diff = p1 - p2
    low = diff - math.sqrt((p1 - first[0]) ** 2 + (second[1] - p2) ** 2)
    high = diff + math.sqrt((first[1] - p1) ** 2 + (p2 - second[0]) ** 2)
    return max(-1.0, low), min(1.0, high)


def judge(interval: tuple[float, float] | None, total1: int, total2: int) -> str:
    """차이의 구간과 두 분모로 한 줄 판정을 낸다(규칙은 모듈 독스트링)."""
    smallest = min(total1, total2)
    if interval is None or smallest < MIN_ARM_SAMPLES:
        return f"판단 보류(표본 부족: 필요 {MIN_ARM_SAMPLES}, 현재 {smallest})"
    low, high = interval
    if low > 0:
        return VERDICT_POSITIVE
    if high < 0:
        return VERDICT_NEGATIVE
    return VERDICT_NONE


def _rate(successes: int, total: int) -> float | None:
    return successes / total if total else None


# ──────────────────────────────────────────────
# 입력과 집계(순수 함수)
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class EffectData:
    """보고서 계산의 입력. 행은 Supabase 가 돌려주는 dict 모양 그대로다.

    subscribers 행에는 id, name, status, is_owner, created_at, has_style_note(문체 요청의
    유무, 내용은 없다)만 쓴다. 이메일과 token 은 collect() 가 넣지 않는다(넣어도 계산은
    읽지 않는다).
    owner_local_labels 는 is_owner 의 로컬 표시(interest_labels.jsonl)다. 관심도 모델의
    활성 여부를 실제와 같게 세는 데만 쓰고 반응률에는 넣지 않는다.
    clicks_available 이 False 면 click_events 를 읽지 못한 것이라 클릭 지표를 비운다.
    """

    subscribers: list[dict[str, Any]]
    sent_items: list[dict[str, Any]]
    feedback_events: list[dict[str, Any]] = field(default_factory=list)
    click_events: list[dict[str, Any]] = field(default_factory=list)
    owner_local_labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    clicks_available: bool = True
    notes: tuple[str, ...] = ()


@dataclass
class _Tally:
    """발송 건 묶음 하나의 집계."""

    sent: int = 0
    want: int = 0
    skip: int = 0
    clicked: int = 0
    subscribers: set[str] = field(default_factory=set)
    position_sum: int = 0
    position_count: int = 0

    def add(
        self, subscriber_id: str, action: str | None, clicked: bool, position: object = None
    ) -> None:
        # position 은 arm 과 같이 추가된 컬럼이라 없는 행이 있다. 있는 행만 평균에 넣는다.
        if isinstance(position, int) and not isinstance(position, bool) and position > 0:
            self.position_sum += position
            self.position_count += 1
        self.sent += 1
        self.subscribers.add(subscriber_id)
        if action == "want":
            self.want += 1
        elif action == "skip":
            self.skip += 1
        if clicked:
            self.clicked += 1

    @property
    def responded(self) -> int:
        return self.want + self.skip

    @property
    def mean_position(self) -> float | None:
        return self.position_sum / self.position_count if self.position_count else None

    def metrics(self, clicks_available: bool) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "responded": self.responded,
            "want": self.want,
            "skip": self.skip,
            "clicked": self.clicked if clicks_available else None,
            "response_rate": _rate(self.responded, self.sent),
            "want_ratio": _rate(self.want, self.responded),
            "click_rate": _rate(self.clicked, self.sent) if clicks_available else None,
        }


def _parse_time(value: object) -> datetime:
    """ISO 시각을 비교 가능한 값으로. 없거나 못 읽으면 가장 오래된 것으로 친다."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _kst_date(value: object) -> str | None:
    """시각을 KST 날짜(YYYY-MM-DD)로. 못 읽으면 None."""
    parsed = _parse_time(value)
    if parsed.year == datetime.min.year:
        return None
    return parsed.astimezone(_KST).date().isoformat()


def _key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    subscriber_id = str(row.get("subscriber_id") or "").strip()
    url = str(row.get("item_url") or "").strip()
    return (subscriber_id, url) if subscriber_id and url else None


def latest_feedback(events: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """(구독자, url) → 가장 늦은 표시(want | skip). 시각이 같으면 뒤에 온 행이 이긴다."""
    latest: dict[tuple[str, str], tuple[datetime, str]] = {}
    for event in events:
        key = _key(event)
        action = event.get("action")
        if key is None or action not in _LEARNED:
            continue
        when = _parse_time(event.get("created_at"))
        previous = latest.get(key)
        if previous is None or when >= previous[0]:
            latest[key] = (when, str(action))
    return {key: action for key, (_, action) in latest.items()}


def clicked_pairs(events: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """클릭이 한 번이라도 있는 (구독자, url). 같은 쌍의 반복 클릭은 1회다."""
    return {key for key in (_key(event) for event in events) if key is not None}


def _arm_of(row: Mapping[str, Any]) -> str:
    arm = str(row.get("arm") or "").strip().lower()
    return arm if arm in (ARM_PERSONAL, ARM_BASELINE, ARM_SHARED) else ARM_UNASSIGNED


def _iso_week(digest_date: object) -> str:
    try:
        year, week, _ = date.fromisoformat(str(digest_date)[:10]).isocalendar()
    except (TypeError, ValueError):
        return "날짜 없음"
    return f"{year}-W{week:02d}"


def display_name(row: Mapping[str, Any]) -> str:
    """구독자 식별 문구. 이름이 없으면 id 앞 8자다.

    이름 칸에 주소가 들어 있는 경우(@ 포함)에도 id 로 대신한다. 이메일은 어떤 경우에도
    보고서에 나가지 않는다.
    """
    short = str(row.get("id") or "")[:8] or "unknown"
    name = str(row.get("name") or "").strip()
    if not name or "@" in name:
        return short
    return name


def _label_counts(data: EffectData) -> dict[str, int]:
    """구독자 id → 관심도 모델이 보는 표시 수(url 별 최신 want/skip, 기간과 무관).

    app/interest_model.py 의 load_interest_labels 와 같은 규칙이다. is_owner 는 로컬 표시를
    합치되 같은 url 은 시각이 늦은 쪽이 이기고, 나중에 unsure 로 고친 것은 빠진다.
    """
    latest: dict[tuple[str, str], tuple[datetime, str]] = {}
    for event in data.feedback_events:
        key = _key(event)
        action = event.get("action")
        if key is None or action not in _LEARNED:
            continue
        when = _parse_time(event.get("created_at"))
        previous = latest.get(key)
        if previous is None or when >= previous[0]:
            latest[key] = (when, str(action))
    owner_ids = [str(s.get("id")) for s in data.subscribers if s.get("is_owner") and s.get("id")]
    for owner_id in owner_ids:
        for url, record in data.owner_local_labels.items():
            key = (owner_id, str(url))
            when = _parse_time(record.get("labeled_at"))
            previous = latest.get(key)
            # 로컬 쪽은 Supabase 표시보다 늦을 때만 이긴다(load_interest_labels 와 같다).
            if previous is None or when > previous[0]:
                latest[key] = (when, str(record.get("human") or ""))
    counts: dict[str, int] = {}
    for (subscriber_id, _), (_, action) in latest.items():
        if action in _LEARNED:
            counts[subscriber_id] = counts.get(subscriber_id, 0) + 1
    return counts


def _comparison(
    personal: tuple[int, int], baseline: tuple[int, int], available: bool = True
) -> dict[str, Any]:
    """personal 대 baseline 의 한 지표 비교. 각 인자는 (분자, 분모)다."""
    (k1, n1), (k2, n2) = personal, baseline
    if not available:
        return {
            "personal": None,
            "baseline": None,
            "diff": None,
            "ci95": None,
            "verdict": "측정 안 됨(click_events 를 읽지 못했다)",
        }
    p1, p2 = _rate(k1, n1), _rate(k2, n2)
    interval = newcombe_diff_interval(k1, n1, k2, n2)
    return {
        "personal": {"successes": k1, "total": n1, "rate": p1},
        "baseline": {"successes": k2, "total": n2, "rate": p2},
        "diff": (p1 - p2) if p1 is not None and p2 is not None else None,
        "ci95": list(interval) if interval is not None else None,
        "verdict": judge(interval, n1, n2),
    }


def compute_report(
    data: EffectData, since: str | None = None, anonymize: bool = False
) -> dict[str, Any]:
    """EffectData 만으로 보고서(dict, JSON 으로 그대로 내보낼 수 있다)를 만든다.

    anonymize 면 구독자별 표를 만들지 않는다(공개 로그용).
    """
    feedback = latest_feedback(data.feedback_events)
    clicks = clicked_pairs(data.click_events) if data.clicks_available else set()
    by_id = {str(s.get("id")): s for s in data.subscribers if s.get("id")}

    # 발송 건. 같은 (구독자, url) 이 두 번 오면 한 건으로 본다(pk 라 원래 유일하다).
    sent: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in data.sent_items:
        key = _key(row)
        if key is None:
            continue
        if since and str(row.get("digest_date") or "")[:10] < since:
            continue
        sent[key] = row

    overall = _Tally()
    arms = {arm: _Tally() for arm in ARM_ORDER}
    per_subscriber: dict[str, _Tally] = {}
    weekly: dict[str, _Tally] = {}
    for key, row in sent.items():
        subscriber_id = key[0]
        action = feedback.get(key)
        clicked = key in clicks
        overall.add(subscriber_id, action, clicked)
        arms[_arm_of(row)].add(subscriber_id, action, clicked, row.get("position"))
        per_subscriber.setdefault(subscriber_id, _Tally()).add(subscriber_id, action, clicked)
        weekly.setdefault(_iso_week(row.get("digest_date")), _Tally()).add(
            subscriber_id, action, clicked
        )

    available = data.clicks_available
    statuses = [str(s.get("status") or "active") for s in data.subscribers]
    summary = {
        "subscribers": {
            "total": len(data.subscribers),
            "active": sum(1 for s in statuses if s == "active"),
            "unsubscribed": sum(1 for s in statuses if s == "unsubscribed"),
        },
        **overall.metrics(available),
        # 발송 기록과 짝이 맞지 않아 지표에서 빠진 표시와 클릭(기간 밖, 기록 누락, 스캐너 등).
        "unmatched_feedback": sum(1 for key in feedback if key not in sent),
        "unmatched_clicks": sum(1 for key in clicks if key not in sent),
    }

    arm_rows: list[dict[str, Any]] = []
    for arm in ARM_ORDER:
        tally = arms[arm]
        want_ci = wilson_interval(tally.want, tally.responded)
        click_ci = wilson_interval(tally.clicked, tally.sent) if available else None
        arm_rows.append(
            {
                "arm": arm,
                "label": ARM_LABELS[arm],
                **tally.metrics(available),
                "subscribers": len(tally.subscribers),
                "mean_position": tally.mean_position,
                "want_ratio_ci95": list(want_ci) if want_ci else None,
                "click_rate_ci95": list(click_ci) if click_ci else None,
            }
        )

    personal, baseline = arms[ARM_PERSONAL], arms[ARM_BASELINE]
    want_cmp = _comparison(
        (personal.want, personal.responded), (baseline.want, baseline.responded)
    )
    click_cmp = _comparison(
        (personal.clicked, personal.sent), (baseline.clicked, baseline.sent), available
    )
    personal_position, baseline_position = personal.mean_position, baseline.mean_position
    position_gap = (
        personal_position - baseline_position
        if personal_position is not None and baseline_position is not None
        else None
    )
    verdict = {
        "position": {
            "personal": personal_position,
            "baseline": baseline_position,
            "diff": position_gap,
            "warning": (
                POSITION_WARNING
                if position_gap is not None and abs(position_gap) >= POSITION_GAP_WARNING
                else None
            ),
        },
        "min_arm_samples": MIN_ARM_SAMPLES,
        "method": "Newcombe hybrid score (method 10), Wilson 95%",
        "want_ratio": want_cmp,
        "click_rate": click_cmp,
        "headline": f"판정(읽고 싶다 비율 기준): {want_cmp['verdict']}",
        "secondary": f"참고(클릭률 기준): {click_cmp['verdict']}",
    }

    # 최근 반응일: 표시와 클릭을 통틀어 가장 늦은 시각(기간과 무관).
    last_seen: dict[str, datetime] = {}
    for event in [*data.feedback_events, *(data.click_events if available else [])]:
        subscriber_id = str(event.get("subscriber_id") or "")
        when = _parse_time(event.get("created_at"))
        if subscriber_id and when > last_seen.get(
            subscriber_id, datetime.min.replace(tzinfo=timezone.utc)
        ):
            last_seen[subscriber_id] = when

    # 지연 import: 상수 하나 때문에 모듈 로드 시점에 app 을 끌어오지 않는다.
    from app.interest_model import MIN_LABELS

    label_counts = _label_counts(data)
    subscriber_rows: list[dict[str, Any]] = []
    known = list(by_id.items())
    # 명단에는 없는데 발송 기록만 남은 id(정상이라면 없다)도 빠뜨리지 않는다.
    known += [(sid, {"id": sid}) for sid in per_subscriber if sid not in by_id]
    for subscriber_id, row in [] if anonymize else known:
        tally = per_subscriber.get(subscriber_id, _Tally())
        labels = label_counts.get(subscriber_id, 0)
        seen = last_seen.get(subscriber_id)
        subscriber_rows.append(
            {
                "subscriber": display_name(row),
                "status": str(row.get("status") or "active"),
                "is_owner": bool(row.get("is_owner")),
                # 문체 요청의 유무만 싣는다(내용은 수집 단계에서 버린다).
                "has_style_note": bool(row.get("has_style_note")),
                "joined": _kst_date(row.get("created_at")),
                **tally.metrics(available),
                "interest_model_active": labels >= MIN_LABELS,
                "interest_labels": labels,
                "interest_labels_needed": MIN_LABELS,
                "last_response": _kst_date(seen.isoformat()) if seen else None,
            }
        )

    weekly_rows = [
        {"week": week, **weekly[week].metrics(available)} for week in sorted(weekly)
    ]

    return {
        "since": since,
        "summary": summary,
        "arms": arm_rows,
        "verdict": verdict,
        # anonymize 면 빈 목록이다(사람별 수치를 공개 로그에 남기지 않는다).
        "subscribers": subscriber_rows,
        "subscribers_omitted": anonymize,
        "weekly": weekly_rows,
        "caveats": list(CAVEATS),
        "notes": list(data.notes),
    }


# ──────────────────────────────────────────────
# 출력
# ──────────────────────────────────────────────

def _pct(rate: float | None, successes: int | None = None, total: int | None = None) -> str:
    if rate is None:
        return "-"
    text = f"{rate * 100:.1f}%"
    if successes is not None and total is not None:
        text += f" ({successes}/{total})"
    return text


def _ci(interval: list[float] | None) -> str:
    if not interval:
        return "-"
    return f"[{interval[0] * 100:.1f}, {interval[1] * 100:.1f}]"


def _position(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _signed_points(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:+.1f}%p"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    """탭 대신 두 칸 공백으로 벌린 표. 한글 폭 때문에 완전히 맞지는 않는다."""
    widths = [
        max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=True)
    ]
    lines = ["  ".join(str(cell).ljust(w) for cell, w in zip(header, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(w) for cell, w in zip(row, widths, strict=True)))
    return [line.rstrip() for line in lines]


def _comparison_lines(title: str, comparison: Mapping[str, Any]) -> list[str]:
    personal, baseline = comparison.get("personal"), comparison.get("baseline")
    if personal is None or baseline is None:
        return [f"{title}: {comparison['verdict']}"]
    return [
        f"{title}: personal {_pct(personal['rate'], personal['successes'], personal['total'])}"
        f" / baseline {_pct(baseline['rate'], baseline['successes'], baseline['total'])}",
        f"  차이(personal - baseline) {_signed_points(comparison['diff'])}, "
        f"95% 구간 {_ci(comparison['ci95'])}%p → {comparison['verdict']}",
    ]


def render_text(report: Mapping[str, Any]) -> str:
    """사람이 읽는 보고서."""
    summary = report["summary"]
    people = summary["subscribers"]
    lines: list[str] = ["개인화 효과 보고서"]
    lines.append(f"기간: {report['since']} 이후 발송분" if report.get("since") else "기간: 전체")
    lines.append("")

    lines.append("(a) 전체 요약")
    lines.append(
        f"  구독자 {people['total']}명 (활성 {people['active']}, 해지 {people['unsubscribed']})"
    )
    lines.append(f"  발송 건 {summary['sent']}")
    lines.append(
        f"  반응률 {_pct(summary['response_rate'], summary['responded'], summary['sent'])}"
    )
    lines.append(
        f"  읽고 싶다 비율 {_pct(summary['want_ratio'], summary['want'], summary['responded'])}"
    )
    lines.append(f"  클릭률 {_pct(summary['click_rate'], summary['clicked'], summary['sent'])}")
    if summary["unmatched_feedback"] or summary["unmatched_clicks"]:
        lines.append(
            f"  발송 기록과 짝이 없어 뺀 것: 표시 {summary['unmatched_feedback']}건, "
            f"클릭 {summary['unmatched_clicks']}건"
        )
    lines.append("")

    lines.append("(b) 비교군별 (구간은 Wilson 95%, 단위 %)")
    lines.extend(
        "  " + line
        for line in _table(
            ["비교군", "발송 건", "구독자", "읽고 싶다 비율", "95% 구간", "클릭률", "95% 구간"],
            [
                [
                    arm["label"],
                    str(arm["sent"]),
                    str(arm["subscribers"]),
                    _pct(arm["want_ratio"], arm["want"], arm["responded"]),
                    _ci(arm["want_ratio_ci95"]),
                    _pct(arm["click_rate"], arm["clicked"], arm["sent"]),
                    _ci(arm["click_rate_ci95"]),
                ]
                for arm in report["arms"]
            ],
        )
    )
    lines.append(f"  * {UNASSIGNED_NOTE}")
    lines.append("")

    verdict = report["verdict"]
    lines.append(
        f"(c) 핵심 판정: personal 대 baseline (차이의 구간은 Newcombe hybrid score, "
        f"비교군별 최소 표본 {verdict['min_arm_samples']})"
    )
    lines.extend("  " + line for line in _comparison_lines("읽고 싶다 비율", verdict["want_ratio"]))
    lines.extend("  " + line for line in _comparison_lines("클릭률", verdict["click_rate"]))
    position = verdict["position"]
    lines.append(
        f"  평균 지면 위치(1 이 맨 위): personal {_position(position['personal'])}"
        f" / baseline {_position(position['baseline'])}"
    )
    lines.append(f"  ▶ {verdict['headline']}")
    lines.append(f"    {verdict['secondary']}")
    if position["warning"]:
        lines.append(f"    ⚠ {position['warning']} (평균 위치 차이 {position['diff']:+.1f})")
    lines.append("")

    lines.append("(d) 구독자별")
    if report.get("subscribers_omitted"):
        lines.append(f"  {SUBSCRIBERS_OMITTED_NOTE}")
    else:
        lines.extend(_subscriber_lines(report))
    lines.append("")

    lines.append("(e) 주차별 추세 (ISO 주, 발송일 기준)")
    lines.extend(_weekly_lines(report))
    lines.append("")

    if report.get("notes"):
        lines.append("수집 메모")
        lines.extend(f"  - {note}" for note in report["notes"])
        lines.append("")

    lines.append("(f) 해석 주의")
    lines.extend(f"  - {caveat}" for caveat in report["caveats"])
    return "\n".join(lines) + "\n"


def _subscriber_lines(report: Mapping[str, Any]) -> list[str]:
    return list(
        "  " + line
        for line in _table(
            ["구독자", "상태", "가입일", "받은 건", "반응률", "읽고 싶다 비율", "클릭률",
             "관심도 모델", "문체 요청", "최근 반응일"],
            [
                [
                    row["subscriber"] + (" (운영자)" if row["is_owner"] else ""),
                    "활성" if row["status"] == "active" else "해지",
                    row["joined"] or "-",
                    str(row["sent"]),
                    _pct(row["response_rate"], row["responded"], row["sent"]),
                    _pct(row["want_ratio"], row["want"], row["responded"]),
                    _pct(row["click_rate"], row["clicked"], row["sent"]),
                    f"{'활성' if row['interest_model_active'] else '비활성'} "
                    f"({row['interest_labels']}/{row['interest_labels_needed']})",
                    "있음" if row.get("has_style_note") else "없음",
                    row["last_response"] or "-",
                ]
                for row in report["subscribers"]
            ],
        )
    )


def _weekly_lines(report: Mapping[str, Any]) -> list[str]:
    return list(
        "  " + line
        for line in _table(
            ["주", "발송 건", "반응률", "읽고 싶다 비율", "클릭률"],
            [
                [
                    row["week"],
                    str(row["sent"]),
                    _pct(row["response_rate"], row["responded"], row["sent"]),
                    _pct(row["want_ratio"], row["want"], row["responded"]),
                    _pct(row["click_rate"], row["clicked"], row["sent"]),
                ]
                for row in report["weekly"]
            ],
        )
    )


# ──────────────────────────────────────────────
# 수집(Supabase). 여기부터는 순수 함수가 아니다.
# ──────────────────────────────────────────────

_SUBSCRIBER_FIELDS = (
    "id", "name", "status", "is_owner", "created_at", "signup_source", "consented_at"
)


class NoData(Exception):
    """보고서를 만들 수 없는 정상적인 사유(미설정, 빈 테이블, 조회 실패). 종료 코드는 0 이다."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__("; ".join(missing))
        self.missing = missing


def collect(since: str | None = None) -> EffectData:
    """Supabase(service role)에서 네 테이블을 읽는다. 만들 수 없으면 NoData 를 올린다."""
    from app.config import get_settings

    if not get_settings().subscribers_enabled:
        raise NoData(["Supabase 가 설정되지 않았다(SUPABASE_URL, SUPABASE_SERVICE_KEY)"])

    from app import db
    from app.interest_model import DEFAULT_USER_ID, _load_local_labels

    rows = db.fetch_subscribers()
    if rows is None:
        raise NoData(["subscribers 조회에 실패했다(키가 service role 인지, 테이블이 있는지 확인)"])
    if not rows:
        raise NoData(["subscribers 가 비어 있다(구독자 0명)"])
    # 이메일과 token 은 여기서 버린다. 뒤 단계 어디에도 넘어가지 않는다.
    # 문체 요청(style_note)도 내용은 버리고 유무만 남긴다(컬럼이 없으면 전원 없음).
    subscribers = [
        {
            **{name: row.get(name) for name in _SUBSCRIBER_FIELDS},
            "has_style_note": bool(str(row.get("style_note") or "").strip()),
        }
        for row in rows
    ]

    sent = db.fetch_sent_items_for_report(since)
    if sent is None:
        raise NoData(["sent_items 조회에 실패했다"])
    if not sent:
        scope = f"{since} 이후 " if since else ""
        raise NoData([f"sent_items 에 {scope}발송 기록이 없다(구독자 발송이 아직 돌지 않았다)"])

    notes: list[str] = []
    feedback = db.fetch_feedback_events_for_report()
    if feedback is None:
        notes.append("feedback_events 조회에 실패해 표시를 0건으로 계산했다")
        feedback = []
    clicks = db.fetch_click_events_for_report()
    clicks_available = clicks is not None
    if clicks is None:
        notes.append("click_events 를 읽지 못해 클릭 지표를 비웠다(마이그레이션 적용 전일 수 있다)")
        clicks = []
    if all(row.get("arm") is None for row in sent):
        notes.append("sent_items 에 arm 이 하나도 없다(컬럼 추가 전이거나 배정이 아직 돌지 않았다)")

    local: dict[str, dict[str, Any]] = {}
    if any(row.get("is_owner") for row in subscribers):
        try:
            local = dict(_load_local_labels(DEFAULT_USER_ID, None))
        except Exception as e:
            notes.append(f"운영자의 로컬 표시를 읽지 못했다({type(e).__name__})")

    return EffectData(
        subscribers=subscribers,
        sent_items=sent,
        feedback_events=feedback,
        click_events=clicks,
        owner_local_labels=local,
        clicks_available=clicks_available,
        notes=tuple(notes),
    )


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as e:
        raise argparse.ArgumentTypeError("YYYY-MM-DD 형식이어야 한다") from e


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 보고서를 만들었든 만들 데이터가 없든 0 이다(주간 CI 를 깨지 않는다)."""
    parser = argparse.ArgumentParser(description="구독자별 개인화 효과 보고서")
    sub = parser.add_subparsers(dest="command", required=True)
    report_parser = sub.add_parser("report", help="보고서를 출력한다")
    report_parser.add_argument("--json", action="store_true", help="기계 판독용 JSON")
    report_parser.add_argument(
        "--since", type=_iso_date, default=None, help="이 날짜(digest_date) 이후 발송분만"
    )
    report_parser.add_argument("--out", default=None, help="같은 내용을 이 파일에도 쓴다")
    report_parser.add_argument(
        "--anonymize", action="store_true", help="구독자별 표를 생략한다(공개 로그용)"
    )
    args = parser.parse_args(argv)

    try:
        data = collect(args.since)
    except NoData as e:
        if args.json:
            text = json.dumps(
                {"status": "no_data", "missing": e.missing}, ensure_ascii=False, indent=2
            ) + "\n"
        else:
            text = "효과 보고서를 만들지 않았다. 없는 것:\n" + "".join(
                f"  - {item}\n" for item in e.missing
            )
    else:
        report = compute_report(data, since=args.since, anonymize=args.anonymize)
        if args.json:
            text = json.dumps({"status": "ok", **report}, ensure_ascii=False, indent=2) + "\n"
        else:
            text = render_text(report)

    sys.stdout.write(text)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    # Windows 터미널이 cp949일 때 유니코드 출력 실패 방지(daily_digest 와 동일)
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    # 조회 경고(structlog)는 stderr 로 보낸다. stdout 은 보고서(특히 --json)만 담는다.
    import structlog

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    sys.exit(main())
