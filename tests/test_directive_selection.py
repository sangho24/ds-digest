"""지시 → 선정 연결, 논문 배경·위치, 퀴즈 결과 되돌려주기 (2026-09-03).

실측 배경: "1번 내용처럼 좀 더 어려운 내용" 지시가 standing_note로 정확히
해석됐는데도 선정은 점수·다양성만으로 돌았다. standing_note는 프롬프트에만
들어가고 정렬 키에는 0이었기 때문이다. 여기서는 지시가 실제로 **점수 축과
타이브레이크**까지 닿는지, 논문 아이템에 배경·위치가 실리는지, 어제 퀴즈 결과가
사람이 읽을 메시지로 돌아오는지를 본다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import app.analyzer as analyzer
import app.deliverers.discord as dc
from app.contract import _item_to_contract as build_contract_item
from app.directives import (
    DEEP_DEPTH,
    DIRECTIVE_WEIGHT,
    SHALLOW_DEPTH,
    Directive,
    directive_score,
    parse_directive,
)
from app.models import ContentAnalysis, DigestItem, EvidenceLevel, RawContent, SourceType
from app.preferences import build_item_index
from app.quiz_results import format_quiz_recap, record_answer


# ── 해석: difficulty → depth_bias ──────────────────────────────────────────

def test_parse_difficulty_to_depth_bias():
    base = {"boost": [], "suppress": [], "drop_sources": [], "standing_note": "더 어렵게"}
    assert parse_directive({**base, "difficulty": "harder"}).depth_bias == 1
    assert parse_directive({**base, "difficulty": "easier"}).depth_bias == -1
    assert parse_directive({**base, "difficulty": ""}).depth_bias == 0
    assert parse_directive(base).depth_bias == 0          # 키가 없어도 죽지 않는다
    assert parse_directive({**base, "difficulty": "HARDER"}).depth_bias == 1


def test_depth_bias_alone_is_not_empty_and_is_visible():
    d = Directive(depth_bias=1)
    assert not d.is_empty()
    assert "🎚" in d.describe()      # 안 보이는 상태값이 큐레이션을 끌고 가면 안 된다


# ── 선정: 타이브레이크와 점수 축 ────────────────────────────────────────────

def test_directive_score_uses_depth_when_biased():
    harder = Directive(depth_bias=1)
    assert directive_score(harder, [], [], depth=DEEP_DEPTH) == DIRECTIVE_WEIGHT
    assert directive_score(harder, [], [], depth=SHALLOW_DEPTH) == -DIRECTIVE_WEIGHT
    assert directive_score(harder, [], [], depth=5) == 0            # 중앙은 건드리지 않는다
    easier = Directive(depth_bias=-1)
    assert directive_score(easier, [], [], depth=DEEP_DEPTH) == -DIRECTIVE_WEIGHT
    assert directive_score(Directive(), [], [], depth=DEEP_DEPTH) == 0
    assert directive_score(harder, [], []) == 0                     # depth를 모르면 0


def test_directive_score_keeps_boost_semantics():
    d = Directive(boost={"인과추론"}, depth_bias=1)
    assert directive_score(d, [], ["인과추론"], depth=DEEP_DEPTH) == 2 * DIRECTIVE_WEIGHT


def test_depth_weight_moves_with_directive():
    assert analyzer.depth_weight_for(None) == analyzer.DEPTH_WEIGHT
    assert analyzer.depth_weight_for(Directive()) == analyzer.DEPTH_WEIGHT
    assert analyzer.depth_weight_for(Directive(depth_bias=1)) == analyzer.DEPTH_WEIGHT_HARDER
    assert analyzer.depth_weight_for(Directive(depth_bias=-1)) == analyzer.DEPTH_WEIGHT_EASIER

    # 얕지만 실행 가능한 글(A=8, D=3) vs 깊지만 바로 못 쓰는 논문(A=3, D=8).
    shallow, deep = (8, 3), (3, 8)
    default = analyzer.DEPTH_WEIGHT
    assert analyzer.derive_relevance_score(*shallow, default) > analyzer.derive_relevance_score(*deep, default)
    harder = analyzer.DEPTH_WEIGHT_HARDER
    assert analyzer.derive_relevance_score(*deep, harder) > analyzer.derive_relevance_score(*shallow, harder)
    # 기본 배합은 그대로다.
    assert analyzer.derive_relevance_score(6, 6) == 6


def _digest_item(title: str, depth: int = 6) -> DigestItem:
    return DigestItem(
        raw=RawContent(
            source_type=SourceType.RSS, source_name="arXiv", source_key="arxiv:cs.LG",
            title=title, url=f"https://arxiv.org/abs/{title}", body="본문 " * 50,
        ),
        analysis=ContentAnalysis(
            relevance_score=6, one_line_summary="요약", tags=["T"], concepts=["개념"],
            evidence_level=EvidenceLevel.FULL, actionability=6, depth=depth,
            key_points=[], production_ideas=[], quiz=[],
        ),
    )


def test_relative_rating_prompt_carries_standing_note(monkeypatch):
    """점수를 벌리는 단계에 지시가 안 들어가면 글쓰기만 바뀌고 순위는 그대로다."""
    captured: dict = {}

    async def fake_llm(prompt, title, **kwargs):
        captured["prompt"] = prompt
        return {"ratings": [{"index": i, "rating": 5} for i in range(3)]}

    monkeypatch.setattr(analyzer, "_call_llm_with_fallback", fake_llm)
    items = [_digest_item(f"p{i}") for i in range(3)]

    asyncio.run(analyzer.apply_relative_rating(items, standing_note="더 어려운 수준으로"))
    assert "## 사용자 지시" in captured["prompt"]
    assert "더 어려운 수준으로" in captured["prompt"]

    asyncio.run(analyzer.apply_relative_rating(items))
    assert "## 사용자 지시" not in captured["prompt"]


# ── 논문 배경·위치 ───────────────────────────────────────────────────────

def test_positioning_is_parsed_and_null_like_values_fold_to_none():
    assert analyzer._clean_positioning("  RAG 코드 생성의 검색 단계 개선 계열  ") == "RAG 코드 생성의 검색 단계 개선 계열"
    for value in (None, "", "null", "None", "없음", "해당 없음"):
        assert analyzer._clean_positioning(value) is None


def test_positioning_rendered_in_discord_and_contract():
    item = _digest_item("paper")
    item.analysis.positioning = "기존 RAG는 파일 단위로 검색했는데, 이 논문은 토큰 단위로 검색 시점을 정한다."
    text = dc._format_item(item, 1)
    assert "📍 기존 RAG는" in text
    assert text.index("📍") > text.index("> 요약")          # 요약 바로 아래

    payload = build_contract_item(item)
    assert payload["positioning"] == item.analysis.positioning

    item.analysis.positioning = None
    assert "📍" not in dc._format_item(item, 1)
    assert build_contract_item(item)["positioning"] is None


# ── 퀴즈 결과 되돌려주기 ────────────────────────────────────────────────

def _write_record(tmp_path: Path) -> Path:
    records = tmp_path / "records"
    records.mkdir()
    (records / "digest_2026-09-02.json").write_text(json.dumps({
        "date": "2026-09-02",
        "items": [{
            "raw": {"url": "https://arxiv.org/abs/1", "title": "논문 A", "source_key": "arxiv:cs.LG"},
            "analysis": {
                "tags": ["t"], "concepts": ["개념"],
                "quiz": [{
                    "question": "핵심 원리는?",
                    "options": ["가", "나", "다"],
                    "answer_index": 1,
                    "explanation": "나이기 때문",
                }],
            },
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return records


def test_record_answer_returns_question_text_for_recap(tmp_path):
    records = _write_record(tmp_path)
    index = build_item_index(records)
    facts = next(iter(index.values()))
    assert facts.title == "논문 A" and facts.quiz[0]["question"] == "핵심 원리는?"

    item_id = next(iter(index))
    result = record_answer(item_id, 0, 2, index=index, path=tmp_path / "quiz.jsonl")
    assert result["correct"] is False
    assert result["question"] == "핵심 원리는?"
    assert result["choice_text"] == "다" and result["answer_text"] == "나"
    assert result["explanation"] == "나이기 때문"

    # 파일에는 라벨만 남는다 — 되돌려주기용 원문은 정본에 있으므로 중복 저장하지 않는다.
    stored = json.loads((tmp_path / "quiz.jsonl").read_text(encoding="utf-8").strip())
    assert "question" not in stored and stored["correct"] is False


def test_format_quiz_recap_shows_my_answer_and_correct_one():
    details = [
        {"title": "논문 A", "question": "핵심 원리는?", "choice_index": 2, "answer_index": 1,
         "choice_text": "다", "answer_text": "나", "explanation": "나이기 때문", "correct": False},
        {"title": "논문 B", "question": "무엇을 재는가?", "choice_index": 0, "answer_index": 0,
         "choice_text": "가", "answer_text": "가", "explanation": "", "correct": True},
    ]
    messages = format_quiz_recap(details)
    assert len(messages) == 1
    text = messages[0]
    assert text.startswith("🧠 **어제 퀴즈 결과 1/2**")
    assert "❌ **논문 A**" in text and "내 답 ③ 다 → 정답 ② 나" in text
    assert "-# 나이기 때문" in text
    assert "✅ **논문 B**" in text and "내 답 ① 가" in text and "→ 정답" not in text.split("논문 B")[1]
    assert format_quiz_recap([]) == []


def test_format_quiz_recap_splits_at_question_boundaries():
    details = [
        {"title": f"논문 {i}", "question": "질문 " * 80, "choice_index": 0, "answer_index": 1,
         "choice_text": "가", "answer_text": "나", "explanation": "해설 " * 80, "correct": False}
        for i in range(6)
    ]
    messages = format_quiz_recap(details, limit=1900)
    assert len(messages) > 1
    assert all(len(m) <= 1900 for m in messages)
    assert sum(m.count("❌") for m in messages) == 6     # 잘려 사라진 문항이 없다
