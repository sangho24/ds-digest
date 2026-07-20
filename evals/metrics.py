"""과거 콘텐츠 산출물에서 품질 신호를 계산하는 순수 함수 모음."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import math
import statistics
from typing import Any, Iterable
from urllib.parse import urlparse


Item = dict[str, Any]


def _round(value: float) -> float:
    """JSON과 콘솔에서 안정적으로 비교할 수 있도록 소수 여섯 자리로 제한한다."""
    return round(value, 6)


def _percentile(sorted_values: list[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _number_stats(values: Iterable[float | int]) -> dict[str, int | float | None]:
    numbers = [float(value) for value in values]
    if not numbers:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "stddev": None,
            "q1": None,
            "q3": None,
            "iqr": None,
        }

    ordered = sorted(numbers)
    q1 = _percentile(ordered, 0.25)
    q3 = _percentile(ordered, 0.75)
    assert q1 is not None and q3 is not None
    return {
        "count": len(numbers),
        "min": _round(ordered[0]),
        "max": _round(ordered[-1]),
        "mean": _round(statistics.fmean(numbers)),
        "median": _round(statistics.median(numbers)),
        "stddev": _round(statistics.pstdev(numbers)),
        "q1": _round(q1),
        "q3": _round(q3),
        "iqr": _round(q3 - q1),
    }


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _display_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def tag_entropy(items: list[Item]) -> dict[str, Any]:
    """태그 분포의 다양성을 측정한다.

    잡아내는 현상: 일부 태그에 생성 결과가 몰려 분류 체계가 정보를 잃는 현상.
    경보 임계값: 정규화 엔트로피가 0.70 미만이면 WARN으로 본다.
    """
    counts: Counter[str] = Counter()
    tagged_items = 0
    for item in items:
        tags = [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()]
        if tags:
            tagged_items += 1
        counts.update(tags)

    total = sum(counts.values())
    entropy = 0.0
    if total:
        entropy = -sum((count / total) * math.log2(count / total) for count in counts.values())
    normalized = entropy / math.log2(len(counts)) if len(counts) > 1 else 0.0
    return {
        "total_items": len(items),
        "tagged_items": tagged_items,
        "total_assignments": total,
        "distinct_tags": len(counts),
        "entropy_bits": _round(entropy),
        "normalized_entropy": _round(normalized),
        "tag_counts": dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))),
    }


def tag_concentration(items: list[Item]) -> dict[str, Any]:
    """각 태그가 전체 아이템 중 몇 건에 붙었는지와 최빈 비율을 계산한다.

    잡아내는 현상: MLOps처럼 범용 태그 하나가 거의 모든 결과에 자동 부착되는 현상.
    경보 임계값: 최빈 태그 부착 비율이 0.40을 초과하면 FAIL이다.
    """
    counts: Counter[str] = Counter()
    for item in items:
        unique_tags = {str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()}
        counts.update(unique_tags)

    top_tag, top_count = (counts.most_common(1)[0] if counts else (None, 0))
    denominator = len(items)
    return {
        "total_items": denominator,
        "top_tag": top_tag,
        "top_tag_item_count": top_count,
        "concentration": _round(top_count / denominator) if denominator else 0.0,
        "tag_item_counts": dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))),
    }


def score_distribution(items: list[Item]) -> dict[str, Any]:
    """관련도 점수의 분포와 산포를 계산한다.

    잡아내는 현상: 점수가 7~9점 같은 좁은 구간에만 몰려 순위 판별력을 잃는 현상.
    경보 임계값: IQR이 1.5 미만이거나 고유 점수가 5개 미만이면 FAIL이다.
    """
    scores: list[float] = []
    for item in items:
        value = item.get("relevance")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            scores.append(float(value))

    histogram = Counter(_display_number(score) for score in scores)
    stats = _number_stats(scores)
    return {
        "total_items": len(items),
        "valid_scores": len(scores),
        "missing_scores": len(items) - len(scores),
        "histogram": dict(sorted(histogram.items(), key=lambda pair: float(pair[0]))),
        "mean": stats["mean"],
        "stddev": stats["stddev"],
        "q1": stats["q1"],
        "q3": stats["q3"],
        "iqr": stats["iqr"],
        "distinct_values": len(set(scores)),
    }


def source_reach(
    items: list[Item],
    window_days: int,
    *,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """소스별 수집량, 최종 등장일과 장기 미등장 여부를 계산한다.

    잡아내는 현상: 수집기 장애나 피드 변경 때문에 특정 소스가 조용히 끊기는 현상.
    경보 임계값: 기준일로부터 30일 이상 미등장한 소스가 하나라도 있으면 WARN이다.
    """
    if window_days < 0:
        raise ValueError("window_days는 0 이상이어야 합니다.")

    counts: Counter[str] = Counter()
    last_seen: dict[str, date] = {}
    valid_dates: list[date] = []
    for item in items:
        source = str(item.get("source_name") or "(소스 없음)")
        counts[source] += 1
        item_date = _parse_date(item.get("date"))
        if item_date is None:
            continue
        valid_dates.append(item_date)
        if source not in last_seen or item_date > last_seen[source]:
            last_seen[source] = item_date

    # 직접 호출에서는 데이터 최종일을 기준으로 삼아 입력만으로 결과가 결정되게 한다.
    # 운영 실행기는 오늘 날짜를 명시적으로 주입한다.
    effective_reference_date = reference_date or (max(valid_dates) if valid_dates else None)
    sources: dict[str, dict[str, Any]] = {}
    stale_sources: list[dict[str, Any]] = []
    for source in sorted(counts):
        seen = last_seen.get(source)
        days_since = (effective_reference_date - seen).days if effective_reference_date and seen else None
        details = {
            "item_count": counts[source],
            "last_seen": seen.isoformat() if seen else None,
            "days_since_last_seen": days_since,
        }
        sources[source] = details
        if days_since is not None and days_since >= window_days:
            stale_sources.append({"source_name": source, **details})

    stale_sources.sort(key=lambda source: (-source["days_since_last_seen"], source["source_name"]))
    return {
        "window_days": window_days,
        "reference_date": effective_reference_date.isoformat() if effective_reference_date else None,
        "source_count": len(counts),
        "sources": sources,
        "stale_source_count": len(stale_sources),
        "stale_sources": stale_sources,
    }


def duplicate_rate(items: list[Item]) -> dict[str, Any]:
    """중복 URL 발생 비율과 같은 URL의 연속 등장 간격을 계산한다.

    잡아내는 현상: 이미 다룬 콘텐츠가 주기적으로 재수집되어 지면을 낭비하는 현상.
    경보 임계값: 중복 URL 발생분이 전체 아이템의 0.05를 초과하면 FAIL이다.
    """
    url_dates: dict[str, list[date]] = defaultdict(list)
    url_counts: Counter[str] = Counter()
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        url_counts[url] += 1
        item_date = _parse_date(item.get("date"))
        if item_date is not None:
            url_dates[url].append(item_date)

    duplicate_occurrences = sum(count - 1 for count in url_counts.values())
    intervals: list[int] = []
    for url, dates in url_dates.items():
        if url_counts[url] < 2:
            continue
        ordered = sorted(dates)
        intervals.extend((current - previous).days for previous, current in zip(ordered, ordered[1:]))

    interval_histogram = Counter(str(interval) for interval in intervals)
    return {
        "total_items": len(items),
        "items_with_url": sum(url_counts.values()),
        "unique_urls": len(url_counts),
        "duplicate_urls": sum(1 for count in url_counts.values() if count > 1),
        "duplicate_occurrences": duplicate_occurrences,
        "duplicate_url_rate": _round(duplicate_occurrences / len(items)) if items else 0.0,
        "reappearance_intervals_days": {
            **_number_stats(intervals),
            "histogram": dict(sorted(interval_histogram.items(), key=lambda pair: int(pair[0]))),
        },
    }


def _is_youtube_url(value: Any) -> bool:
    try:
        hostname = (urlparse(str(value)).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "youtu.be" or hostname == "youtube.com" or hostname.endswith(".youtube.com")


def evidence_proxy(items: list[Item]) -> dict[str, Any]:
    """YouTube 아이템 중 타임스탬프가 있는 비율을 자막 근거 확보율로 본다.

    잡아내는 현상: 영상 원문 근거 없이 제목이나 설명만으로 요약을 생성하는 현상.
    경보 임계값: YouTube 타임스탬프 보유율이 0.70 미만이면 FAIL이다.
    """
    youtube_items = [item for item in items if _is_youtube_url(item.get("url"))]
    with_timestamp = sum(item.get("has_timestamp") is True for item in youtube_items)
    count = len(youtube_items)
    return {
        "youtube_items": count,
        "with_timestamp": with_timestamp,
        "timestamp_rate": _round(with_timestamp / count) if count else 0.0,
    }


def _dominant_value(counts: Counter[Any], total: int) -> dict[str, Any]:
    value, count = counts.most_common(1)[0] if counts else (None, 0)
    return {
        "dominant_value": value,
        "dominant_count": count,
        "fixed_ratio": _round(count / total) if total else 0.0,
        "distribution": {str(key): value for key, value in sorted(counts.items(), key=lambda pair: str(pair[0]))},
    }


def schema_rigidity(items: list[Item]) -> dict[str, Any]:
    """적용 아이디어 개수와 퀴즈 개수가 최빈 고정값에 몰린 비율을 계산한다.

    잡아내는 현상: 내용의 복잡도와 무관하게 정해진 개수만 채워 스키마를 기계적으로 완성하는 현상.
    경보 임계값: 두 필드 중 하나라도 최빈값 고정 비율이 0.95를 초과하면 FAIL이다.
    """
    idea_counts: Counter[int] = Counter()
    quiz_counts: Counter[Any] = Counter()
    combined: Counter[tuple[int, Any]] = Counter()
    for item in items:
        ideas = item.get("production_ideas")
        idea_count = len(ideas) if isinstance(ideas, list) else 0
        quiz_count = item.get("quiz_count")
        idea_counts[idea_count] += 1
        quiz_counts[quiz_count] += 1
        combined[(idea_count, quiz_count)] += 1

    combined_value, combined_count = combined.most_common(1)[0] if combined else ((None, None), 0)
    return {
        "total_items": len(items),
        "production_ideas_count": _dominant_value(idea_counts, len(items)),
        "quiz_count": _dominant_value(quiz_counts, len(items)),
        "combined_fixed_values": {
            "production_ideas_count": combined_value[0],
            "quiz_count": combined_value[1],
            "dominant_count": combined_count,
            "fixed_ratio": _round(combined_count / len(items)) if items else 0.0,
        },
    }


def _text_length(value: Any) -> int:
    return len(str(value).strip()) if value is not None else 0


def _list_text_stats(items: list[Item], field: str) -> dict[str, Any]:
    counts: list[int] = []
    per_entry_lengths: list[int] = []
    per_item_lengths: list[int] = []
    for item in items:
        raw = item.get(field)
        entries = raw if isinstance(raw, list) else []
        lengths = [_text_length(entry) for entry in entries]
        counts.append(len(entries))
        per_entry_lengths.extend(lengths)
        per_item_lengths.append(sum(lengths))
    return {
        "entries_per_item": _number_stats(counts),
        "characters_per_entry": _number_stats(per_entry_lengths),
        "characters_per_item": _number_stats(per_item_lengths),
    }


def summary_stats(items: list[Item]) -> dict[str, Any]:
    """한 줄 요약과 핵심포인트·적용아이디어 텍스트의 문자 길이를 요약한다.

    잡아내는 현상: 지나치게 짧고 상투적인 요약 또는 비정상적으로 장황한 산출물로의 급격한 변화.
    경보 임계값: 한 줄 요약 평균이 20자 미만이면 WARN이다(회귀 비교에서는 길이 급락도 확인).
    """
    return {
        "total_items": len(items),
        "one_line_summary": _number_stats(_text_length(item.get("one_line_summary")) for item in items),
        "key_points": _list_text_stats(items, "key_points"),
        "production_ideas": _list_text_stats(items, "production_ideas"),
    }
