"""source_key / source_label 정체성 채움과 그룹핑 폴백 검증.

source_name(자유 표시명)만으로 그룹핑하면 채널/피드가 이름을 바꿀 때 소스가
갈라져 계측·캡이 어긋난다. 불변 식별자 source_key를 도입하되, 기존 독자와
과거 데이터(source_key 없음)를 깨지 않도록 source_name 폴백을 유지한다.

실행: pytest tests/test_source_identity.py -v
"""
from datetime import date

from app.models import RawContent, SourceType


def test_backfill_from_source_name_when_empty():
    """source_key/source_label를 안 주면 source_name으로 폴백 채운다(하위호환)."""
    item = RawContent(
        source_type=SourceType.RSS,
        source_name="토스 기술블로그",
        title="t",
        url="https://toss.tech/article/1",
    )
    assert item.source_label == "토스 기술블로그"
    assert item.source_key == "토스 기술블로그"


def test_explicit_source_key_and_label_preserved():
    """명시된 source_key/source_label는 그대로 보존되고 source_name도 남는다."""
    item = RawContent(
        source_type=SourceType.YOUTUBE,
        source_name="채널명",
        source_key="UC123",
        source_label="채널 표시명",
        title="t",
        url="https://youtu.be/x",
    )
    assert item.source_key == "UC123"
    assert item.source_label == "채널 표시명"
    assert item.source_name == "채널명"


def test_key_derives_from_label_when_only_label_given():
    """source_label만 주면 source_key는 label에서 파생된다."""
    item = RawContent(
        source_type=SourceType.RSS,
        source_name="name",
        source_label="라벨",
        title="t",
        url="https://example.com/x",
    )
    assert item.source_label == "라벨"
    assert item.source_key == "라벨"


def test_cap_per_channel_groups_by_source_key():
    """표시명이 달라도 같은 source_key면 한 버킷으로 묶여 캡이 걸린다."""
    from app.jobs.daily_digest import _cap_per_channel

    items = [
        RawContent(
            source_type=SourceType.YOUTUBE,
            source_name=f"채널 표시명 v{i}",  # 표시명이 매번 다름
            source_key="UC1",                  # 그러나 불변 키는 동일
            title=f"v{i}",
            url=f"https://youtu.be/{i}",
        )
        for i in range(4)
    ]

    capped = _cap_per_channel(items, 2)

    assert len(capped) == 2


def test_source_reach_prefers_source_key_over_name():
    """source_key가 있으면 그것으로 묶고, source_name이 달라도 한 소스로 센다."""
    from evals.metrics import source_reach

    items = [
        {"date": "2026-01-01", "source_key": "k1", "source_name": "표시1", "url": "u1"},
        {"date": "2026-01-02", "source_key": "k1", "source_name": "표시2", "url": "u2"},
    ]

    result = source_reach(items, window_days=30)

    assert result["source_count"] == 1
    assert "k1" in result["sources"]


def test_source_reach_falls_back_to_source_name_when_no_key():
    """과거 데이터엔 source_key가 없어 source_name으로 폴백해야 한다."""
    from evals.metrics import source_reach

    items = [{"date": "2026-01-01", "source_name": "old", "url": "u1"}]

    result = source_reach(items, window_days=30)

    assert "old" in result["sources"]


# --- source_reach 의 퇴역 소스 분리 (§50) ---------------------------------------
# 설정에서 뺀 소스는 정본에 과거 아이템이 남아 있는 한 "미등장 일수"가 커지기만 한다.
# current_keys 를 주면 그 소스는 stale 이 아니라 retired 로 따로 보여야 한다.

_REACH_ITEMS = [
    {"date": "2026-01-01", "source_key": "retired", "url": "u1"},   # 설정에서 뺀 소스, 오래됨
    {"date": "2026-01-01", "source_key": "dead", "url": "u2"},      # 설정에 남아 있는데 오래됨
    {"date": "2026-03-01", "source_key": "alive", "url": "u3"},     # 최근 등장
]
_REFERENCE = date(2026, 3, 2)


def test_source_reach_without_current_keys_counts_retired_as_stale():
    """current_keys 를 안 주면 기존 동작: 퇴역 소스도 stale 로 세고 retired 키는 없다."""
    from evals.metrics import source_reach

    result = source_reach(_REACH_ITEMS, window_days=30, reference_date=_REFERENCE)

    assert result["stale_source_count"] == 2
    assert [s["source_name"] for s in result["stale_sources"]] == ["dead", "retired"]
    assert "retired_sources" not in result
    assert "retired_source_count" not in result


def test_source_reach_with_current_keys_moves_retired_out_of_stale():
    """current_keys 에 없는 오래된 소스는 retired 로, 있는 오래된 소스는 여전히 stale."""
    from evals.metrics import source_reach

    result = source_reach(
        _REACH_ITEMS, window_days=30, reference_date=_REFERENCE, current_keys={"dead", "alive"}
    )

    assert result["stale_source_count"] == 1
    assert [s["source_name"] for s in result["stale_sources"]] == ["dead"]
    assert result["retired_source_count"] == 1
    assert result["retired_sources"] == [
        {"source_name": "retired", "item_count": 1, "last_seen": "2026-01-01", "days_since_last_seen": 60}
    ]
    # 가시성: sources 와 source_count 는 퇴역 소스까지 전부 담는다.
    assert result["source_count"] == 3
    assert set(result["sources"]) == {"retired", "dead", "alive"}


def test_source_reach_current_keys_only_narrows_stale_not_recent():
    """현재 설정에 없어도 최근 등장한 소스는 retired 에 들어가지 않는다(판정 대상이 아니니까)."""
    from evals.metrics import source_reach

    result = source_reach(
        _REACH_ITEMS, window_days=30, reference_date=_REFERENCE, current_keys={"dead"}
    )

    assert [s["source_name"] for s in result["stale_sources"]] == ["dead"]
    assert [s["source_name"] for s in result["retired_sources"]] == ["retired"]


def test_source_reach_empty_current_keys_behaves_like_none():
    """빈 set 은 None 과 같다. 판정을 비우면 모든 소스가 retired 가 되어 게이트가 꺼진다."""
    from evals.metrics import source_reach

    with_none = source_reach(_REACH_ITEMS, window_days=30, reference_date=_REFERENCE)
    with_empty = source_reach(_REACH_ITEMS, window_days=30, reference_date=_REFERENCE, current_keys=set())

    assert with_empty == with_none
    assert with_empty["stale_source_count"] == 2


def test_current_keys_returns_none_when_app_package_unavailable(monkeypatch):
    """structlog 없는 시스템 파이썬처럼 app.source_stats import 가 실패하면 None."""
    import sys

    from evals import source_funnel as funnel_module

    # sys.modules 에 None 을 두면 import 문이 ImportError 를 낸다.
    monkeypatch.setitem(sys.modules, "app.source_stats", None)

    assert funnel_module.current_keys() is None


def test_current_keys_returns_none_when_records_empty(monkeypatch):
    """기록이 비어 있으면(퍼널 도입 전 구간) None. 빈 set 이 아니다."""
    from app import source_stats
    from evals import source_funnel as funnel_module

    monkeypatch.setattr(source_stats, "load", lambda path=None, since=None: [])

    assert funnel_module.current_keys() is None


def test_current_keys_reads_latest_day_from_records(tmp_path, monkeypatch):
    """기록이 있으면 가장 최근 날짜의 키 합집합을 돌려준다."""
    import json

    from app import source_stats
    from evals import source_funnel as funnel_module

    stats = tmp_path / "source_stats.jsonl"
    stats.write_text(
        json.dumps({"date": "2026-01-01", "sources": {"old": {}, "kept": {}}}) + "\n"
        + json.dumps({"date": "2026-01-02", "sources": {"kept": {}, "new": {}}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(source_stats, "STATS_PATH", stats)

    assert funnel_module.current_keys() == {"kept", "new"}


def test_calculate_metrics_passes_current_keys_to_source_reach(monkeypatch):
    """실행기가 current_keys() 결과를 실제로 source_reach 에 넘긴다."""
    import evals.run as evals_run

    monkeypatch.setattr(evals_run, "current_keys", lambda: {"dead", "alive"})
    monkeypatch.setattr(evals_run, "date", _FixedDate)

    reach = evals_run.calculate_metrics(_REACH_ITEMS)["source_reach"]

    assert reach["reference_date"] == _REFERENCE.isoformat()
    assert [s["source_name"] for s in reach["stale_sources"]] == ["dead"]
    assert [s["source_name"] for s in reach["retired_sources"]] == ["retired"]


class _FixedDate(date):
    """evals.run 의 date.today() 를 고정한다."""

    @classmethod
    def today(cls) -> "_FixedDate":
        return cls(_REFERENCE.year, _REFERENCE.month, _REFERENCE.day)
