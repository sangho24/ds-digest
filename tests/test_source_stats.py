"""소스 퍼널 기록 — 지표의 사각지대를 메운다.

`source_reach`는 소스 목록을 **발송된 아이템**에서 만든다. 그래서 한 번도
발송되지 않은 소스는 목록에 없고 "장기 미등장"으로 잡히지도 않는다.
실측: arXiv가 40일간 발송 0건인데 지표는 "소스 18개 / 미등장 1개"로 정상 보고했다.

실행: pytest tests/test_source_stats.py -v
"""

from __future__ import annotations

import json

import pytest

from app.models import RawContent, SourceType
from app.source_stats import aggregate, build_funnel, load, record


def _item(source_key: str, n: int = 0) -> RawContent:
    return RawContent(
        source_type=SourceType.RSS,
        source_name=source_key,
        source_key=source_key,
        title=f"{source_key}-{n}",
        url=f"https://example.com/{source_key}/{n}",
    )


# ──────────────────────────────────────────────
# 퍼널 구성
# ──────────────────────────────────────────────

def test_build_funnel_counts_three_stages():
    collected = [_item("arxiv", 0), _item("arxiv", 1), _item("hn", 0)]
    candidates = [_item("arxiv", 0), _item("hn", 0)]
    delivered = [_item("hn", 0)]

    funnel = build_funnel(collected, candidates, delivered)

    assert funnel["arxiv"] == {"collected": 2, "candidates": 1, "delivered": 0}
    assert funnel["hn"] == {"collected": 1, "candidates": 1, "delivered": 1}


def test_build_funnel_includes_source_seen_only_at_collection():
    """수집만 되고 후보에도 못 든 소스가 목록에서 사라지면 안 된다 — 그게 핵심이다."""
    funnel = build_funnel([_item("dead_feed")], [], [])

    assert funnel["dead_feed"] == {"collected": 1, "candidates": 0, "delivered": 0}


def test_build_funnel_falls_back_to_source_name():
    item = RawContent(source_type=SourceType.RSS, source_name="이름만",
                      title="t", url="https://e.com/1")
    # source_key는 validator가 source_name으로 채운다.
    assert "이름만" in build_funnel([item], [], [])


# ──────────────────────────────────────────────
# 기록 · 읽기
# ──────────────────────────────────────────────

def test_record_appends_one_line_per_run(tmp_path):
    out = tmp_path / "s.jsonl"

    record([_item("hn")], [_item("hn")], [_item("hn")], date="2026-09-01", path=out)
    record([_item("hn")], [_item("hn")], [], date="2026-09-02", path=out)

    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert [r["date"] for r in load(out)] == ["2026-09-01", "2026-09-02"]


def test_load_skips_broken_line(tmp_path):
    out = tmp_path / "s.jsonl"
    record([_item("hn")], [], [], date="2026-09-01", path=out)
    with out.open("a", encoding="utf-8") as f:
        f.write("{ 깨짐\n")

    assert len(load(out)) == 1


def test_load_since_filters(tmp_path):
    out = tmp_path / "s.jsonl"
    record([_item("hn")], [], [], date="2026-08-01", path=out)
    record([_item("hn")], [], [], date="2026-09-01", path=out)

    assert [r["date"] for r in load(out, since="2026-09-01")] == ["2026-09-01"]


def test_load_missing_file(tmp_path):
    assert load(tmp_path / "없음.jsonl") == []


# ──────────────────────────────────────────────
# 집계 — 사각지대가 드러나는가
# ──────────────────────────────────────────────

def test_aggregate_reveals_starved_source(tmp_path):
    """이게 이 모듈의 존재 이유다: 수집되는데 한 번도 안 나가는 소스를 이름으로 잡는다."""
    out = tmp_path / "s.jsonl"
    for day in ("2026-09-01", "2026-09-02"):
        record(
            collected=[_item("arxiv", 0), _item("arxiv", 1), _item("hn", 0)],
            candidates=[_item("arxiv", 0), _item("hn", 0)],
            delivered=[_item("hn", 0)],
            date=day, path=out,
        )

    agg = aggregate(out)

    assert agg["starved_sources"] == ["arxiv"]
    assert agg["starved_count"] == 1
    assert agg["sources"]["arxiv"]["collected"] == 4
    assert agg["sources"]["arxiv"]["reach"] == 0.0
    assert agg["sources"]["hn"]["reach"] == 1.0
    assert agg["days"] == 2


def test_aggregate_reach_is_delivered_over_collected(tmp_path):
    """v2 §3.6이 정의한 그 비율 — 분모가 이제 실제로 존재한다."""
    out = tmp_path / "s.jsonl"
    record([_item("hn", i) for i in range(4)], [_item("hn", 0)], [_item("hn", 0)],
           date="2026-09-01", path=out)

    assert aggregate(out)["sources"]["hn"]["reach"] == 0.25


def test_aggregate_empty_when_no_records(tmp_path):
    """퍼널 도입 전 구간에는 기록이 없다. 없는 데이터로 경보를 울리면 안 된다."""
    agg = aggregate(tmp_path / "없음.jsonl")

    assert agg["starved_count"] == 0
    assert agg["source_count"] == 0


def test_aggregate_source_delivered_once_is_not_starved(tmp_path):
    """한 번이라도 나갔으면 굶은 게 아니다 — 그건 source_reach의 미등장 지표가 본다."""
    out = tmp_path / "s.jsonl"
    record([_item("a")], [_item("a")], [_item("a")], date="2026-09-01", path=out)
    record([_item("a")], [_item("a")], [], date="2026-09-02", path=out)

    assert aggregate(out)["starved_sources"] == []
