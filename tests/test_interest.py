"""관심도 표시 모듈(evals/interest.py)의 단위 테스트. tmp_path 에 정본 2일치 가짜 파일을 만들어 돈다."""

from __future__ import annotations

from datetime import date
import json
import re
from pathlib import Path

import pytest

from evals import interest
from evals.interest import (
    HUMAN_CHOICES,
    build_page,
    format_report,
    import_labels,
    load_items,
    load_labels,
    main,
    score_band,
    summarize,
)


def _item(url: str, title: str, score: int | None = 9, source_type: str = "rss", label: str = "GeekNews", **extra):
    analysis = {
        "relevance_score": score,
        "one_line_summary": f"{title} 요약",
        "key_points": [
            {"point": "첫째 포인트", "timestamp": None},
            {"point": "둘째 포인트", "timestamp": "01:00"},
            "셋째 포인트(문자열)",
            {"point": "넷째는 잘린다", "timestamp": None},
        ],
        "tags": ["LLM", "평가", "데이터", "파이프라인", "운영", "여섯째는 잘린다"],
        "domain": ["systems"],
        "content_type": "article",
        "scores": None,
    }
    analysis.update(extra.pop("analysis", {}))
    raw = {
        "source_type": source_type,
        "source_name": label.lower(),
        "source_key": f"key-{label}",
        "source_label": label,
        "title": title,
        "url": url,
        "published_at": "2026-10-30T00:00:00",
    }
    raw.update(extra)
    return {"raw": raw, "analysis": analysis}


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    """정본 2일치: 10-30 에 3건, 10-31 에 2건(그중 하나는 10-30 과 url 중복)."""
    base = tmp_path / "records"
    base.mkdir()
    day1 = {
        "date": "2026-10-30",
        "items": [
            _item("https://a.example/1", "에이 기사", score=9),
            _item("https://b.example/2", "비 영상 <script>alert(1)</script>", score=4, source_type="youtube", label="kakao tech"),
            _item("https://c.example/3", "씨 뉴스레터", score=7, source_type="newsletter", label="Weekly"),
        ],
    }
    day2 = {
        "date": "2026-10-31",
        "items": [
            _item("https://d.example/4", "디 기사", score=9),
            _item("https://a.example/1", "에이 기사(재수록)", score=9),
            {"raw": {"title": "url 없는 아이템"}, "analysis": None},
        ],
    }
    (base / "digest_2026-10-30.json").write_text(json.dumps(day1, ensure_ascii=False), encoding="utf-8")
    (base / "digest_2026-10-31.json").write_text(json.dumps(day2, ensure_ascii=False), encoding="utf-8")
    return base


# ──────────────────────────────────────────────
# load_items
# ──────────────────────────────────────────────


def test_load_items_latest_first_and_dedupes_url(records_dir: Path):
    items = load_items(records_dir)
    urls = [item["url"] for item in items]
    # 최신 날짜(10-31)가 먼저, 날짜 안에서는 파일 순서. 중복 url 은 첫 것만, url 없는 아이템은 제외.
    assert urls == ["https://d.example/4", "https://a.example/1", "https://b.example/2", "https://c.example/3"]
    assert items[1]["title"] == "에이 기사(재수록)"
    assert items[1]["date"] == "2026-10-31"


def test_load_items_filters_window(records_dir: Path):
    only_first = load_items(records_dir, since=date(2026, 10, 30), until=date(2026, 10, 30))
    assert [item["date"] for item in only_first] == ["2026-10-30"] * 3
    assert load_items(records_dir, since=date(2026, 11, 1)) == []


def test_load_items_normalizes_fields(records_dir: Path):
    item = next(i for i in load_items(records_dir) if i["url"] == "https://b.example/2")
    assert item["key_points"] == ["첫째 포인트", "둘째 포인트", "셋째 포인트(문자열)", "넷째는 잘린다"]
    assert item["source_label"] == "kakao tech"
    assert item["relevance_score"] == 4


def test_load_items_defends_missing_fields(tmp_path: Path):
    base = tmp_path / "r"
    base.mkdir()
    payload = {"date": "2026-10-30", "items": [{"raw": {"url": "https://x.example"}, "analysis": {}}, "garbage"]}
    (base / "digest_2026-10-30.json").write_text(json.dumps(payload), encoding="utf-8")
    items = load_items(base)
    assert len(items) == 1
    assert items[0]["title"] == "(제목 없음)"
    assert items[0]["key_points"] == []
    assert items[0]["relevance_score"] is None


def test_load_items_missing_dir():
    assert load_items(Path("/nonexistent/dir/for/test")) == []


# ──────────────────────────────────────────────
# build_page
# ──────────────────────────────────────────────


def test_build_page_cards_and_radios(records_dir: Path):
    items = load_items(records_dir)
    page = build_page(items)
    assert page.count('<section class="card"') == 4
    for choice in HUMAN_CHOICES:
        assert page.count(f'value="{choice}"') == 4
    assert 'name="h-0"' in page and 'name="h-3"' in page
    assert 'maxlength="200"' in page
    assert 'target="_blank" rel="noopener"' in page


def test_build_page_escapes_and_hides_model_scores(records_dir: Path):
    page = build_page(load_items(records_dir))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    # 모델 점수는 data 속성에만 있고 보이는 텍스트에는 없다.
    assert 'data-score="9"' in page
    visible = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", page, flags=re.S)
    visible = re.sub(r"<[^>]+>", "\n", visible)
    assert not re.search(r"^\s*9\s*$", visible, flags=re.M)
    assert "관련도" not in visible and "relevance" not in visible.lower()
    assert "systems" not in visible


def test_build_page_truncates_points_and_tags(records_dir: Path):
    page = build_page(load_items(records_dir))
    assert "넷째는 잘린다" not in page
    assert "여섯째는 잘린다" not in page
    assert "셋째 포인트(문자열)" in page


def test_build_page_local_storage_and_no_external_resources(records_dir: Path):
    page = build_page(load_items(records_dir))
    assert "localStorage" in page
    assert "interest:" in page
    assert 'src="http' not in page
    # 외부 href 는 원문 링크(a 태그)만 허용한다.
    for match in re.finditer(r'<(\w+)[^>]*href="http', page):
        assert match.group(1) == "a"
    assert "<link" not in page


def test_build_page_empty():
    page = build_page([])
    assert "아이템 0건" in page
    assert "정본 아이템이 없다" in page
    assert 'id="copy"' not in page


# ──────────────────────────────────────────────
# import_labels / load_labels
# ──────────────────────────────────────────────


def test_import_labels_rejects_bad_rows_and_adds_labeled_at(tmp_path: Path):
    source = tmp_path / "labels.json"
    source.write_text(
        json.dumps(
            [
                {"url": "https://a.example/1", "date": "2026-10-31", "title": "에이", "source_key": "k", "source_type": "rss", "human": "want", "note": " 메모 "},
                {"url": "https://b.example/2", "human": "maybe"},
                {"title": "url 없음", "human": "skip"},
                "문자열",
            ]
        ),
        encoding="utf-8",
    )
    target = tmp_path / "out.jsonl"
    result = import_labels(source, target)
    assert result == {"imported": 1, "rejected": 3}
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["note"] == "메모"
    assert rows[0]["labeled_at"]
    assert "T" in rows[0]["labeled_at"]


def test_import_labels_accepts_wrapped_payload(tmp_path: Path):
    source = tmp_path / "labels.json"
    source.write_text(json.dumps({"labels": [{"url": "https://a.example/1", "human": "unsure"}]}), encoding="utf-8")
    target = tmp_path / "out.jsonl"
    assert import_labels(source, target)["imported"] == 1
    with pytest.raises(ValueError):
        source.write_text(json.dumps({"nope": 1}), encoding="utf-8")
        import_labels(source, target)


def test_load_labels_last_wins(tmp_path: Path):
    target = tmp_path / "out.jsonl"
    target.write_text(
        "\n".join(
            [
                json.dumps({"url": "https://a.example/1", "human": "want", "labeled_at": "2026-11-01T00:00:00+09:00"}),
                "not json",
                json.dumps({"url": "https://a.example/1", "human": "skip", "labeled_at": "2026-11-02T00:00:00+09:00"}),
                json.dumps({"url": "https://b.example/2", "human": "bogus"}),
            ]
        ),
        encoding="utf-8",
    )
    labels = load_labels(target)
    assert set(labels) == {"https://a.example/1"}
    assert labels["https://a.example/1"]["human"] == "skip"
    assert labels["https://a.example/1"]["labeled_at"].startswith("2026-11-02")


def test_load_labels_missing_file(tmp_path: Path):
    assert load_labels(tmp_path / "none.jsonl") == {}


# ──────────────────────────────────────────────
# summarize / format_report
# ──────────────────────────────────────────────


def test_score_band():
    assert score_band(0) == "0~5" and score_band(5) == "0~5"
    assert score_band(6) == "6~7" and score_band(7) == "6~7"
    assert score_band(8) == "8~10" and score_band(10) == "8~10"
    assert score_band(None) == "미상" and score_band(11) == "미상"


def test_summarize_counts(records_dir: Path):
    items = load_items(records_dir)
    labels = {
        "https://d.example/4": {"url": "https://d.example/4", "human": "want", "date": "2026-10-31", "title": "디 기사", "source_type": "rss"},
        "https://a.example/1": {"url": "https://a.example/1", "human": "skip", "date": "2026-10-31", "title": "에이", "source_type": "rss"},
        "https://b.example/2": {"url": "https://b.example/2", "human": "unsure", "date": "2026-10-30", "title": "비", "source_type": "youtube"},
        "https://c.example/3": {"url": "https://c.example/3", "human": "want", "date": "2026-10-30", "title": "씨", "source_type": "newsletter"},
        "https://zz.example/9": {"url": "https://zz.example/9", "human": "want", "date": "2026-10-20", "title": "정본 밖", "source_type": "rss"},
    }
    summary = summarize(labels, items)
    # 전체: want 3, skip 1, unsure 1 → 비율 3/4
    assert summary["total"] == 5
    assert (summary["want"], summary["skip"], summary["unsure"]) == (3, 1, 1)
    assert summary["want_ratio"] == 0.75
    # 소스 유형별: rss 3건(want 2 / skip 1 → 0.667). youtube·newsletter 는 1건이라 제외.
    by_type = {row["key"]: row for row in summary["by_source_type"]}
    assert set(by_type) == {"rss"}
    assert by_type["rss"]["count"] == 3 and by_type["rss"]["want_ratio"] == 0.667
    # 출처별: GeekNews(d, a) 2건 → want 1 / skip 1 = 0.5. 정본 밖 1건, 나머지 1건씩은 제외.
    by_label = {row["key"]: row for row in summary["by_source_label"]}
    assert set(by_label) == {"GeekNews"}
    assert by_label["GeekNews"]["want_ratio"] == 0.5
    # 점수 구간별: 8~10 (d=9 want, a=9 skip) → 0.5, 6~7 (c=7 want) → 1.0, 0~5 (b=4 unsure) → None, 미상(정본 밖) → 1.0
    by_band = {row["key"]: row for row in summary["by_score_band"]}
    assert [row["key"] for row in summary["by_score_band"]] == ["0~5", "6~7", "8~10", "미상"]
    assert by_band["8~10"]["want_ratio"] == 0.5
    assert by_band["6~7"]["want_ratio"] == 1.0
    assert by_band["0~5"]["want_ratio"] is None and by_band["0~5"]["unsure"] == 1
    assert by_band["미상"]["count"] == 1
    # 주차별: 10-30·10-31 은 2026-W44, 10-20 은 2026-W43
    by_week = {row["key"]: row for row in summary["by_week"]}
    assert set(by_week) == {"2026-W43", "2026-W44"}
    assert by_week["2026-W44"]["count"] == 4
    # 제목 목록은 날짜 내림차순
    assert [t["title"] for t in summary["want_titles"]] == ["디 기사", "씨", "정본 밖"]
    assert [t["title"] for t in summary["skip_titles"]] == ["에이"]


def test_summarize_empty():
    summary = summarize({}, [])
    assert summary["total"] == 0 and summary["want_ratio"] is None
    text = format_report(summary)
    assert "표시 0건" in text and "읽고 싶다 -" in text


def test_format_report_mentions_sections(records_dir: Path):
    labels = {
        "https://d.example/4": {"url": "https://d.example/4", "human": "want", "date": "2026-10-31", "title": "디 기사", "source_type": "rss"},
        "https://a.example/1": {"url": "https://a.example/1", "human": "skip", "date": "2026-10-31", "title": "에이", "source_type": "rss"},
    }
    text = format_report(summarize(labels, load_items(records_dir)))
    assert "읽고 싶다 50%" in text
    assert "relevance_score 구간별" in text
    assert "8~10: 읽고 싶다 50% (1/2" in text
    assert "[2026-10-31] 디 기사" in text
    assert "\u2014" not in text


# ──────────────────────────────────────────────
# main 왕복
# ──────────────────────────────────────────────


def test_main_roundtrip(records_dir: Path, tmp_path: Path, capsys):
    page_path = tmp_path / "interest.html"
    rc = main(["page", "--since", "2026-10-01", "--until", "2026-10-31", "--out", str(page_path), "--records-dir", str(records_dir)])
    assert rc == 0
    assert "아이템 4건" in capsys.readouterr().out
    assert page_path.exists() and page_path.read_text(encoding="utf-8").count('<section class="card"') == 4

    source = tmp_path / "copied.json"
    source.write_text(
        json.dumps(
            [
                {"url": "https://d.example/4", "date": "2026-10-31", "title": "디 기사", "source_key": "key-GeekNews", "source_type": "rss", "human": "want", "note": ""},
                {"url": "https://b.example/2", "date": "2026-10-30", "title": "비", "source_key": "k", "source_type": "youtube", "human": "skip", "note": "지루"},
                {"url": "https://c.example/3", "human": "nope"},
            ]
        ),
        encoding="utf-8",
    )
    labels_path = tmp_path / "labels.jsonl"
    rc = main(["import-labels", str(source), "--labels", str(labels_path)])
    assert rc == 0
    assert "반입 2건 (거부 1건)" in capsys.readouterr().out

    rc = main(["report", "--labels", str(labels_path), "--records-dir", str(records_dir)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "표시 2건" in text and "읽고 싶다 50%" in text
    assert "[2026-10-30] 비" in text

    rc = main(["report", "--labels", str(labels_path), "--records-dir", str(records_dir), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 2 and data["want_ratio"] == 0.5
    bands = {row["key"]: row for row in data["by_score_band"]}
    assert bands["8~10"]["want"] == 1 and bands["0~5"]["skip"] == 1


def test_main_import_bad_file(tmp_path: Path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["import-labels", str(bad), "--labels", str(tmp_path / "l.jsonl")]) == 1
    assert "반입 실패" in capsys.readouterr().err


def test_main_page_empty_records(tmp_path: Path, capsys):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    out = tmp_path / "page.html"
    rc = main(["page", "--days", "14", "--out", str(out), "--records-dir", str(empty_dir)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "정본 아이템이 없다" in captured
    assert "정본 아이템이 없다" in out.read_text(encoding="utf-8")


def test_resolve_window_days(monkeypatch):
    parser = interest.build_parser()
    args = parser.parse_args(["page", "--days", "14"])
    since, until = interest._resolve_window(args, today=date(2026, 11, 2))
    assert (since, until) == (date(2026, 10, 20), date(2026, 11, 2))
    args = parser.parse_args(["page", "--since", "2026-10-01"])
    assert interest._resolve_window(args, today=date(2026, 11, 2)) == (date(2026, 10, 1), None)
