"""계기판 권고가 코드로 확정 적용되는 쪽으로 새지 않는지 고정한다.

지시는 두 갈래로 적용된다. drop_sources·boost·suppress 는 코드가 확정 적용하고,
standing_note 는 프롬프트에 얹힌다. 자동 생성물이 앞쪽에 닿으면 소스가 통째로
빠지거나 점수가 흔들리는데, 원인은 어디에도 적히지 않는다. 검증에서 규칙 위반
모델이 실제로 arxiv 를 drop 시키는 것을 재현해, 계기판 줄을 LLM 경로에서 아예
빼는 쪽으로 고쳤다. 이 파일은 그 경계를 지킨다.
"""

from __future__ import annotations

import asyncio

import pytest

from app.directives import DASHBOARD_PREFIX, capture, interpret, load_raw


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def raw_path(tmp_path):
    return tmp_path / "directives.jsonl"


def test_dashboard_line_never_reaches_the_llm(raw_path):
    """모델이 규칙을 어길 수 있으므로, 어길 표면 자체를 없앤다."""
    seen = {}

    async def _call_llm(prompt, title, json_schema=None):
        seen["prompt"] = prompt
        return {"boost": [], "suppress": [], "drop_sources": [], "standing_note": "사람 지시", "difficulty": ""}

    capture("논문보다 실무 사례 위주로", path=raw_path)
    capture(f"{DASHBOARD_PREFIX} 한 주제로 쏠렸다. 분야를 섞어라.", path=raw_path)

    directive = _run(interpret(_call_llm, "", [], path=raw_path))

    assert "논문보다 실무 사례" in seen["prompt"]
    # 프롬프트 본문이 아니라 "사용자 메시지" 칸에 권고가 실리지 않았는지 본다.
    assert "분야를 섞어라" not in seen["prompt"], seen["prompt"]
    assert "분야를 섞어라" in directive.standing_note
    assert "사람 지시" in directive.standing_note


def test_dashboard_only_run_skips_the_llm_entirely(raw_path):
    """권고만 있는 주에는 LLM 을 부를 이유가 없다. 부르면 새는 경로가 생긴다."""

    async def _boom(*args, **kwargs):
        raise AssertionError("계기판 줄만 있는데 LLM 을 불렀다")

    capture(f"{DASHBOARD_PREFIX} 퀴즈 개수가 매번 같다. 개수를 달리하라.", path=raw_path)

    directive = _run(interpret(_boom, "", ["arxiv:cs.LG"], path=raw_path))

    assert "개수를 달리하라" in directive.standing_note
    assert not directive.drop_sources and not directive.boost and not directive.suppress


def test_rule_breaking_model_cannot_drop_sources_from_a_dashboard_line(raw_path):
    """규칙 위반 모델을 넣어도 계기판 줄만으로는 하드 필터가 켜지지 않는다."""

    async def _rule_breaker(prompt, title, json_schema=None):
        return {"boost": ["퀴즈"], "suppress": ["요약"], "drop_sources": ["arxiv:cs.LG"],
                "standing_note": "", "difficulty": ""}

    capture(f"{DASHBOARD_PREFIX} 관련도 점수가 몰렸다. 차이를 벌려라.", path=raw_path)

    directive = _run(interpret(_rule_breaker, "", ["arxiv:cs.LG"], path=raw_path))

    assert not directive.drop_sources, directive.drop_sources
    assert not directive.boost and not directive.suppress


def test_load_raw_can_see_past_the_recent_cap(raw_path):
    """중복 판정은 상한 밖의 살아 있는 원문도 봐야 한다."""
    capture(f"{DASHBOARD_PREFIX} 오래된 권고", path=raw_path)
    for i in range(35):
        capture(f"사람 지시 {i}", path=raw_path)

    capped = [r["text"] for r in load_raw(path=raw_path)]
    everything = [r["text"] for r in load_raw(path=raw_path, limit=None)]

    assert not any(t.startswith(DASHBOARD_PREFIX) for t in capped), "상한 안에서는 안 보인다"
    assert any(t.startswith(DASHBOARD_PREFIX) for t in everything)


def test_advice_survives_llm_failure(raw_path):
    """권고는 LLM 을 거치지 않는 경로다. 모델 장애에 같이 죽으면 안 된다."""

    async def _broken(*args, **kwargs):
        raise RuntimeError("provider down")

    capture("사람 지시", path=raw_path)
    capture(f"{DASHBOARD_PREFIX} 분야를 고르게 섞어라.", path=raw_path)

    directive = _run(interpret(_broken, "", [], path=raw_path))

    assert "분야를 고르게 섞어라" in directive.standing_note


def test_advice_is_seen_even_outside_the_recent_cap(raw_path):
    """해석은 최근 30건만 본다. 권고가 창 밖으로 밀리면 7일간 반영도 재기록도 없다."""

    async def _call_llm(prompt, title, json_schema=None):
        return {"boost": [], "suppress": [], "drop_sources": [], "standing_note": "사람 지시 해석", "difficulty": ""}

    capture(f"{DASHBOARD_PREFIX} 퀴즈 개수를 달리하라.", path=raw_path)
    for i in range(35):
        capture(f"사람 지시 {i}", path=raw_path)

    directive = _run(interpret(_call_llm, "", [], path=raw_path))

    assert "퀴즈 개수를 달리하라" in directive.standing_note


def test_merged_note_never_ends_mid_sentence(raw_path):
    """상한을 넘기면 문장을 중간에서 자르지 말고 통째로 버린다."""
    from app.directives import MAX_NOTE_CHARS, _merge_notes

    long_notes = [f"권고 문장 {i} 입니다. 내용에 맞게 개수를 달리하라." for i in range(12)]
    merged = _merge_notes("사람이 쓴 지시", long_notes)

    assert len(merged) <= MAX_NOTE_CHARS
    assert merged.startswith("사람이 쓴 지시")
    assert merged.endswith("."), merged
