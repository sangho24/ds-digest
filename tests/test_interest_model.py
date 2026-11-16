"""관심도 표시를 선정에 되먹이는 계층(app/interest_model.py)과 analyzer 연결의 단위 테스트.

실제 LLM 은 호출하지 않는다. 정본과 라벨 파일은 tmp_path 에 만들고, analyze_content 는
통제된 ContentAnalysis 로 바꾼다. pytest-asyncio 가 없으므로 asyncio.run() 으로 돈다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.analyzer as analyzer
import app.interest_model as interest_model
from app.interest_model import (
    MAX_ADJUST,
    MIN_FEATURE_COUNT,
    MIN_LABELS,
    SHRINKAGE_K,
    FeatureStat,
    InterestModel,
    build_interest_model,
    feature_points,
    interest_adjust,
    item_features,
    load_interest_labels,
    ranked_features,
)
from app.models import ContentAnalysis, EvidenceLevel, RawContent, SourceType, UserProfile
from evals.interest import format_report, main, normalize_label, summarize, summarize_learned


def _label(url: str, human: str, **extra) -> dict:
    record = {"url": url, "human": human, "note": "", "source_key": None}
    record.update(extra)
    return record


def _write_labels(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _entry(url: str, source_key: str, domain: list[str], content_type: str, tags: list[str]) -> dict:
    return {
        "raw": {"url": url, "source_key": source_key, "source_name": source_key, "title": url},
        "analysis": {"domain": domain, "content_type": content_type, "tags": tags},
    }


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[dict[str, dict], Path]:
    """라벨 20건(want 10, skip 10)과 그 정본.

    arxiv 계열 8건은 want 7 / skip 1, hackernews 8건은 want 1 / skip 7,
    나머지 4건(want 2 / skip 2)은 출처가 제각각이라 출처 특징에 못 든다.
    """
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    entries: list[dict] = []
    labels: dict[str, dict] = {}

    def add(url: str, human: str, source_key: str, domain: str, content_type: str, tags: list[str]):
        entries.append(_entry(url, source_key, [domain], content_type, tags))
        labels[url] = _label(url, human)

    for i in range(8):
        add(f"https://arxiv.org/abs/{i}", "want" if i < 7 else "skip",
            f"arxiv:cs.{'LG' if i % 2 else 'CL'}", "ai-ml", "paper", ["LLM", f"one-off-{i}"])
    for i in range(8):
        add(f"https://news.ycombinator.com/item?id={i}", "want" if i < 1 else "skip",
            "hackernews", "business", "news", ["Funding"])
    for i in range(4):
        add(f"https://blog{i}.example/post", "want" if i < 2 else "skip",
            f"https://blog{i}.example/rss", "tools", "tutorial", [])

    half = len(entries) // 2
    for name, chunk in (("digest_2026-11-01.json", entries[:half]), ("digest_2026-11-02.json", entries[half:])):
        (records_dir / name).write_text(
            json.dumps({"date": name[7:17], "items": chunk}, ensure_ascii=False), encoding="utf-8"
        )
    return labels, records_dir


# ──────────────────────────────────────────────
# 라벨 읽기
# ──────────────────────────────────────────────


def test_missing_file_returns_empty_and_inactive(tmp_path: Path):
    labels = load_interest_labels(path=tmp_path / "nope.jsonl")
    assert labels == {}
    model = build_interest_model(labels, tmp_path / "no_records")
    assert model.active is False
    assert model.label_count == 0
    assert interest_adjust(item_features("arxiv:cs.LG", ["ai-ml"], "paper", ["LLM"]), model) == (0.0, [])


def test_default_path_is_resolved_at_call_time(tmp_path: Path, monkeypatch):
    """기본 경로는 호출 시점에 읽는다. conftest 의 격리가 이 성질에 기댄다."""
    path = _write_labels(tmp_path / "labels.jsonl", [_label("https://a.example/1", "want")])
    monkeypatch.setattr(interest_model, "DEFAULT_LABELS_PATH", path)
    assert set(load_interest_labels()) == {"https://a.example/1"}


def test_load_skips_unsure_broken_lines_and_keeps_last(tmp_path: Path):
    path = tmp_path / "labels.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_label("https://a.example/1", "skip")),
                "{깨진 줄",
                "[1, 2]",
                json.dumps({"human": "want"}),
                json.dumps(_label("https://b.example/2", "unsure")),
                json.dumps(_label("https://a.example/1", "want")),
                json.dumps(_label("https://c.example/3", "want")),
                json.dumps(_label("https://c.example/3", "unsure")),
                "",
            ]
        ),
        encoding="utf-8",
    )
    labels = load_interest_labels(path=path)
    # a 는 마지막 기록(want)이 이기고, b 는 unsure 라 빠지고, c 는 나중에 unsure 로 고쳐 빠진다.
    assert set(labels) == {"https://a.example/1"}
    assert labels["https://a.example/1"]["human"] == "want"


def test_load_filters_by_user_id(tmp_path: Path):
    path = _write_labels(
        tmp_path / "labels.jsonl",
        [
            _label("https://a.example/1", "want"),
            _label("https://b.example/2", "skip", user_id="default"),
            _label("https://c.example/3", "want", user_id="other"),
            # 다른 사용자가 같은 url 을 뒤에 표시해도 내 기록을 덮지 않는다.
            _label("https://a.example/1", "skip", user_id="other"),
        ],
    )
    mine = load_interest_labels(path=path)
    assert set(mine) == {"https://a.example/1", "https://b.example/2"}
    assert mine["https://a.example/1"]["human"] == "want"

    theirs = load_interest_labels("other", path=path)
    assert set(theirs) == {"https://c.example/3", "https://a.example/1"}
    assert theirs["https://a.example/1"]["human"] == "skip"


# ──────────────────────────────────────────────
# 모델
# ──────────────────────────────────────────────


def test_model_inactive_below_min_labels(corpus):
    labels, records_dir = corpus
    assert len(labels) == MIN_LABELS
    short = dict(list(labels.items())[: MIN_LABELS - 1])
    model = build_interest_model(short, records_dir)
    assert model.active is False
    assert model.label_count == MIN_LABELS - 1
    assert model.features == {}
    assert interest_adjust(item_features("arxiv:cs.LG", ["ai-ml"], "paper", ["LLM"]), model) == (0.0, [])


def test_unsure_does_not_count_toward_activation(corpus):
    labels, records_dir = corpus
    padded = dict(list(labels.items())[: MIN_LABELS - 1])
    for i in range(10):
        padded[f"https://unsure.example/{i}"] = _label(f"https://unsure.example/{i}", "unsure")
    assert build_interest_model(padded, records_dir).active is False


def test_shrinkage_rate_and_deviation(corpus):
    labels, records_dir = corpus
    model = build_interest_model(labels, records_dir)
    assert model.active is True
    assert model.label_count == 20
    assert model.base_rate == pytest.approx(0.5)

    # arxiv:cs.LG 와 arxiv:cs.CL 이 한 계열로 접혀 8건이 된다.
    arxiv = model.features["source"]["arxiv"]
    assert (arxiv.want, arxiv.skip) == (7, 1)
    assert arxiv.rate == pytest.approx((7 + SHRINKAGE_K * 0.5) / (8 + SHRINKAGE_K))
    assert arxiv.deviation == pytest.approx(9.5 / 13 - 0.5)

    hn = model.features["source"]["hackernews"]
    assert hn.rate == pytest.approx((1 + SHRINKAGE_K * 0.5) / (8 + SHRINKAGE_K))
    assert hn.deviation == pytest.approx(-arxiv.deviation)

    # 태그는 소문자로 정규화된다. 원시 비율 7/8 보다 수축 비율이 전체 평균 쪽에 있다.
    llm = model.features["tag"]["llm"]
    assert 0.5 < llm.rate < 7 / 8


def test_features_below_min_count_are_excluded(corpus):
    labels, records_dir = corpus
    model = build_interest_model(labels, records_dir)
    assert MIN_FEATURE_COUNT == 3
    # 블로그 4곳은 출처당 1건, one-off 태그도 1건씩이라 모델에 없다.
    assert set(model.features["source"]) == {"arxiv", "hackernews"}
    assert set(model.features["tag"]) == {"llm", "funding"}
    # 같은 블로그 4건이 공유하는 도메인·유형은 4건이라 들어간다.
    assert "tools" in model.features["domain"]
    assert "tutorial" in model.features["content_type"]


def test_unjoined_label_falls_back_to_its_source_key(tmp_path: Path):
    """정본에서 못 찾은 url 도 라벨에 적힌 source_key 로 출처 특징은 낸다."""
    labels = {
        f"https://gone.example/{i}": _label(
            f"https://gone.example/{i}", "want" if i < 15 else "skip",
            source_key="arxiv:cs.LG" if i < 10 else "hackernews",
        )
        for i in range(20)
    }
    model = build_interest_model(labels, tmp_path / "no_records")
    assert model.active is True
    assert set(model.features) == {"source"}
    assert model.features["source"]["arxiv"].want == 10


# ──────────────────────────────────────────────
# 조정값
# ──────────────────────────────────────────────


def _model_with(*stats: FeatureStat) -> InterestModel:
    model = InterestModel(active=True, label_count=MIN_LABELS, base_rate=0.5)
    for stat in stats:
        model.features.setdefault(stat.kind, {})[stat.value] = stat
    return model


def _stat(kind: str, value: str, deviation: float) -> FeatureStat:
    return FeatureStat(kind=kind, value=value, want=5, skip=5, rate=0.5 + deviation, deviation=deviation)


def test_adjust_single_kind_renormalizes_weight():
    """매칭된 종류가 하나면 그 종류의 가중이 1 로 재정규화된다. 편차 0.25 는 0.75점."""
    model = _model_with(_stat("tag", "llm", 0.25))
    value, reasons = interest_adjust(item_features("unknown", ["ai-ml"], "paper", ["LLM", "x"]), model)
    assert value == pytest.approx(0.75)
    assert reasons == ["tag:llm +0.75"]


def test_adjust_combines_kinds_by_weight():
    model = _model_with(
        _stat("source", "arxiv", 0.3),
        _stat("domain", "ai-ml", -0.1),
        _stat("domain", "tools", 0.3),
        _stat("content_type", "paper", 0.2),
    )
    value, reasons = interest_adjust(
        item_features("arxiv:cs.LG", ["ai-ml", "tools"], "paper", ["처음 보는 태그"]), model
    )
    # 매칭된 종류는 source(0.4), domain(0.3), content_type(0.1). 도메인은 두 값의 평균 0.1.
    expected = (0.4 * 0.3 + 0.3 * 0.1 + 0.1 * 0.2) / 0.8 * (MAX_ADJUST / 0.5)
    assert value == pytest.approx(expected, abs=1e-3)
    assert [r.split(" ")[0] for r in reasons] == ["source:arxiv", "domain:ai-ml,tools", "content_type:paper"]
    assert sum(float(r.split(" ")[1]) for r in reasons) == pytest.approx(expected, abs=0.02)


def test_adjust_is_clamped_both_ways():
    model = _model_with(_stat("source", "arxiv", 0.9), _stat("source", "hackernews", -0.9))
    up, _ = interest_adjust(item_features("arxiv:cs.LG"), model)
    down, _ = interest_adjust(item_features("hackernews"), model)
    assert up == MAX_ADJUST
    assert down == -MAX_ADJUST
    assert feature_points(_stat("source", "x", 0.9)) == MAX_ADJUST


def test_adjust_zero_without_match_or_when_inactive():
    model = _model_with(_stat("source", "arxiv", 0.3))
    assert interest_adjust(item_features("hackernews", ["ai-ml"], "news", ["LLM"]), model) == (0.0, [])
    model.active = False
    assert interest_adjust(item_features("arxiv:cs.LG"), model) == (0.0, [])


def test_ranked_features_orders_by_deviation(corpus):
    labels, records_dir = corpus
    ranked = ranked_features(build_interest_model(labels, records_dir))
    deviations = [stat.deviation for stat in ranked]
    assert deviations == sorted(deviations, reverse=True)
    assert ranked[0].deviation > 0 > ranked[-1].deviation


# ──────────────────────────────────────────────
# analyzer 선정 연결
# ──────────────────────────────────────────────


async def _noop_sleep(_seconds):
    return None


def _raw(title: str, source_key: str) -> RawContent:
    return RawContent(
        source_type=SourceType.RSS,
        source_name=source_key,
        source_key=source_key,
        title=title,
        url=f"https://example.com/{title}",
        body="본문",
    )


def _run_selection(monkeypatch, specs: list[tuple[str, str, int]], *, max_items: int = 1):
    """(제목, source_key, relevance_score) 목록을 filter_and_analyze 에 통과시킨다."""
    score_by_title = {title: score for title, _, score in specs}

    async def fake_analyze(item, profile, **_kwargs):
        return ContentAnalysis(
            relevance_score=score_by_title[item.title],
            one_line_summary="요약",
            evidence_level=EvidenceLevel.FULL,
            skip_reason=None,
        )

    monkeypatch.setattr(analyzer, "analyze_content", fake_analyze)
    monkeypatch.setattr(analyzer, "load_vocabulary", lambda *a, **k: {"version": 1, "concepts": {}})
    monkeypatch.setattr(analyzer, "save_vocabulary", lambda *a, **k: None)
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())
    monkeypatch.setattr(
        analyzer,
        "get_settings",
        lambda: SimpleNamespace(
            dry_run=True,
            groq_api_key="",
            relevance_floor=4,
            max_items_per_digest=max_items,
            max_items_per_source=3,
            source_diversity_strength=1.0,
        ),
    )
    monkeypatch.setattr(analyzer.asyncio, "sleep", _noop_sleep)
    items = [_raw(title, source_key) for title, source_key, _ in specs]
    return asyncio.run(analyzer.filter_and_analyze(items, UserProfile()))


def _use_model(monkeypatch, model: InterestModel) -> None:
    monkeypatch.setattr(analyzer, "build_interest_model", lambda *a, **k: model)


# 점수가 다른 두 후보. 조정이 없으면 hackernews(7점)가 arxiv(6점)를 이긴다.
_TWO = [("높음", "hackernews", 7), ("낮음", "arxiv:cs.LG", 6)]


def test_selection_keeps_order_when_model_inactive(monkeypatch):
    """라벨 파일이 없으면(conftest 가 기본 경로를 없는 파일로 돌려 둔다) 기존 순서 그대로다."""
    selected = _run_selection(monkeypatch, _TWO)
    assert [d.raw.title for d in selected] == ["높음"]
    assert selected[0].analysis.notes == []

    both = _run_selection(monkeypatch, _TWO, max_items=2)
    assert [d.raw.title for d in both] == ["높음", "낮음"]
    assert all(d.analysis.notes == [] for d in both)


def test_selection_keeps_order_when_labels_below_threshold(monkeypatch, tmp_path: Path):
    """표시가 있어도 MIN_LABELS 미만이면 선정은 그대로다."""
    path = _write_labels(
        tmp_path / "labels.jsonl",
        [_label(f"https://arxiv.org/abs/{i}", "want", source_key="arxiv:cs.LG") for i in range(MIN_LABELS - 1)],
    )
    monkeypatch.setattr(interest_model, "DEFAULT_LABELS_PATH", path)
    selected = _run_selection(monkeypatch, _TWO)
    assert [d.raw.title for d in selected] == ["높음"]
    assert selected[0].analysis.notes == []


def test_selection_order_flips_with_adjustment(monkeypatch):
    """점수 동률이 아닌 두 후보의 순서가 조정으로 뒤집힌다. 저장되는 점수는 그대로다."""
    _use_model(monkeypatch, _model_with(_stat("source", "arxiv", 0.4)))
    selected = _run_selection(monkeypatch, _TWO)

    # 6 + 1.2 = 7.2 > 7 이라 한 자리를 arxiv 가 가져간다.
    assert [d.raw.title for d in selected] == ["낮음"]
    picked = selected[0]
    assert picked.analysis.relevance_score == 6
    assert len(picked.analysis.notes) == 1
    note = picked.analysis.notes[0]
    assert note.chapter == analyzer.INTEREST_NOTE_CHAPTER
    assert note.text == "interest_adjust +1.20 (source:arxiv +1.20)"


def test_selection_end_to_end_from_label_file(monkeypatch, tmp_path: Path):
    """라벨 파일에서 모델까지 실제 경로로 돌려도 순서가 뒤집힌다."""
    records = [
        _label(f"https://arxiv.org/abs/{i}", "want", source_key="arxiv:cs.LG") for i in range(10)
    ] + [
        _label(f"https://news.ycombinator.com/item?id={i}", "skip", source_key="hackernews") for i in range(10)
    ]
    monkeypatch.setattr(interest_model, "DEFAULT_LABELS_PATH", _write_labels(tmp_path / "labels.jsonl", records))
    monkeypatch.setattr(interest_model, "RECORDS_DIR", tmp_path / "no_records")

    selected = _run_selection(monkeypatch, _TWO)
    assert [d.raw.title for d in selected] == ["낮음"]
    assert selected[0].analysis.relevance_score == 6


def test_adjustment_does_not_rescue_items_below_floor(monkeypatch):
    """바닥값은 조정 전 점수로 잰다. 조정으로 바닥을 넘는 일은 없다."""
    _use_model(monkeypatch, _model_with(_stat("source", "arxiv", 0.5)))
    selected = _run_selection(monkeypatch, [("바닥 아래", "arxiv:cs.LG", 3), ("통과", "hackernews", 4)], max_items=2)
    assert [d.raw.title for d in selected] == ["통과"]


def test_display_order_follows_stored_score(monkeypatch):
    """둘 다 뽑히면 발송 순서는 표시되는 relevance_score 순이다. 조정은 누가 뽑히는지만 바꾼다."""
    _use_model(monkeypatch, _model_with(_stat("source", "arxiv", 0.4)))
    selected = _run_selection(monkeypatch, _TWO, max_items=2)
    assert [d.raw.title for d in selected] == ["높음", "낮음"]
    assert selected[0].analysis.notes == []
    assert len(selected[1].analysis.notes) == 1


def test_selection_survives_model_failure(monkeypatch):
    """모델 로드·계산이 터져도 조정 없이 계속 간다."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("라벨 파일 손상")

    monkeypatch.setattr(analyzer, "load_interest_labels", boom)
    selected = _run_selection(monkeypatch, _TWO)
    assert [d.raw.title for d in selected] == ["높음"]
    assert selected[0].analysis.notes == []


def test_user_id_flows_to_label_loader(monkeypatch):
    seen: list[str] = []

    def fake_load(user_id="default", path=None):
        seen.append(user_id)
        return {}

    monkeypatch.setattr(analyzer, "load_interest_labels", fake_load)
    _run_selection(monkeypatch, _TWO)
    assert seen == ["default"]


# ──────────────────────────────────────────────
# evals/interest.py 보고
# ──────────────────────────────────────────────


def test_normalize_label_preserves_user_id_only_when_given():
    assert "user_id" not in normalize_label({"url": "https://a.example/1", "human": "want"})
    kept = normalize_label({"url": "https://a.example/1", "human": "want", "user_id": " kim "})
    assert kept["user_id"] == "kim"


def test_report_learned_section_inactive():
    learned = summarize_learned(build_interest_model({}))
    assert learned["active"] is False
    assert (learned["label_count"], learned["min_labels"]) == (0, MIN_LABELS)
    summary = summarize({}, [])
    summary["learned"] = learned
    text = format_report(summary)
    assert "선정에 반영되는 학습 결과" in text
    assert f"모델 비활성 (읽고 싶다+아니다 0건 / 필요 {MIN_LABELS}건)" in text


def test_report_learned_section_active(corpus, tmp_path: Path, capsys):
    labels, records_dir = corpus
    path = _write_labels(tmp_path / "labels.jsonl", list(labels.values()))

    assert main(["report", "--labels", str(path), "--records-dir", str(records_dir), "--json"]) == 0
    learned = json.loads(capsys.readouterr().out)["learned"]
    assert learned["active"] is True
    assert learned["label_count"] == 20
    assert 0 < len(learned["raised"]) <= 5 and 0 < len(learned["lowered"]) <= 5
    assert all(row["points"] > 0 for row in learned["raised"])
    assert all(row["points"] < 0 for row in learned["lowered"])
    # 가장 크게 올리는 것과 내리는 것이 맨 앞이다.
    assert learned["raised"][0]["points"] == max(r["points"] for r in learned["raised"])
    assert learned["lowered"][0]["points"] == min(r["points"] for r in learned["lowered"])

    assert main(["report", "--labels", str(path), "--records-dir", str(records_dir)]) == 0
    text = capsys.readouterr().out
    assert "모델 활성 (읽고 싶다+아니다 20건 / 필요 20건)" in text
    assert "source:arxiv" in text and "source:hackernews" in text
