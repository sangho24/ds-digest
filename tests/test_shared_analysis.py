"""공용 분석과 개인 선정의 분리 (PROGRESS §58).

분석(요약, 점수, 퀴즈)은 하루 한 번 만들어 모든 수신자가 공유하고, 사람마다 다른 것은
선정뿐이다. 그래서 LLM 프롬프트에는 시스템 권고(품질 계기판)만 들어가고, 운영자 개인의
지시·키워드·퀴즈 기록은 select_for_user 에서만 쓰여야 한다.

여기서 고정하는 것:
    1. 개인 지시가 없는 날(품질 계기판 줄만 있는 날)의 프롬프트와 선정 결과는 분리
       전과 한 글자도 다르지 않다. 아래 해시와 선정 목록은 분리 전 코드로 뽑은 값이다.
    2. 개인 지시·키워드가 있어도 프롬프트는 1과 같다(개인 신호가 새지 않는다).
    3. drop_sources, depth_bias 는 풀을 바꾸지 않고 그 사용자의 선정만 바꾼다.
    4. 운영자 개인 기록(퀴즈 약점, 키워드)은 user_id 가 default 일 때만 선정에 쓰인다.

실제 LLM 은 호출하지 않는다. pytest-asyncio 가 없으므로 asyncio.run() 으로 돈다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import app.analyzer as analyzer
from app.directives import DASHBOARD_PREFIX, KST, Directive, interpret
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    RawContent,
    SourceType,
    UserProfile,
)
from app.pool import ScoredCandidate, candidates_from_entries

DASHBOARD_NOTE = "퀴즈 개수가 매번 똑같다. 내용에 맞게 개수를 달리하라."

# (제목, source_key, actionability, depth, 상대 평가). 동점과 같은 계열 다수를 섞었다.
_SPECS: list[tuple[str, str, int, int, int]] = [
    ("hn-a", "hackernews", 8, 4, 7),
    ("hn-b", "hackernews", 7, 5, 7),
    ("arxiv-a", "arxiv:cs.LG", 4, 9, 6),
    ("arxiv-b", "arxiv:stat.ML", 5, 8, 8),
    ("blog-a", "https://toss.tech/rss.xml", 8, 3, 5),
    ("blog-b", "https://d2.naver.com/d2.atom", 6, 6, 4),
    ("thin-a", "hackernews", 3, 1, 2),
]

# 분리 전 코드(커밋 9d4bfc5 + 작업 트리)로 같은 시나리오를 돌려 얻은 값.
GOLDEN_PROMPT_SHA256 = {
    "youtube-metadata-ranking": "c069da31896b538f2283b912d9e18453fd3217d554f7ff8f8b439652b02fb7f8",
    "analysis": "bd5e8f8853603d09d453cc9fd371e2d35d7ed3dda268c49984032b6b249cef53",
    "relative-rating": "c057f79f40392f5a1e0215b7bad1b6a24c500fc6f00834017bdeb71cd9d2ebad",
}
GOLDEN_SELECTED: list[tuple[str, int]] = [("hn-a", 7), ("arxiv-b", 7), ("hn-b", 7)]
# 지시가 아예 없는 날(directive None)의 같은 값.
GOLDEN_PROMPT_SHA256_NO_DIRECTIVE = {
    "youtube-metadata-ranking": "5fa3b7f1e4eb4cca85f41b37536db5850f773c6478753f631895e3edf96f9ec3",
    "analysis": "f8ede2a3c96d180e7d9e39ff79ba03e7885c46acb4b64c9727a697dd9e9f2c48",
    "relative-rating": "9428860feb6ad099d5e8816006cd09daacb549ab025d011b5901e22d04e0a5b7",
}
# 분리 전 코드가 운영자 지시별로 고른 목록(제목). 그때는 분석 단계에서 적용했다.
OLD_SELECTED_HARDER = ["arxiv-b", "hn-a", "hn-b"]
OLD_SELECTED_EASIER = ["hn-a", "arxiv-b", "blog-a"]
OLD_SELECTED_DROP_HACKERNEWS = ["arxiv-b", "blog-a", "blog-b"]
# 분리 전 "더 어렵게" 런에서 arxiv-b 에 표시되던 점수(배합이 점수에 직접 걸렸다).
OLD_HARDER_SCORE_ARXIV_B = 8


async def _noop_sleep(_seconds: float) -> None:
    return None


def _raw(title: str, source_key: str) -> RawContent:
    return RawContent(
        source_type=SourceType.RSS,
        source_name=source_key,
        source_key=source_key,
        title=title,
        url=f"https://example.com/{title}",
        body="본문 원문 " * 250,
    )


def _youtube(n: int) -> RawContent:
    return RawContent(
        source_type=SourceType.YOUTUBE,
        source_name=f"채널 {n}",
        source_key=f"youtube:ch{n}",
        title=f"영상 {n}",
        url=f"https://youtu.be/vid{n}",
        body=f"설명글 {n}",
    )


def _settings(max_items: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False,
        groq_api_key="test",
        gemini_api_key="",
        groq_ranking_model="ranking-model",
        relevance_floor=4,
        max_items_per_digest=max_items,
        max_items_per_source=2,
        source_diversity_strength=1.0,
        supabase_url="",
        supabase_key="",
    )


def _write_directives(path: Path, texts: list[str]) -> datetime:
    """지시 원문 파일을 만들고, 전부 살아 있는 기준 시각을 돌려준다."""
    now = datetime(2026, 11, 16, 9, 0, tzinfo=KST)
    with path.open("w", encoding="utf-8") as file:
        for text in texts:
            row = {
                "text": text,
                "received_at": now.isoformat(),
                "expires_at": (now + timedelta(days=7)).isoformat(),
            }
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    return now


def _interpret(tmp_path: Path, texts: list[str], response: dict[str, Any] | None = None) -> Directive:
    path = tmp_path / "directives.jsonl"
    now = _write_directives(path, texts)

    async def fake_interpreter(prompt: str, title: str, **_kwargs: Any) -> dict[str, Any]:
        assert response is not None, "사람 지시가 없으면 해석 LLM 을 부르면 안 된다"
        return response

    return asyncio.run(
        interpret(fake_interpreter, "(아직 없음)", ["hackernews", "arxiv:cs.LG"], path=path, now=now)
    )


def _patch(monkeypatch, prompts: dict[str, list[str]], max_items: int = 3) -> None:
    """analyzer 의 LLM 호출을 가짜로 바꾸고 프롬프트를 종류별로 모은다."""
    spec_by_title = {title: (a, d, r) for title, _, a, d, r in _SPECS}

    async def fake_llm(prompt: str, title: str, **_kwargs: Any) -> dict[str, Any]:
        if title == "youtube-metadata-ranking":
            prompts.setdefault(title, []).append(prompt)
            return {"top_indices": [1, 0]}
        if title == "relative-rating":
            prompts.setdefault(title, []).append(prompt)
            # 목록에 실린 순서대로 그 아이템의 상대 평가를 돌려준다.
            order = [line.split("제목: ", 1)[1] for line in prompt.splitlines() if "] 제목: " in line]
            return {
                "ratings": [
                    {"index": i, "rating": spec_by_title[name][2]} for i, name in enumerate(order)
                ]
            }
        prompts.setdefault("analysis", []).append(prompt)
        actionability, depth, _ = spec_by_title[title]
        return {
            "one_line_summary": f"{title} 요약",
            "domain": ["ai-ml"],
            "content_type": "tutorial",
            "half_life": "durable",
            "tags": [title.split("-")[0]],
            "concepts": [f"개념 {title}"],
            "key_points": [{"point": f"{title} 핵심", "timestamp": None}],
            "positioning": None,
            "actionability": actionability,
            "depth": depth,
            "skip_reason": None,
        }

    monkeypatch.setattr(analyzer, "_call_llm_with_fallback", fake_llm)
    monkeypatch.setattr(analyzer, "load_vocabulary", lambda *a, **k: {"version": 1, "concepts": {}})
    monkeypatch.setattr(analyzer, "save_vocabulary", lambda *a, **k: None)
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())
    monkeypatch.setattr(analyzer, "get_settings", lambda: _settings(max_items))
    monkeypatch.setattr(analyzer.asyncio, "sleep", _noop_sleep)


def _run(
    monkeypatch,
    directive: Directive | None,
    profile: UserProfile | None = None,
    specs: list[tuple[str, str, int, int, int]] | None = None,
) -> tuple[list[DigestItem], list[dict[str, Any]], dict[str, list[str]]]:
    """YouTube 1차 랭킹과 filter_and_analyze 를 돌려 (선정, 풀 스냅샷, 프롬프트)를 돌려준다."""
    prompts: dict[str, list[str]] = {}
    _patch(monkeypatch, prompts)
    profile = profile or UserProfile()
    asyncio.run(
        analyzer._select_top_youtube_items([_youtube(n) for n in range(3)], profile, 2, directive)
    )
    entries: list[dict[str, Any]] = []
    items = [_raw(title, key) for title, key, *_ in (specs or _SPECS)]
    selected = asyncio.run(
        analyzer.filter_and_analyze(items, profile, directive, pool_out=entries)
    )
    return selected, entries, prompts


def _digest(prompts: dict[str, list[str]]) -> dict[str, str]:
    return {
        kind: hashlib.sha256("\n\x00\n".join(texts).encode("utf-8")).hexdigest()
        for kind, texts in prompts.items()
    }


def _picked(selected: list[DigestItem]) -> list[tuple[str, int]]:
    return [(d.raw.title, d.analysis.relevance_score) for d in selected]


# ──────────────────────────────────────────────
# 1. 개인 지시가 없는 날은 분리 전과 완전히 같다
# ──────────────────────────────────────────────

def test_dashboard_only_day_is_byte_identical_to_before_the_split(tmp_path, monkeypatch):
    directive = _interpret(tmp_path, [f"{DASHBOARD_PREFIX} {DASHBOARD_NOTE}"])
    selected, entries, prompts = _run(monkeypatch, directive)

    assert len(prompts["analysis"]) == len(_SPECS)
    assert all(DASHBOARD_NOTE in p for texts in prompts.values() for p in texts)
    assert _digest(prompts) == GOLDEN_PROMPT_SHA256
    assert _picked(selected) == GOLDEN_SELECTED


def test_day_without_any_directive_is_byte_identical_to_before_the_split(monkeypatch):
    selected, _entries, prompts = _run(monkeypatch, None)

    assert _digest(prompts) == GOLDEN_PROMPT_SHA256_NO_DIRECTIVE
    assert _picked(selected) == GOLDEN_SELECTED


# ──────────────────────────────────────────────
# 2. 개인 신호는 프롬프트와 풀에 닿지 않는다
# ──────────────────────────────────────────────

_PERSONAL_RESPONSE: dict[str, Any] = {
    "boost": ["인과추론"],
    "suppress": [],
    "drop_sources": ["hackernews"],
    "standing_note": "논문보다 실무 사례를 우선하고 요약은 더 어렵게",
    "difficulty": "harder",
}


def _shared_part(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """풀에서 모든 수신자가 공유하는 부분. selected_default 는 운영자 선정의 기록이라 뺀다."""
    return [{k: v for k, v in e.items() if k != "selected_default"} for e in entries]


def test_interpret_separates_system_note_from_personal_note(tmp_path):
    directive = _interpret(
        tmp_path,
        ["요약 좀 더 어렵게, HN 은 그만", f"{DASHBOARD_PREFIX} {DASHBOARD_NOTE}"],
        _PERSONAL_RESPONSE,
    )

    assert directive.system_note == DASHBOARD_NOTE
    assert directive.personal_note == _PERSONAL_RESPONSE["standing_note"]
    # 표시용 standing_note 는 예전처럼 사람 지시 뒤에 권고를 이은 값이다.
    assert directive.standing_note == f"{directive.personal_note} {DASHBOARD_NOTE}"
    assert directive.drop_sources == {"hackernews"} and directive.depth_bias == 1


def test_system_note_survives_a_long_personal_note_and_a_failed_interpretation(tmp_path):
    """사람 지시가 300자 상한을 다 써도, 해석이 실패해도 시스템 권고는 프롬프트로 간다."""
    long_note = "가" * 299
    directive = _interpret(
        tmp_path,
        ["길게 말함", f"{DASHBOARD_PREFIX} {DASHBOARD_NOTE}"],
        {**_PERSONAL_RESPONSE, "standing_note": long_note},
    )
    assert directive.system_note == DASHBOARD_NOTE
    assert directive.standing_note == long_note

    path = tmp_path / "broken.jsonl"
    now = _write_directives(path, ["사람 지시", f"{DASHBOARD_PREFIX} {DASHBOARD_NOTE}"])

    async def broken(prompt: str, title: str, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("모델 장애")

    failed = asyncio.run(interpret(broken, "", [], path=path, now=now))
    assert failed.system_note == DASHBOARD_NOTE and failed.personal_note == ""


def test_personal_directive_and_keywords_do_not_reach_any_prompt_or_the_pool(tmp_path, monkeypatch):
    system_only = _interpret(tmp_path, [f"{DASHBOARD_PREFIX} {DASHBOARD_NOTE}"])
    _, shared_entries, _ = _run(monkeypatch, system_only)

    personal = _interpret(
        tmp_path,
        ["요약 좀 더 어렵게, HN 은 그만", f"{DASHBOARD_PREFIX} {DASHBOARD_NOTE}"],
        _PERSONAL_RESPONSE,
    )
    profile = UserProfile(keyword_requests=["llm evaluation", "feature store"])
    _, entries, prompts = _run(monkeypatch, personal, profile)

    # 프롬프트는 개인 지시가 없는 날과 한 글자도 다르지 않다.
    assert _digest(prompts) == GOLDEN_PROMPT_SHA256
    joined = "\n".join(p for texts in prompts.values() for p in texts)
    assert "실무 사례" not in joined and "llm evaluation" not in joined
    # 끊은 출처도 분석돼 풀에 남고, 풀의 공용 부분은 개인 지시와 무관하다.
    assert len(prompts["analysis"]) == len(_SPECS)
    assert _shared_part(entries) == _shared_part(shared_entries)


def test_directly_built_standing_note_is_not_treated_as_system_advice(monkeypatch):
    """출처를 모르는 standing_note 는 공용 프롬프트에 싣지 않는다."""
    _, _, prompts = _run(monkeypatch, Directive(standing_note="나만의 지시"))

    assert _digest(prompts) == GOLDEN_PROMPT_SHA256_NO_DIRECTIVE


# ──────────────────────────────────────────────
# 3. drop_sources 는 선정에서만 뺀다
# ──────────────────────────────────────────────

def test_dropped_source_stays_in_the_pool_but_not_in_the_owners_selection(monkeypatch):
    directive = Directive(drop_sources={"hackernews"})
    selected, entries, prompts = _run(monkeypatch, directive)

    assert [d.raw.title for d in selected] == OLD_SELECTED_DROP_HACKERNEWS
    assert len(prompts["analysis"]) == len(_SPECS)
    assert {e["raw"]["source_key"] for e in entries} >= {"hackernews"}
    assert [e["raw"]["title"] for e in entries if e["selected_default"]] == sorted(
        OLD_SELECTED_DROP_HACKERNEWS, key=[t for t, *_ in _SPECS].index
    )

    # 같은 풀에서 다른 수신자는 그 출처를 받을 수 있다.
    scored = candidates_from_entries(entries)
    other = analyzer.select_for_user(scored, UserProfile(user_id="friend"), None, user_id="friend")
    assert _picked(other.selected) == GOLDEN_SELECTED
    # 운영자가 풀에서 다시 골라도 같은 결과다(구독자 발송의 운영자 재선정 경로).
    again = analyzer.select_for_user(scored, UserProfile(), directive)
    assert [d.raw.title for d in again.selected] == OLD_SELECTED_DROP_HACKERNEWS


def test_dropping_every_source_gives_up_the_drop(monkeypatch):
    """제외하면 후보가 0건이 되는 지시는 포기한다. 지시 한 줄이 발송을 비우면 안 된다."""
    specs = [s for s in _SPECS if s[1] == "hackernews"]
    selected, _, _ = _run(monkeypatch, Directive(drop_sources={"hackernews"}), specs=specs)

    assert [d.raw.title for d in selected] == ["hn-a", "hn-b"]


# ──────────────────────────────────────────────
# 4. depth_bias 는 선정 점수에만 걸린다
# ──────────────────────────────────────────────

def test_depth_bias_changes_the_owners_selection_but_not_the_stored_scores(monkeypatch):
    _, neutral_entries, _ = _run(monkeypatch, None)

    for directive, old_titles in (
        (Directive(depth_bias=1), OLD_SELECTED_HARDER),
        (Directive(depth_bias=-1), OLD_SELECTED_EASIER),
    ):
        selected, entries, _ = _run(monkeypatch, directive)
        assert [d.raw.title for d in selected] == old_titles
        assert _shared_part(entries) == _shared_part(neutral_entries)
        # 표시 점수는 풀에 저장된 중립 값 그대로다.
        stored = {e["raw"]["title"]: e["analysis"]["relevance_score"] for e in entries}
        assert all(d.analysis.relevance_score == stored[d.raw.title] for d in selected)
        # 풀에서 다시 골라도 같다.
        again = analyzer.select_for_user(candidates_from_entries(entries), UserProfile(), directive)
        assert [d.raw.title for d in again.selected] == old_titles


def test_personal_relevance_reproduces_the_score_the_old_analysis_stage_made(monkeypatch):
    _, entries, _ = _run(monkeypatch, None)
    by_title = {c.item.raw.title: c for c in candidates_from_entries(entries)}
    arxiv_b = by_title["arxiv-b"]

    assert arxiv_b.item.analysis.relevance_score == 7 and arxiv_b.relative_rating == 8
    assert (
        analyzer._personal_relevance(arxiv_b, analyzer.DEPTH_WEIGHT_HARDER)
        == OLD_HARDER_SCORE_ARXIV_B
    )
    assert analyzer._personal_relevance(arxiv_b, analyzer.DEPTH_WEIGHT) == 7
    # 근거 천장은 개인 배합으로도 넘지 못한다.
    for candidate in by_title.values():
        assert analyzer._personal_relevance(
            candidate, analyzer.DEPTH_WEIGHT_HARDER
        ) <= analyzer.evidence_ceiling(candidate.item.analysis.evidence_level)


def test_personal_relevance_without_a_stored_rating_moves_by_the_blend_difference():
    def candidate(relevance: int, absolute: int) -> ScoredCandidate:
        return ScoredCandidate(
            item=DigestItem(
                raw=_raw("x", "hackernews"),
                analysis=ContentAnalysis(
                    relevance_score=relevance, one_line_summary="요약",
                    evidence_level=EvidenceLevel.FULL, actionability=4, depth=9,
                ),
            ),
            absolute_score=absolute,
        )

    harder = analyzer.DEPTH_WEIGHT_HARDER
    neutral = analyzer.derive_relevance_score(4, 9)
    personal = analyzer.derive_relevance_score(4, 9, harder)
    assert (neutral, personal) == (6, 7)
    # 상대 평가가 걸리지 않은 후보: 예전 분석 단계가 만들던 절대 점수와 같다.
    assert analyzer._personal_relevance(candidate(6, 6), harder) == personal
    # 원점수가 없는 옛 풀 파일의 섞인 점수: 절대 점수 몫만큼만 옮긴다.
    assert analyzer._personal_relevance(candidate(8, 6), harder) == 8 + analyzer.ABSOLUTE_WEIGHT


# ──────────────────────────────────────────────
# 5. 운영자 개인 기록은 default 사용자에게만
# ──────────────────────────────────────────────

def _tied(title: str, source_key: str, tag: str, concept: str) -> ScoredCandidate:
    return ScoredCandidate(
        item=DigestItem(
            raw=_raw(title, source_key),
            analysis=ContentAnalysis(
                relevance_score=7, one_line_summary="요약", tags=[tag], concepts=[concept],
                evidence_level=EvidenceLevel.FULL, actionability=7, depth=7,
            ),
        ),
        absolute_score=7,
    )


def _tied_pool() -> list[ScoredCandidate]:
    return [
        _tied("first", "https://a.example/rss", "pandas", "데이터 전처리"),
        _tied("second", "https://b.example/rss", "dowhy", "인과추론"),
    ]


def test_quiz_weak_concepts_only_steer_the_default_user(monkeypatch):
    monkeypatch.setattr(analyzer, "get_settings", lambda: _settings(max_items=1))
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: {"인과추론"})

    owner = analyzer.select_for_user(_tied_pool(), UserProfile())
    assert [d.raw.title for d in owner.selected] == ["second"]
    assert owner.review_concepts == ["인과추론"]

    friend = analyzer.select_for_user(
        _tied_pool(), UserProfile(user_id="friend"), None, user_id="friend"
    )
    assert [d.raw.title for d in friend.selected] == ["first"]
    assert friend.review_concepts == [] and friend.preference_applied is False


def test_keyword_requests_break_ties_for_the_default_user_only(monkeypatch):
    monkeypatch.setattr(analyzer, "get_settings", lambda: _settings(max_items=1))
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())
    keywords = ["DoWhy"]

    assert [
        d.raw.title for d in analyzer.select_for_user(_tied_pool(), UserProfile()).selected
    ] == ["first"]

    directive = Directive(suppress={"없는 개념"})
    owner = analyzer.select_for_user(
        _tied_pool(), UserProfile(keyword_requests=keywords), directive
    )
    assert [d.raw.title for d in owner.selected] == ["second"]
    assert directive.boost == set(), "넘어온 지시를 바꾸면 안 된다"

    friend = analyzer.select_for_user(
        _tied_pool(),
        UserProfile(user_id="friend", keyword_requests=keywords),
        None,
        user_id="friend",
    )
    assert [d.raw.title for d in friend.selected] == ["first"]


def test_keyword_requests_never_outrank_a_higher_score(monkeypatch):
    """키워드는 동점 안에서만 순서를 정한다. 점수가 낮은 후보를 올리지 않는다."""
    monkeypatch.setattr(analyzer, "get_settings", lambda: _settings(max_items=1))
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())
    pool = _tied_pool()
    pool[0].item.analysis.relevance_score = 8

    owner = analyzer.select_for_user(pool, UserProfile(keyword_requests=["dowhy"]))
    assert [d.raw.title for d in owner.selected] == ["first"]
