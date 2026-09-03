"""개념 어휘 — 태그가 못 하는 일반화 (v2 §3.5 Layer 2).

태그는 §3.5대로 "본문에 실제 등장한 고유명사"라 `glm-5.3-flash` 같은 1회성
문자열이 된다. 같은 값이 두 번 나올 일이 없으니 정답률 집계도 취향 일반화도
표본 1에 머문다. 개념은 어휘로 해소돼 재사용된다.

실행: pytest tests/test_concepts.py -v
"""

from __future__ import annotations

import json

import pytest

from app.concepts import (
    load_vocabulary,
    normalize,
    novelty_rate,
    register,
    resolve,
    save_vocabulary,
    vocabulary_for_prompt,
)


@pytest.fixture
def vocab():
    return {"version": 1, "updated_at": None, "concepts": {}}


# ──────────────────────────────────────────────
# 정규화 (해소 1단)
# ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MLOps", "mlops"),
        ("ml-ops", "ml ops"),
        ("  ML   Ops  ", "ml ops"),
        ("ML_Ops", "ml ops"),
        ("모델 서빙", "모델 서빙"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_variants_resolve_to_same_concept(vocab):
    """MLOps / ml-ops / ML Ops 가 한 노드로 병합돼야 한다 (§8.2의 핵심 요구)."""
    register(["MLOps"], vocab)

    for variant in ("ml-ops", "ML Ops", "ML_OPS", "  mlops "):
        assert resolve(variant, vocab) == "MLOps", variant


def test_resolve_unknown_returns_none(vocab):
    register(["MLOps"], vocab)

    assert resolve("인과추론", vocab) is None


def test_resolve_does_not_merge_by_substring(vocab):
    """과다 병합이 과소 병합보다 위험하다(§8.2) — 부분 일치로 합치지 않는다."""
    register(["모델 서빙"], vocab)

    assert resolve("모델", vocab) is None
    assert resolve("모델 서빙 최적화", vocab) is None


# ──────────────────────────────────────────────
# 등록
# ──────────────────────────────────────────────

def test_register_returns_resolved_and_created(vocab):
    resolved, created = register(["MLOps", "인과추론"], vocab, date="2026-08-30")

    assert resolved == ["MLOps", "인과추론"]
    assert created == ["MLOps", "인과추론"]


def test_register_second_time_is_not_new(vocab):
    register(["MLOps"], vocab, date="2026-08-30")

    resolved, created = register(["ml-ops"], vocab, date="2026-08-31")

    assert resolved == ["MLOps"]   # 표준명으로 되돌아온다
    assert created == []


def test_register_records_alias(vocab):
    register(["MLOps"], vocab)
    register(["ML Ops"], vocab)

    assert "ML Ops" in vocab["concepts"]["MLOps"]["aliases"]


def test_register_counts_and_dates(vocab):
    register(["MLOps"], vocab, date="2026-08-01")
    register(["MLOps"], vocab, date="2026-08-30")

    entry = vocab["concepts"]["MLOps"]
    assert entry["count"] == 2
    assert entry["first_seen"] == "2026-08-01"
    assert entry["last_seen"] == "2026-08-30"


def test_register_skips_blank(vocab):
    resolved, created = register(["", "   ", None], vocab)

    assert resolved == [] and created == []


def test_register_dedupes_within_one_call(vocab):
    resolved, _ = register(["MLOps", "ml-ops"], vocab)

    assert resolved == ["MLOps"]


# ──────────────────────────────────────────────
# 프롬프트용 어휘
# ──────────────────────────────────────────────

def test_vocabulary_for_prompt_orders_by_frequency(vocab):
    register(["드문것"], vocab)
    for _ in range(5):
        register(["흔한것"], vocab)

    assert vocabulary_for_prompt(vocab).startswith("흔한것")


def test_vocabulary_for_prompt_respects_limit(vocab):
    register([f"개념{i}" for i in range(10)], vocab)

    assert len(vocabulary_for_prompt(vocab, limit=3).split(", ")) == 3


def test_vocabulary_for_prompt_empty_is_not_blank(vocab):
    """빈 문자열을 넣으면 모델이 형식을 오해한다."""
    assert vocabulary_for_prompt(vocab).strip() != ""


# ──────────────────────────────────────────────
# 신규율 (§3.6 메타 루브릭)
# ──────────────────────────────────────────────

def test_novelty_rate():
    assert novelty_rate(["a", "b", "c", "d"], ["a"]) == 0.25
    assert novelty_rate([], []) == 0.0
    assert novelty_rate(["a"], ["a"]) == 1.0


# ──────────────────────────────────────────────
# 영속성
# ──────────────────────────────────────────────

def test_save_and_load_roundtrip(tmp_path, vocab):
    register(["MLOps", "인과추론"], vocab)
    path = tmp_path / "concepts.json"

    save_vocabulary(vocab, path)
    loaded = load_vocabulary(path)

    assert set(loaded["concepts"]) == {"MLOps", "인과추론"}
    assert loaded["updated_at"] is not None


def test_load_missing_file_returns_empty(tmp_path):
    assert load_vocabulary(tmp_path / "없음.json")["concepts"] == {}


def test_load_broken_file_returns_empty(tmp_path):
    """어휘가 깨졌다고 파이프라인이 멈추면 안 된다."""
    path = tmp_path / "concepts.json"
    path.write_text("{ 깨짐", encoding="utf-8")

    assert load_vocabulary(path)["concepts"] == {}


def test_load_wrong_shape_returns_empty(tmp_path):
    path = tmp_path / "concepts.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert load_vocabulary(path)["concepts"] == {}


# ── 쉼표로 이어 온 개념 나누기 (실측 2026-09-02) ────────────────────────────
# 모델이 `"멀티프로세싱, 병렬 처리"`를 한 덩어리로 냈고, 어휘에 **세 번째 표준
# 개념**으로 등록됐다. 이미 있던 `멀티프로세싱`·`병렬 처리`와 영영 매칭되지
# 않으므로 그 아이템의 반응은 다시 안 나올 개념으로 귀속되어 사라진다.

def test_split_raw_divides_on_comma_only():
    from app.concepts import split_raw
    assert split_raw("멀티프로세싱, 병렬 처리") == ["멀티프로세싱", "병렬 처리"]
    assert split_raw("A，B") == ["A", "B"]          # 전각 쉼표
    # 나누면 안 되는 것들 — 과다 분할은 되돌리기 어렵다(§8.2).
    assert split_raw("A/B 테스트") == ["A/B 테스트"]
    assert split_raw("신경망 기하학 및 개념 매니폴드") == ["신경망 기하학 및 개념 매니폴드"]
    assert split_raw("수집·분석") == ["수집·분석"]
    # 빈 값·None은 조용히 사라진다 (None이 "None" 개념으로 등록되면 안 된다).
    assert split_raw(" , ") == [] and split_raw(None) == []


def test_register_merges_comma_joined_into_existing_concepts():
    from app.concepts import register
    vocab = {"version": 1, "concepts": {}}
    register(["멀티프로세싱", "병렬 처리"], vocab, date="2026-09-01")
    resolved, created = register(["멀티프로세싱, 병렬 처리"], vocab, date="2026-09-02")

    assert created == [], f"파편이 새 개념으로 등록됐다: {created}"
    assert resolved == ["멀티프로세싱", "병렬 처리"]
    assert vocab["concepts"]["멀티프로세싱"]["count"] == 2
    assert vocab["concepts"]["병렬 처리"]["count"] == 2
    assert len(vocab["concepts"]) == 2


def test_analysis_parsing_splits_and_caps_concepts():
    """나눈 뒤 상한을 다시 걸지 않으면 pydantic이 거부해 아이템이 통째로 실패한다."""
    from app.analyzer import _split_concepts
    from app.concepts import MAX_CONCEPTS_PER_ITEM
    from app.models import ContentAnalysis, EvidenceLevel

    out = _split_concepts(["A, B", "C, D", "E"])
    assert out == ["A", "B", "C"][:MAX_CONCEPTS_PER_ITEM]
    assert len(out) <= MAX_CONCEPTS_PER_ITEM
    assert _split_concepts(["A, A", "A"]) == ["A"]        # 중복은 접는다
    assert _split_concepts(None) == [] and _split_concepts([]) == []

    # 상한을 넘겨도 모델 생성이 실패하지 않는다.
    ContentAnalysis(relevance_score=5, one_line_summary="요약",
                    concepts=_split_concepts(["가, 나", "다, 라"]),
                    evidence_level=EvidenceLevel.FULL)
