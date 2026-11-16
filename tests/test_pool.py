"""후보 풀(app/pool.py), 선정 순수 함수(select_for_user), 미리보기 CLI 테스트.

지키려는 것은 하나다. 분석을 한 번만 하고 선정을 사용자별로 다시 해도, 같은 사용자
맥락이면 원래 발송 선정과 한 건도 다르지 않아야 한다.

실제 LLM 은 호출하지 않는다. analyze_content 는 통제된 ContentAnalysis 로 바꾸고
파일은 전부 tmp_path 에 쓴다. pytest-asyncio 가 없으므로 asyncio.run() 으로 돈다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import app.analyzer as analyzer
import app.interest_model as interest_model
import app.jobs.select_preview as preview
import app.pool as pool
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    RawContent,
    SourceType,
    UserProfile,
)
from app.pool import (
    POOL_RETENTION_DAYS,
    ScoredCandidate,
    load_pool,
    prune_pool,
    save_pool,
    snapshot_pool,
)

# (제목, source_key, relevance_score). 동점, 같은 계열 다수, 바닥값(4) 미만을 섞었다.
_SPECS: list[tuple[str, str, int]] = [
    ("hn-a", "hackernews", 8),
    ("hn-b", "hackernews", 8),
    ("hn-c", "hackernews", 7),
    ("arxiv-a", "arxiv:cs.LG", 7),
    ("arxiv-b", "arxiv:stat.ML", 6),
    ("blog-a", "https://toss.tech/rss.xml", 6),
    ("blog-b", "https://d2.naver.com/d2.atom", 5),
    ("thin-a", "hackernews", 3),
    ("thin-b", "arxiv:cs.CL", 2),
]


async def _noop_sleep(_seconds: float) -> None:
    return None


def _raw(title: str, source_key: str) -> RawContent:
    return RawContent(
        source_type=SourceType.RSS,
        source_name=source_key,
        source_key=source_key,
        title=title,
        url=f"https://example.com/{title}",
        transcript="자막 원문 " * 50,
        body="본문 원문 " * 50,
    )


def _settings(max_items: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=True,
        groq_api_key="",
        relevance_floor=4,
        max_items_per_digest=max_items,
        max_items_per_source=2,
        source_diversity_strength=1.0,
        supabase_url="",
        supabase_key="",
    )


def _patch_analyzer(monkeypatch, max_items: int = 3) -> None:
    score_by_title = {title: score for title, _, score in _SPECS}

    async def fake_analyze(item: RawContent, profile: UserProfile, **_kwargs: Any) -> ContentAnalysis:
        return ContentAnalysis(
            relevance_score=score_by_title[item.title],
            one_line_summary="요약",
            tags=[item.title.split("-")[0]],
            concepts=[f"개념 {item.title}"],
            evidence_level=EvidenceLevel.FULL,
        )

    monkeypatch.setattr(analyzer, "analyze_content", fake_analyze)
    monkeypatch.setattr(analyzer, "load_vocabulary", lambda *a, **k: {"version": 1, "concepts": {}})
    monkeypatch.setattr(analyzer, "save_vocabulary", lambda *a, **k: None)
    monkeypatch.setattr(analyzer, "weak_concepts", lambda *a, **k: set())
    monkeypatch.setattr(analyzer, "get_settings", lambda: _settings(max_items))
    monkeypatch.setattr(analyzer.asyncio, "sleep", _noop_sleep)


def _run(monkeypatch, max_items: int = 3) -> tuple[list[DigestItem], list[dict[str, Any]]]:
    """filter_and_analyze 를 돌려 (선정 목록, 풀 스냅샷)을 돌려준다."""
    _patch_analyzer(monkeypatch, max_items)
    entries: list[dict[str, Any]] = []
    items = [_raw(title, source_key) for title, source_key, _ in _SPECS]
    selected = asyncio.run(
        analyzer.filter_and_analyze(items, UserProfile(), pool_out=entries)
    )
    return selected, entries


def _use_interest_labels(monkeypatch, tmp_path: Path) -> None:
    """arxiv 는 want, hackernews 는 skip 으로 표시해 관심도 모델을 활성으로 만든다."""
    records = [
        {"url": f"https://arxiv.org/abs/{i}", "human": "want", "note": "", "source_key": "arxiv:cs.LG"}
        for i in range(10)
    ] + [
        {"url": f"https://news.ycombinator.com/item?id={i}", "human": "skip", "note": "", "source_key": "hackernews"}
        for i in range(10)
    ]
    path = tmp_path / "labels.jsonl"
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    monkeypatch.setattr(interest_model, "DEFAULT_LABELS_PATH", path)
    monkeypatch.setattr(interest_model, "RECORDS_DIR", tmp_path / "no_records")


def _urls(items: list[DigestItem]) -> list[str]:
    return [d.raw.url for d in items]


# ──────────────────────────────────────────────
# select_for_user 가 기존 선정과 같은가
# ──────────────────────────────────────────────


def test_pool_snapshot_covers_every_analyzed_candidate(monkeypatch):
    """풀에는 탈락과 바닥 미달까지 전부 들어가고, 선정 표시는 실제 선정과 같다."""
    selected, entries = _run(monkeypatch)

    assert [e["raw"]["title"] for e in entries] == [title for title, _, _ in _SPECS]
    assert len(selected) == 3
    flagged = [e["raw"]["url"] for e in entries if e["selected_default"]]
    assert sorted(flagged) == sorted(_urls(selected))
    assert all(e["absolute_score"] == e["analysis"]["relevance_score"] for e in entries)


def test_select_for_user_matches_filter_and_analyze(monkeypatch):
    """같은 후보를 select_for_user 에 직접 넣으면 같은 목록이 같은 순서로 나온다."""
    selected, _ = _run(monkeypatch)

    score_by_title = {title: score for title, _, score in _SPECS}
    scored = [
        ScoredCandidate(
            item=DigestItem(
                raw=_raw(title, source_key),
                analysis=ContentAnalysis(
                    relevance_score=score,
                    one_line_summary="요약",
                    tags=[title.split("-")[0]],
                    concepts=[f"개념 {title}"],
                    evidence_level=EvidenceLevel.FULL,
                ),
            ),
            absolute_score=score_by_title[title],
        )
        for title, source_key, score in _SPECS
    ]
    result = analyzer.select_for_user(scored, UserProfile())

    assert _urls(result.selected) == _urls(selected)
    assert result.above_floor == 7
    assert result.adjustments == {}


def test_select_for_user_does_not_mutate_candidates(monkeypatch, tmp_path: Path):
    """조정이 걸려도 select_for_user 는 아이템을 바꾸지 않는다. 노트는 호출부가 단다."""
    _use_interest_labels(monkeypatch, tmp_path)
    selected, entries = _run(monkeypatch)
    save_pool(entries, "2026-11-16", base_dir=tmp_path)

    scored = load_pool("2026-11-16", base_dir=tmp_path)
    before = [c.item.model_dump_json() for c in scored]
    result = analyzer.select_for_user(scored, UserProfile())

    assert result.adjustments, "관심도 모델이 활성인 경우를 검사해야 한다"
    assert [c.item.model_dump_json() for c in scored] == before
    # 발송본에는 노트가 달리지만 풀 스냅샷에는 없다(후처리 전에 굳힌다).
    assert any(d.analysis.notes for d in selected)
    assert all(e["analysis"]["notes"] == [] for e in entries)


# ──────────────────────────────────────────────
# 저장, 로드 왕복
# ──────────────────────────────────────────────


def test_roundtrip_reselection_matches_original(monkeypatch, tmp_path: Path):
    selected, entries = _run(monkeypatch)
    path = save_pool(entries, "2026-11-16", base_dir=tmp_path)

    assert path == tmp_path / "data" / "pool" / "pool_2026-11-16.json"
    scored = load_pool("2026-11-16", base_dir=tmp_path)
    result = analyzer.select_for_user(scored, UserProfile())

    assert _urls(result.selected) == _urls(selected)
    assert [c.item.raw.url for c in scored if c.selected_default] == [
        e["raw"]["url"] for e in entries if e["selected_default"]
    ]


def test_roundtrip_reselection_matches_with_interest_adjustment(monkeypatch, tmp_path: Path):
    """관심도 조정이 순서를 바꾸는 날에도 왕복 후 재선정이 같다."""
    plain, _ = _run(monkeypatch)
    _use_interest_labels(monkeypatch, tmp_path)
    selected, entries = _run(monkeypatch)
    assert _urls(selected) != _urls(plain), "조정이 선정을 바꾸는 경우여야 의미가 있다"

    save_pool(entries, "2026-11-16", base_dir=tmp_path)
    result = analyzer.select_for_user(load_pool("2026-11-16", base_dir=tmp_path), UserProfile())

    assert _urls(result.selected) == _urls(selected)


def test_pool_file_has_metadata_and_no_source_text(monkeypatch, tmp_path: Path):
    _, entries = _run(monkeypatch)
    path = save_pool(entries, "2026-11-16", base_dir=tmp_path)
    assert path is not None

    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["date"] == "2026-11-16"
    assert data["schema_version"] == pool.POOL_SCHEMA_VERSION
    assert data["generated_at"]
    assert len(data["items"]) == len(_SPECS)
    for entry in data["items"]:
        assert set(entry) == {"raw", "analysis", "absolute_score", "selected_default"}
        assert set(entry["raw"]) == {
            "source_type", "source_name", "source_key", "source_label",
            "title", "url", "published_at", "duration_seconds",
        }
    assert "자막 원문" not in text and "본문 원문" not in text


def test_absolute_score_survives_roundtrip_separately_from_relevance(tmp_path: Path):
    """바닥값은 혼합 전 절대 점수로 잰다. 표시 점수가 낮아도 절대 점수가 넘으면 후보다."""
    item = DigestItem(
        raw=RawContent(source_type=SourceType.RSS, source_name="s", title="t", url="https://example.com/t"),
        analysis=ContentAnalysis(relevance_score=2, one_line_summary="요약", evidence_level=EvidenceLevel.FULL),
    )
    entries = snapshot_pool([ScoredCandidate(item=item, absolute_score=6)], [item])
    save_pool(entries, "2026-11-16", base_dir=tmp_path)

    (loaded,) = load_pool("2026-11-16", base_dir=tmp_path)
    assert loaded.absolute_score == 6
    assert loaded.item.analysis.relevance_score == 2
    assert loaded.selected_default is True


# ──────────────────────────────────────────────
# 보존 기간, fail-open
# ──────────────────────────────────────────────


def test_save_prunes_files_older_than_retention(tmp_path: Path):
    directory = pool.pool_dir(tmp_path)
    directory.mkdir(parents=True)
    assert POOL_RETENTION_DAYS == 30
    # 2026-11-16 기준 30일 전은 2026-10-17 이다. 그날까지는 남고 그 전날부터 지운다.
    for name in ("pool_2026-10-16.json", "pool_2026-10-17.json", "pool_2026-11-15.json", "pool_notes.json", "keep.txt"):
        (directory / name).write_text("{}", encoding="utf-8")

    save_pool([], "2026-11-16", base_dir=tmp_path)

    assert sorted(p.name for p in directory.iterdir()) == [
        "keep.txt", "pool_2026-10-17.json", "pool_2026-11-15.json", "pool_2026-11-16.json", "pool_notes.json",
    ]


def test_prune_returns_removed_paths(tmp_path: Path):
    directory = pool.pool_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "pool_2026-01-01.json").write_text("{}", encoding="utf-8")
    removed = prune_pool("2026-11-16", base_dir=tmp_path)
    assert [p.name for p in removed] == ["pool_2026-01-01.json"]


def test_save_pool_is_fail_open(tmp_path: Path):
    """쓸 수 없는 위치여도 예외를 올리지 않는다. 풀 때문에 발송이 멈추면 안 된다."""
    blocker = tmp_path / "data"
    blocker.write_text("디렉토리 자리에 파일", encoding="utf-8")
    assert save_pool([], "2026-11-16", base_dir=tmp_path) is None


def test_load_pool_missing_file_raises(tmp_path: Path):
    try:
        load_pool("2026-11-16", base_dir=tmp_path)
    except FileNotFoundError as e:
        assert "pool_2026-11-16.json" in str(e)
    else:
        raise AssertionError("FileNotFoundError 가 나야 한다")


# ──────────────────────────────────────────────
# 미리보기 CLI
# ──────────────────────────────────────────────


def test_preview_missing_pool_exits_1(tmp_path: Path, capsys):
    code = preview.main(["--date", "2026-11-16"], base_dir=tmp_path)
    captured = capsys.readouterr()
    assert code == 1
    assert "풀 파일이 없다" in captured.err
    assert "pool_2026-11-16.json" in captured.err


def test_preview_prints_table_and_matches_stored_selection(monkeypatch, tmp_path: Path, capsys):
    selected, entries = _run(monkeypatch)
    save_pool(entries, "2026-11-16", base_dir=tmp_path)
    monkeypatch.setattr(preview, "get_settings", lambda: _settings())
    monkeypatch.setattr(preview, "PROFILE_PATH", tmp_path / "no_profile.json")

    code = preview.main(["--date", "2026-11-16", "--user-id", "default"], base_dir=tmp_path)
    out = capsys.readouterr().out

    assert code == 0
    assert "지시(directive)는 LLM 해석이 필요해 적용하지 않았다" in out
    assert "후보 9건, 바닥값(4) 이상 7건, 선정 3건" in out
    assert "저장 당시 default 선정과 같다." in out
    picked_rows = [line for line in out.splitlines() if line.startswith("선정")]
    assert len(picked_rows) == len(selected)
    assert all(any(d.raw.title in row for row in picked_rows) for d in selected)
    assert sum(line.startswith("바닥미달") for line in out.splitlines()) == 2
