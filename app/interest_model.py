"""관심도 표시(want/skip)를 아이템 선정에 되먹이는 계층.

왜 필요한가:
    evals/interest.py 가 쌓는 `data/evals/interest_labels.jsonl` 은 지금까지 보고 전용이었다.
    사용자가 "읽고 싶다 / 아니다" 를 표시해도 다음 선정은 그대로였다. 이 모듈은 그 표시를
    출처 계열·도메인·콘텐츠 유형·태그 단위로 일반화해, 선정 단계의 정렬 점수에 작은 가감을 준다.

어디에 반영하고, 어디에 반영하지 않는가:
    app/preferences.py 의 원칙을 그대로 따른다. 근거 점수(relevance_score, actionability, depth)에는
    취향을 넣지 않는다. 저장·표시되는 점수와 바닥값 판정은 조정 전 값 그대로이고, 이 모듈의
    조정값은 선정 순서를 정하는 데만 쓴다(app/analyzer.py 의 filter_and_analyze).

    다만 👍/👎 가 동점 타이브레이크에만 쓰이는 것과 달리, 여기의 조정은 최대 ±MAX_ADJUST 점까지
    점수 차를 넘어 순서를 바꾼다. 표본이 쌓인 뒤에만 켜지고(MIN_LABELS), 표본이 적은 특징은
    전체 평균 쪽으로 수축하므로(SHRINKAGE_K) 몇 건의 표시로 순서가 흔들리지 않는다.

LLM 호출은 없다. 순수 계산이다.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "data" / "records"
# 후보 풀(app/pool.py). 정본에는 운영자에게 발송된 것만 있어서, 구독자가 표시한 아이템
# (운영자에게는 탈락한 후보일 수 있다)의 특징은 여기서 찾는다.
POOL_DIR = ROOT / "data" / "pool"
DEFAULT_LABELS_PATH = ROOT / "data" / "evals" / "interest_labels.jsonl"

# 다중 사용자 확장용 키. 레코드에 user_id 가 없으면 이 값으로 본다(지금 쌓이는 라벨은 전부 한 사람 것이다).
DEFAULT_USER_ID = "default"
# 모델에 들어가는 표시 값. unsure 는 방향이 없어 학습에서 뺀다.
LEARNED_CHOICES = ("want", "skip")

# 특징 종류. 태그는 소문자로 정규화해 센다(app/preferences.py 와 같은 규칙).
FEATURE_KINDS = ("source", "domain", "tag", "content_type")
# 종류 가중. 출처는 반복 소비되는 단위라 신호가 가장 진하고, 도메인은 닫힌 어휘 9개라 표본이 빨리 찬다.
# 태그는 1회성 고유명사가 많아 일치해도 정보가 적고, 콘텐츠 유형은 값이 8개뿐이라 변별이 가장 약하다.
KIND_WEIGHTS: dict[str, float] = {"source": 0.4, "domain": 0.3, "tag": 0.2, "content_type": 0.1}
# 수축 강도. 가상의 "평균적인 표시" 5건을 섞는다. 표본 3건짜리 특징은 편차의 3/8 만 남는다.
SHRINKAGE_K = 5.0
# 특징 값 하나가 모델에 들어가는 최소 표본. 1~2건은 우연과 구분되지 않는다.
MIN_FEATURE_COUNT = 3
# 모델을 켜는 최소 want+skip 수. 하루 5건 발송 기준 나흘치로, 그 아래에서는 전체 비율(p0)부터 불안정하다.
MIN_LABELS = 20
# 조정 폭 상한(점). relevance_score 는 0~10 정수이고 실측 IQR 이 1 이라, 1.5 는 한 등급을 넘기되 두 등급은 못 넘긴다.
MAX_ADJUST = 1.5
# 이 편차가 MAX_ADJUST 에 대응한다. 전체 비율보다 50%p 높거나 낮으면 상한까지 민다.
DEVIATION_AT_MAX = 0.5


@dataclass(frozen=True)
class FeatureStat:
    """특징 값 하나의 표시 집계와 수축 비율."""

    kind: str
    value: str
    want: int
    skip: int
    rate: float
    deviation: float

    @property
    def count(self) -> int:
        return self.want + self.skip


@dataclass(frozen=True)
class ItemFeatures:
    """아이템 하나에서 뽑은 특징. 라벨 쪽과 후보 쪽이 같은 함수(item_features)로 만든다."""

    source: str = ""
    domains: tuple[str, ...] = ()
    content_type: str = ""
    tags: tuple[str, ...] = ()

    def values(self, kind: str) -> tuple[str, ...]:
        """종류별 값 목록. 빈 값은 내지 않는다."""
        if kind == "source":
            return (self.source,) if self.source else ()
        if kind == "domain":
            return self.domains
        if kind == "content_type":
            return (self.content_type,) if self.content_type else ()
        if kind == "tag":
            return self.tags
        return ()


@dataclass
class InterestModel:
    """표시에서 학습한 특징별 편차. 비활성이면 모든 조정값이 0 이다."""

    user_id: str = DEFAULT_USER_ID
    active: bool = False
    # want + skip 수(unsure 제외). 활성 판정의 분자다.
    label_count: int = 0
    # 전체 want 비율(p0). 라벨이 없으면 0.
    base_rate: float = 0.0
    # 종류 → 값 → 집계. 표본이 MIN_FEATURE_COUNT 미만인 값은 들어 있지 않다.
    features: dict[str, dict[str, FeatureStat]] = field(default_factory=dict)


def _normalize_tag(tag: object) -> str:
    return str(tag).strip().lower()


def _clean_strings(values: object) -> list[str]:
    """문자열 목록으로 정규화. None, 단일 문자열, 빈 항목을 방어한다."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    return [str(v).strip() for v in values if v is not None and str(v).strip()]


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    """순서를 지키며 중복을 없앤다. 같은 값이 한 아이템에서 두 번 세이면 안 된다."""
    return tuple(dict.fromkeys(values))


def item_features(
    source_key: str | None,
    domain: Iterable[str] | str | None = None,
    content_type: str | None = None,
    tags: Iterable[str] | str | None = None,
) -> ItemFeatures:
    """원시 필드를 특징으로. 출처는 계열(source_family)로 접는다."""
    # analyzer 가 이 모듈을 가져오므로 최상단에서 가져오면 순환 참조가 된다.
    # 계열 규칙을 여기서 다시 구현하면 정규화와 학습의 단위가 갈라지므로 재사용한다.
    from app.analyzer import source_family

    return ItemFeatures(
        source=source_family(str(source_key or "").strip()),
        domains=_dedupe(_clean_strings(domain)),
        content_type=str(content_type or "").strip(),
        tags=_dedupe(_normalize_tag(t) for t in _clean_strings(tags)),
    )


def _parse_time(value: object) -> datetime:
    """ISO 시각을 비교 가능한 값으로. 없거나 못 읽으면 가장 오래된 것으로 친다."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_local_labels(user_id: str, path: Path | str | None) -> dict[str, dict[str, object]]:
    """jsonl 의 url 별 마지막 레코드(unsure 포함). 다른 사용자의 표시는 뺀다."""
    labels_path = Path(path) if path is not None else DEFAULT_LABELS_PATH
    latest: dict[str, dict[str, object]] = {}
    if not labels_path.is_file():
        return latest
    try:
        lines = labels_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return latest
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        url = str(record.get("url") or "").strip()
        if not url:
            continue
        owner = str(record.get("user_id") or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID
        if owner != user_id:
            continue
        latest[url] = record
    return latest


def _load_subscriber_labels(user_id: str) -> dict[str, dict[str, object]]:
    """Supabase feedback_events 에서 이 사용자의 url 별 최신 want/skip 을 읽는다.

    user_id 가 "default" 면 is_owner 구독자(운영자 본인)의 표시를, 그 밖이면 그 값을
    subscriber id 로 보고 읽는다. Supabase 미설정, 드라이런, 조회 실패는 전부 빈 dict 다
    (fail-open: 라벨을 못 읽으면 조정 없이 선정한다).
    """
    try:
        from app.config import get_settings

        settings = get_settings()
        if settings.dry_run or not settings.subscribers_enabled:
            return {}
        from app.db import fetch_feedback_events, fetch_subscribers

        if user_id == DEFAULT_USER_ID:
            rows = fetch_subscribers() or []
            subscriber_ids = [str(r["id"]) for r in rows if r.get("is_owner") and r.get("id")]
        else:
            subscriber_ids = [user_id]
        events = fetch_feedback_events(subscriber_ids) or []
    except Exception as e:
        from app.subscribers import scrub_error

        logger.warning("interest_subscriber_labels_failed", error=scrub_error(e))
        return {}

    latest: dict[str, dict[str, object]] = {}
    # 오래된 것부터 온다. 그래도 시각을 비교해, 순서가 섞여 와도 늦은 쪽이 이기게 한다.
    for event in events:
        url = str(event.get("item_url") or "").strip()
        action = event.get("action")
        if not url or action not in LEARNED_CHOICES:
            continue
        record: dict[str, object] = {
            "url": url,
            "human": action,
            "user_id": user_id,
            "date": event.get("digest_date"),
            "labeled_at": event.get("created_at"),
        }
        previous = latest.get(url)
        if previous is None or _parse_time(record["labeled_at"]) >= _parse_time(
            previous.get("labeled_at")
        ):
            latest[url] = record
    return latest


def load_interest_labels(
    user_id: str = DEFAULT_USER_ID, path: Path | str | None = None
) -> dict[str, dict[str, object]]:
    """url → 표시 레코드. unsure 와 다른 사용자의 표시는 뺀다.

    출처는 둘이다.
      - interest_labels.jsonl: 운영자가 evals/interest.py 로 남긴 표시. 같은 url 이 여러
        번이면 뒤의 것이 이긴다(evals.interest.load_labels 와 같은 규칙). 나중에 unsure 로
        고친 표시는 앞의 want/skip 을 지운다. 파일이 없거나 깨진 줄은 조용히 넘어간다.
      - Supabase feedback_events: 구독자가 메일의 "읽고 싶다 / 아니다" 로 남긴 표시.

    user_id 가 "default" 면 jsonl 에 is_owner 구독자의 feedback_events 를 합치고, 같은
    url 은 시각이 늦은 쪽이 이긴다. 그 밖의 user_id(구독자 id)는 jsonl 에 표시가 없으므로
    사실상 feedback_events 만 쓴다. Supabase 조회가 실패하면 jsonl 만으로 계속 간다.
    """
    merged = _load_local_labels(user_id, path)
    for url, record in _load_subscriber_labels(user_id).items():
        previous = merged.get(url)
        if previous is None or _parse_time(record.get("labeled_at")) >= _parse_time(
            previous.get("labeled_at")
        ):
            merged[url] = record
    return {url: record for url, record in merged.items() if record.get("human") in LEARNED_CHOICES}


def _record_features(
    urls: set[str], records_dir: Path, pool_dir: Path | None = None
) -> dict[str, ItemFeatures]:
    """라벨된 url 을 정본과 후보 풀에서 찾아 특징으로.

    같은 url 이 여러 날 실렸으면 최신 날짜가 이긴다. 풀을 먼저 읽고 정본을 나중에 읽어,
    둘 다에 있으면 실제로 발송된 정본 쪽 값이 남는다. 풀은 구독자 라벨용이다: 정본에는
    운영자에게 발송된 것만 있어, 구독자만 받은 아이템은 풀에서만 찾을 수 있다.
    """
    found: dict[str, ItemFeatures] = {}
    if not urls:
        return found
    paths: list[Path] = []
    if pool_dir is not None and pool_dir.is_dir():
        paths.extend(sorted(pool_dir.glob("pool_????-??-??.json")))
    if records_dir.is_dir():
        paths.extend(sorted(records_dir.glob("digest_????-??-??.json")))
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        for entry in record.get("items") or []:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}
            analysis = entry.get("analysis") if isinstance(entry.get("analysis"), dict) else {}
            url = str(raw.get("url") or "").strip()
            if url not in urls:
                continue
            found[url] = item_features(
                raw.get("source_key") or raw.get("source_name"),
                analysis.get("domain"),
                analysis.get("content_type"),
                analysis.get("tags"),
            )
    return found


def build_interest_model(
    labels: Mapping[str, Mapping[str, object]],
    records_dir: Path | str | None = None,
    user_id: str = DEFAULT_USER_ID,
    pool_dir: Path | str | None = None,
) -> InterestModel:
    """라벨(url → 레코드)을 정본·후보 풀과 조인해 특징별 수축 비율을 계산한다.

    rate = (want + K * p0) / (n + K), 편차 = rate - p0.
    p0 는 전체 want 비율이다. 표본이 적은 값일수록 p0 쪽으로 끌려가 편차가 0 에 가까워진다.

    want + skip 이 MIN_LABELS 미만이면 비활성 모델을 돌려주고 정본은 읽지 않는다.
    정본과 풀 어디에서도 못 찾은 url 은 라벨에 적힌 source_key 만으로 출처 특징을 낸다.
    pool_dir 를 넘기지 않으면 기본 풀 경로(data/pool)를 본다.
    """
    learned = {
        url: record for url, record in labels.items() if record.get("human") in LEARNED_CHOICES
    }
    want_total = sum(1 for record in learned.values() if record.get("human") == "want")
    label_count = len(learned)
    base_rate = want_total / label_count if label_count else 0.0
    model = InterestModel(user_id=user_id, label_count=label_count, base_rate=base_rate)
    if label_count < MIN_LABELS:
        return model

    directory = Path(records_dir) if records_dir is not None else RECORDS_DIR
    pool_directory = Path(pool_dir) if pool_dir is not None else POOL_DIR
    by_url = _record_features(set(learned), directory, pool_directory)

    counts: dict[str, dict[str, Counter[str]]] = {kind: {} for kind in FEATURE_KINDS}
    for url, record in learned.items():
        features = by_url.get(url)
        if features is None:
            source_key = record.get("source_key")
            features = item_features(str(source_key) if source_key else None)
        human = str(record.get("human"))
        for kind in FEATURE_KINDS:
            for value in features.values(kind):
                counts[kind].setdefault(value, Counter())[human] += 1

    for kind in FEATURE_KINDS:
        stats: dict[str, FeatureStat] = {}
        for value, counter in counts[kind].items():
            want, skip = counter["want"], counter["skip"]
            total = want + skip
            if total < MIN_FEATURE_COUNT:
                continue
            rate = (want + SHRINKAGE_K * base_rate) / (total + SHRINKAGE_K)
            stats[value] = FeatureStat(
                kind=kind, value=value, want=want, skip=skip, rate=rate, deviation=rate - base_rate
            )
        if stats:
            model.features[kind] = stats
    model.active = True
    return model


def _to_points(deviation: float) -> float:
    """편차를 점수 단위로. ±DEVIATION_AT_MAX 가 ±MAX_ADJUST 에 대응하고 그 밖은 자른다."""
    scaled = deviation / DEVIATION_AT_MAX * MAX_ADJUST
    return max(-MAX_ADJUST, min(MAX_ADJUST, scaled))


def feature_points(stat: FeatureStat) -> float:
    """이 특징 하나만 매칭됐을 때의 조정값(점). 보고에서 특징끼리 폭을 비교하는 데 쓴다."""
    return _to_points(stat.deviation)


def ranked_features(model: InterestModel) -> list[FeatureStat]:
    """모델의 특징을 편차 내림차순으로. 동률은 표본이 많은 쪽, 그다음 이름순."""
    stats = [stat for by_value in model.features.values() for stat in by_value.values()]
    stats.sort(key=lambda s: (-s.deviation, -s.count, s.kind, s.value))
    return stats


def interest_adjust(features: ItemFeatures, model: InterestModel) -> tuple[float, list[str]]:
    """아이템 특징에 대한 선정 점수 조정값과 근거.

    종류별로 매칭된 값들의 편차를 평균하고, 매칭된 종류끼리 가중을 재정규화해 합친다.
    한 종류도 매칭되지 않으면 (0.0, []) 이다. 근거는 "source:arxiv +0.80" 형태이며,
    각 근거의 숫자는 그 종류가 기여한 점수라 합치면 자르기 전 조정값이 된다.
    """
    if not model.active:
        return 0.0, []

    matched: list[tuple[str, list[str], float]] = []
    for kind in FEATURE_KINDS:
        known = model.features.get(kind) or {}
        hits = [value for value in features.values(kind) if value in known]
        if not hits:
            continue
        mean_deviation = sum(known[value].deviation for value in hits) / len(hits)
        matched.append((kind, hits, mean_deviation))
    if not matched:
        return 0.0, []

    weight_sum = sum(KIND_WEIGHTS[kind] for kind, _, _ in matched)
    scale = MAX_ADJUST / DEVIATION_AT_MAX
    combined = 0.0
    reasons: list[str] = []
    for kind, hits, mean_deviation in matched:
        share = KIND_WEIGHTS[kind] / weight_sum * mean_deviation
        combined += share
        reasons.append(f"{kind}:{','.join(hits)} {share * scale:+.2f}")
    return round(_to_points(combined), 3), reasons
