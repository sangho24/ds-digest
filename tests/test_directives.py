"""자연어 지시 — 봇에게 한국말로 말하면 다음 날 큐레이션이 바뀐다.

예전엔 `/keyword`로 시작하지 않는 자유 텍스트를 버리고 acknowledge까지 해서
텔레그램 서버에서도 지웠다. "논문 말고 실무 사례 위주로" 같은 말이 흔적 없이
증발했다.

실행: pytest tests/test_directives.py -v
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.directives import (
    DEFAULT_TTL_DAYS,
    DIRECTIVE_WEIGHT,
    KST,
    MAX_NOTE_CHARS,
    Directive,
    capture,
    directive_score,
    filter_sources,
    interpret,
    load_raw,
    parse_directive,
)
from app.models import RawContent, SourceType

NOW = datetime(2026, 8, 30, 7, 10, tzinfo=KST)


# ──────────────────────────────────────────────
# 수집 · 만료
# ──────────────────────────────────────────────

def test_capture_writes_entry(tmp_path):
    out = tmp_path / "d.jsonl"

    entry = capture("논문 말고 실무 사례 위주로", path=out, now=NOW)

    assert entry["text"] == "논문 말고 실무 사례 위주로"
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_capture_ignores_blank(tmp_path):
    out = tmp_path / "d.jsonl"

    assert capture("   ", path=out) is None
    assert not out.exists()


def test_load_raw_drops_expired(tmp_path):
    """3개월 전 지시가 영원히 사는 것이 이 시스템에서 제일 위험하다."""
    out = tmp_path / "d.jsonl"
    capture("오래된 지시", path=out, now=NOW - timedelta(days=DEFAULT_TTL_DAYS + 1))
    capture("최근 지시", path=out, now=NOW)

    rows = load_raw(out, now=NOW)

    assert [r["text"] for r in rows] == ["최근 지시"]


def test_load_raw_keeps_order(tmp_path):
    """나중 지시가 앞선 지시를 뒤집으므로 순서가 의미를 가진다."""
    out = tmp_path / "d.jsonl"
    capture("첫째", path=out, now=NOW)
    capture("둘째", path=out, now=NOW)

    assert [r["text"] for r in load_raw(out, now=NOW)] == ["첫째", "둘째"]


def test_load_raw_skips_broken_line(tmp_path):
    out = tmp_path / "d.jsonl"
    capture("정상", path=out, now=NOW)
    with out.open("a", encoding="utf-8") as f:
        f.write("{ 깨짐\n")

    assert len(load_raw(out, now=NOW)) == 1


def test_load_raw_keeps_entry_with_unreadable_expiry(tmp_path):
    """만료 시각을 못 읽는다고 지시를 조용히 잃으면 안 된다."""
    out = tmp_path / "d.jsonl"
    out.write_text(json.dumps({"text": "지시", "expires_at": "이상한값"}) + "\n", encoding="utf-8")

    assert len(load_raw(out, now=NOW)) == 1


def test_load_raw_missing_file(tmp_path):
    assert load_raw(tmp_path / "없음.jsonl") == []


# ──────────────────────────────────────────────
# 파싱 — drop_sources는 하드 필터라 방어가 중요하다
# ──────────────────────────────────────────────

def test_parse_directive_basic():
    d = parse_directive(
        {
            "boost": ["인과추론"],
            "suppress": ["쿠버네티스"],
            "drop_sources": ["arxiv"],
            "standing_note": "실무 사례 우선",
        },
        known_sources=["arxiv", "hackernews"],
    )

    assert d.boost == {"인과추론"}
    assert d.suppress == {"쿠버네티스"}
    assert d.drop_sources == {"arxiv"}
    assert d.standing_note == "실무 사례 우선"


def test_parse_directive_ignores_unknown_source():
    """모델이 없는 출처를 지어내도 하드 필터가 걸리면 안 된다."""
    d = parse_directive({"drop_sources": ["존재하지않는소스"]}, known_sources=["arxiv"])

    assert d.drop_sources == set()


def test_parse_directive_matches_source_case_insensitively():
    d = parse_directive({"drop_sources": ["ARXIV"]}, known_sources=["arxiv"])

    assert d.drop_sources == {"arxiv"}


def test_parse_directive_truncates_note():
    d = parse_directive({"standing_note": "가" * 1000}, known_sources=[])

    assert len(d.standing_note) == MAX_NOTE_CHARS


def test_parse_directive_handles_garbage():
    d = parse_directive({"boost": "리스트가 아님", "standing_note": None}, known_sources=[])

    assert d.is_empty()


def test_directive_describe_is_human_readable():
    d = Directive(boost={"인과추론"}, drop_sources={"arxiv"}, standing_note="짧게")

    text = d.describe()

    assert "인과추론" in text and "arxiv" in text and "짧게" in text


# ──────────────────────────────────────────────
# 해석
# ──────────────────────────────────────────────

def _raw(url: str, source_key: str) -> RawContent:
    return RawContent(
        source_type=SourceType.RSS,
        source_name=source_key,
        source_key=source_key,
        title="제목",
        url=url,
        body="본문",
    )


def test_interpret_skips_llm_when_no_messages(tmp_path):
    called = False

    async def _call(*_a, **_k):
        nonlocal called
        called = True
        return {}

    result = asyncio.run(interpret(_call, "어휘", ["arxiv"], path=tmp_path / "d.jsonl"))

    assert result.is_empty()
    assert called is False


def test_interpret_returns_empty_when_llm_fails(tmp_path):
    """해석이 실패해도 다이제스트는 나가야 한다."""
    out = tmp_path / "d.jsonl"
    capture("arxiv 그만", path=out, now=NOW)

    async def _boom(*_a, **_k):
        raise RuntimeError("llm down")

    result = asyncio.run(interpret(_boom, "어휘", ["arxiv"], path=out, now=NOW))

    assert result.is_empty()


def test_interpret_passes_messages_in_order(tmp_path):
    out = tmp_path / "d.jsonl"
    capture("arxiv 그만", path=out, now=NOW)
    capture("역시 arxiv 살려", path=out, now=NOW)
    seen = {}

    async def _call(prompt, _title, **_k):
        seen["prompt"] = prompt
        return {"boost": [], "suppress": [], "drop_sources": [], "standing_note": ""}

    asyncio.run(interpret(_call, "어휘", ["arxiv"], path=out, now=NOW))

    body = seen["prompt"]
    assert body.index("arxiv 그만") < body.index("역시 arxiv 살려")
    assert "arxiv" in body


# ──────────────────────────────────────────────
# 적용 — 하드 필터
# ──────────────────────────────────────────────

def test_filter_sources_removes_matching():
    items = [_raw("https://a/1", "arxiv"), _raw("https://a/2", "hackernews")]

    kept = filter_sources(items, Directive(drop_sources={"arxiv"}))

    assert [i.source_key for i in kept] == ["hackernews"]


def test_filter_sources_is_noop_without_directive():
    items = [_raw("https://a/1", "arxiv")]

    assert filter_sources(items, Directive()) == items


def test_filter_sources_refuses_to_empty_the_digest():
    """지시 한 줄이 빈 다이제스트를 만들면 안 된다 — 상대 랭킹 설계의 목적이 그거였다."""
    items = [_raw("https://a/1", "arxiv"), _raw("https://a/2", "arxiv")]

    kept = filter_sources(items, Directive(drop_sources={"arxiv"}))

    assert kept == items


# ──────────────────────────────────────────────
# 적용 — 타이브레이크
# ──────────────────────────────────────────────

def test_directive_score_boost_and_suppress():
    boost = Directive(boost={"인과추론"})
    suppress = Directive(suppress={"쿠버네티스"})

    assert directive_score(boost, [], ["인과추론"]) == DIRECTIVE_WEIGHT
    assert directive_score(suppress, ["Kubernetes"], []) == 0  # 태그 표기가 다르면 안 걸림
    assert directive_score(suppress, ["쿠버네티스"], []) == -DIRECTIVE_WEIGHT


def test_directive_score_matches_partial_terms():
    """사용자는 '쿠버네티스 운영'이라 말하고 개념은 '쿠버네티스'일 수 있다."""
    d = Directive(suppress={"쿠버네티스"})

    assert directive_score(d, [], ["쿠버네티스 운영"]) == -DIRECTIVE_WEIGHT


def test_directive_score_counts_each_side_once():
    """지시어가 여러 개 걸려도 한 번만 센다 — 태그 수로 점수가 부풀면 안 된다."""
    d = Directive(boost={"a", "b", "c"})

    assert directive_score(d, ["a", "b"], ["c"]) == DIRECTIVE_WEIGHT


def test_directive_score_outweighs_preference():
    """명시적 지시(±3)가 추론된 취향(±2)보다 무거워야 한다."""
    from app.preferences import PreferenceSignal, preference_score

    signal = PreferenceSignal(liked_concepts={"쿠버네티스"})
    liked = preference_score(signal, None, [], ["쿠버네티스"])
    suppressed = directive_score(Directive(suppress={"쿠버네티스"}), [], ["쿠버네티스"])

    assert liked + suppressed < 0


def test_directive_score_empty_is_zero():
    assert directive_score(Directive(), ["a"], ["b"]) == 0
