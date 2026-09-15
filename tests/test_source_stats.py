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


# ──────────────────────────────────────────────
# 수집 0건 소스 — 굶는 소스보다 나쁜 상태
# ──────────────────────────────────────────────
#
# arXiv는 URL이 http라 301 리다이렉트에서 본문이 비어 40일간 한 건도 수집되지
# 않았다. 그런데 퍼널을 발송 아이템에서만 만들면 그 소스는 목록에 아예 없어
# "굶는 소스"로도 안 잡힌다 — 또 한 번 투명인간이다.

def test_expected_source_with_zero_collection_appears(tmp_path):
    funnel = build_funnel([_item("hn")], [_item("hn")], [_item("hn")],
                          expected=["arxiv:cs.LG", "hn"])

    assert funnel["arxiv:cs.LG"] == {"collected": 0, "candidates": 0, "delivered": 0}


def test_aggregate_reports_silent_sources(tmp_path):
    out = tmp_path / "s.jsonl"
    record([_item("hn")], [_item("hn")], [_item("hn")],
           date="2026-09-01", path=out, expected=["arxiv:cs.LG", "hn"])

    agg = aggregate(out)

    assert agg["silent_sources"] == ["arxiv:cs.LG"]
    assert agg["silent_count"] == 1
    # 수집이 0이면 "굶는" 것과는 다른 상태다 — 섞이면 처방을 틀린다.
    assert agg["starved_sources"] == []


def test_silent_is_separate_from_starved(tmp_path):
    """수집 0건(수집기 고장)과 수집되나 미발송(채점에서 밀림)은 처방이 다르다."""
    out = tmp_path / "s.jsonl"
    record(
        collected=[_item("arxiv:cs.LG")], candidates=[_item("arxiv:cs.LG")], delivered=[],
        date="2026-09-01", path=out, expected=["arxiv:cs.LG", "arxiv:cs.DB"],
    )

    agg = aggregate(out)

    assert agg["starved_sources"] == ["arxiv:cs.LG"]   # 수집됐는데 안 나감
    assert agg["silent_sources"] == ["arxiv:cs.DB"]    # 아예 안 수집됨


def test_expected_ignores_blank_keys():
    funnel = build_funnel([], [], [], expected=["", "  ", "hn"])

    assert set(funnel) == {"hn"}


# ──────────────────────────────────────────────
# 현재 설정 소스만 판정 - 설정에서 뺀 소스가 게이트를 막지 않는다
# ──────────────────────────────────────────────
#
# 2026-09-15 죽은 소스 6개를 설정에서 뺐는데, 판정은 전체 기록을 합산하므로
# 빠진 키가 과거 행에 남아 30일이 지나도 confirmed_silent로 잡혔다. 소스를
# 정리해도 게이트가 영영 FAIL이었다. 판정 대상은 가장 최근 날짜의 키로 한정한다.

def _days(start: int, count: int) -> list[str]:
    """2026-08-01에서 start일 뒤부터 count개의 날짜."""
    from datetime import date, timedelta

    base = date(2026, 8, 1)
    return [(base + timedelta(days=start + i)).isoformat() for i in range(count)]


def _write_rows(path, rows: list[tuple[str, dict[str, int]]]) -> None:
    """(날짜, {키: collected}) 목록을 기록 형식으로 쓴다. candidates·delivered는 0."""
    with path.open("a", encoding="utf-8") as f:
        for day, counts in rows:
            sources = {k: {"collected": c, "candidates": 0, "delivered": 0} for k, c in counts.items()}
            f.write(json.dumps({"date": day, "sources": sources}, ensure_ascii=False) + "\n")


def test_retired_zero_key_is_not_confirmed_silent(tmp_path):
    """과거 행에만 있고 최근 행에 없는 0건 키는 confirmed_silent가 아니다."""
    out = tmp_path / "s.jsonl"
    old = {"hackernews": 3, "https://dataelixir.com/issues.rss": 0}
    new = {"hackernews": 3}
    _write_rows(out, [(d, old) for d in _days(0, 20)] + [(d, new) for d in _days(20, 15)])

    agg = aggregate(out)

    assert agg["days"] == 35
    assert agg["enough_days_for_silence"] is True
    assert agg["confirmed_silent_families"] == []
    assert agg["confirmed_silent_count"] == 0
    assert agg["silent_sources"] == []
    assert agg["silent_family_count"] == 0
    # 판정에는 안 넣지만 리포트에서는 보여야 한다.
    assert agg["retired_sources"] == ["https://dataelixir.com/issues.rss"]
    assert agg["retired_families"] == ["https://dataelixir.com/issues.rss"]
    # 누적 합계(기록)는 지우지 않는다.
    assert "https://dataelixir.com/issues.rss" in agg["sources"]


def test_current_zero_key_still_confirmed_after_30_days(tmp_path):
    """게이트가 무뎌지지 않았는가: 최근 행에 있는 0건 키는 30일 이상이면 여전히 FAIL 대상이다."""
    out = tmp_path / "s.jsonl"
    old = {"hackernews": 3, "arxiv:cs.LG": 0, "retired_feed": 0}
    new = {"hackernews": 3, "arxiv:cs.LG": 0}
    _write_rows(out, [(d, old) for d in _days(0, 25)] + [(d, new) for d in _days(25, 10)])

    agg = aggregate(out)

    assert agg["confirmed_silent_families"] == ["arxiv"]
    assert agg["confirmed_silent_count"] == 1
    assert agg["silent_sources"] == ["arxiv:cs.LG"]
    assert agg["retired_sources"] == ["retired_feed"]


def test_current_keys_are_union_of_all_rows_on_latest_date(tmp_path):
    """같은 날짜에 행이 여러 개면 그 날짜의 모든 행 키를 합친다(마지막 행 하나가 아니다).

    수동 재실행이 소스 일부만 설정된 채 기록을 남겨도, 진짜 설정된 죽은 소스가
    판정에서 빠지면 안 된다. 최근 날짜는 파일 순서가 아니라 date 최댓값으로 고른다.
    """
    out = tmp_path / "s.jsonl"
    full = {"hackernews": 3, "dead_feed": 0, "retired_feed": 0}
    rows = [(d, full) for d in _days(0, 34)]
    last = _days(34, 1)[0]
    rows.append((last, {"hackernews": 3, "dead_feed": 0}))  # 정기 실행(정리된 설정)
    rows.append((last, {"hackernews": 3}))                  # 같은 날 부분 설정 재실행
    rows.append((_days(10, 1)[0], full))                    # 뒤늦게 붙은 과거 날짜 행
    _write_rows(out, rows)

    agg = aggregate(out)

    assert agg["days"] == 35
    assert agg["confirmed_silent_families"] == ["dead_feed"]
    assert agg["retired_sources"] == ["retired_feed"]


def test_new_key_zero_on_first_day_is_confirmed_immediately(tmp_path):
    """새로 추가된 키의 동작을 고정한다(기존 규칙 그대로).

    days는 전체 기록 기준이라, 기록이 이미 30일 이상이면 새 키가 추가 첫날
    수집 0건이어도 곧바로 confirmed_silent다. 첫날 수집이 되면(YouTube 채널은
    시간 필터 없이 최근 10건) 판정 대상이 아니다. 빠진 키는 판정에서 사라진다.
    """
    out = tmp_path / "s.jsonl"
    old = {"hackernews": 3, "UC-retired": 0}
    new = {"hackernews": 3, "UCnew-zero": 0, "UCnew-ok": 10}
    _write_rows(out, [(d, old) for d in _days(0, 34)] + [(_days(34, 1)[0], new)])

    agg = aggregate(out)

    assert agg["confirmed_silent_families"] == ["UCnew-zero"]
    assert agg["retired_sources"] == ["UC-retired"]


def test_family_partially_retired_is_judged_on_remaining_keys(tmp_path):
    """arXiv처럼 접히는 계열에서 일부 키만 남으면, 남은 키만으로 계열을 판정한다.

    빠진 카테고리가 과거에 수집한 건수가 계열 합계를 채워 "살아 있음"으로
    가리면 안 된다. 계열은 남은 키가 하나라도 있으면 retired가 아니다.
    """
    out = tmp_path / "s.jsonl"
    old = {"hackernews": 3, "arxiv:cs.LG": 0, "arxiv:cs.SI": 2, "arxiv:cs.DB": 0}
    new = {"hackernews": 3, "arxiv:cs.LG": 0}
    _write_rows(out, [(d, old) for d in _days(0, 30)] + [(d, new) for d in _days(30, 5)])

    agg = aggregate(out)

    assert agg["confirmed_silent_families"] == ["arxiv"]
    assert agg["silent_sources"] == ["arxiv:cs.LG"]
    assert agg["retired_sources"] == ["arxiv:cs.DB", "arxiv:cs.SI"]
    assert agg["retired_families"] == []
    # 빠진 cs.SI는 수집됐지만 한 번도 안 나갔다. 현재 설정이 아니므로 starved도 아니다.
    assert agg["starved_sources"] == ["hackernews"]
    assert agg["starved_families"] == ["hackernews"]


def test_retired_filter_respects_since(tmp_path):
    """since는 하한만 거른다. 걸러진 구간 안에서도 최근 날짜 키가 현재 설정이다."""
    out = tmp_path / "s.jsonl"
    old = {"hackernews": 3, "retired_feed": 0}
    new = {"hackernews": 3, "dead_feed": 0}
    _write_rows(out, [(d, old) for d in _days(0, 40)] + [(d, new) for d in _days(40, 2)])

    agg = aggregate(out, since=_days(5, 1)[0])

    assert agg["days"] == 37
    # dead_feed는 2일치뿐이지만 days는 전체(since 이후) 기록 기준이라 confirmed다.
    assert agg["confirmed_silent_families"] == ["dead_feed"]
    assert agg["retired_sources"] == ["retired_feed"]
    # since 뒤에 행이 없으면 현재 키도 없어 판정이 빈다.
    empty = aggregate(out, since="2099-01-01")
    assert empty["confirmed_silent_count"] == 0
    assert empty["retired_sources"] == []
