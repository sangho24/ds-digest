"""source_key / source_label 정체성 채움과 그룹핑 폴백 검증.

source_name(자유 표시명)만으로 그룹핑하면 채널/피드가 이름을 바꿀 때 소스가
갈라져 계측·캡이 어긋난다. 불변 식별자 source_key를 도입하되, 기존 독자와
과거 데이터(source_key 없음)를 깨지 않도록 source_name 폴백을 유지한다.

실행: pytest tests/test_source_identity.py -v
"""
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
