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
