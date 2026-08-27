"""공개 JSON 계약 테스트.

계약의 요점은 "내부 모델이 바뀌어도 소비자가 보는 필드가 안 바뀐다"이므로,
필드 이름·구조를 명시적으로 못박는 테스트가 곧 계약 문서다.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.contract import (
    CONTRACT_VERSION,
    build_contract,
    item_id,
    publish,
    rebuild_index,
    today_kst,
)
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    KeyPoint,
    QuizItem,
    RawContent,
    SourceType,
)


def _item(url: str = "https://example.com/a", title: str = "제목") -> DigestItem:
    return DigestItem(
        raw=RawContent(
            source_type=SourceType.YOUTUBE,
            source_name="어떤 채널",
            title=title,
            url=url,
            published_at=datetime(2026, 8, 18, 9, 0, 0),
        ),
        analysis=ContentAnalysis(
            relevance_score=9,
            one_line_summary="왜 중요한지 한 줄",
            # 모델이 evidence_level=title_only면 quiz·ideas를 떼어낸다
            # (unsupported_outputs_removed). 계약 형태를 검증하려면 근거가 있어야 한다.
            evidence_level=EvidenceLevel.FULL,
            tags=["MLOps"],
            key_points=[KeyPoint(point="핵심", timestamp="12:34")],
            production_ideas=["적용 아이디어"],
            quiz=[
                QuizItem(
                    question="질문?",
                    options=["a", "b", "c"],
                    answer_index=1,
                    explanation="해설",
                )
            ],
        ),
    )


def test_contract_shape_is_pinned():
    payload = build_contract([_item()], "2026-08-18")

    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["date"] == "2026-08-18"
    assert payload["count"] == 1
    assert payload["archive_url"].endswith("/2026-08-18.html")

    item = payload["items"][0]
    assert set(item) == {
        "id",
        "title",
        "url",
        "source",
        "published_at",
        "summary",
        "tags",
        "relevance",
        "key_points",
        "ideas",
        "quiz",
    }
    assert item["source"] == {"type": "youtube", "name": "어떤 채널"}
    assert item["summary"] == "왜 중요한지 한 줄"
    assert item["key_points"] == [{"point": "핵심", "timestamp": "12:34"}]
    assert item["quiz"][0]["answer_index"] == 1


def test_item_id_is_stable_across_title_and_url_noise():
    """제목이 바뀌어도, URL 표기가 흔들려도 같은 아이템이면 같은 id."""
    a = build_contract([_item(title="원제목")], "2026-08-18")["items"][0]["id"]
    b = build_contract([_item(title="재분석된 제목")], "2026-08-18")["items"][0]["id"]
    assert a == b
    assert item_id("https://example.com/a/") == item_id("https://EXAMPLE.com/a")


def test_internal_scoring_fields_are_not_leaked():
    """scores/notes/triplets는 튜닝 중이라 계약에 넣지 않는다."""
    item = build_contract([_item()], "2026-08-18")["items"][0]
    for leaked in ("scores", "notes", "triplets", "analysis", "raw"):
        assert leaked not in item


def test_publish_writes_three_files(tmp_path):
    docs = tmp_path / "docs"
    paths = publish([_item()], "2026-08-18", docs)

    assert (docs / "2026-08-18.json").exists()
    assert (docs / "latest.json").exists()
    assert (docs / "index.json").exists()

    latest = json.loads(paths["latest"].read_text(encoding="utf-8"))
    dated = json.loads(paths["dated"].read_text(encoding="utf-8"))
    assert latest == dated


def test_index_lists_dates_newest_first(tmp_path):
    docs = tmp_path / "docs"
    publish([_item()], "2026-08-16", docs)
    publish([_item()], "2026-08-18", docs)
    publish([_item()], "2026-08-17", docs)

    index = json.loads(rebuild_index(docs).read_text(encoding="utf-8"))
    assert index["dates"] == ["2026-08-18", "2026-08-17", "2026-08-16"]
    assert index["latest"] == "2026-08-18"


def test_index_ignores_non_date_json(tmp_path):
    """latest.json·index.json 자신이 목록에 섞이면 안 된다."""
    docs = tmp_path / "docs"
    publish([_item()], "2026-08-18", docs)
    index = json.loads((docs / "index.json").read_text(encoding="utf-8"))
    assert index["dates"] == ["2026-08-18"]


def test_today_kst_is_ahead_of_utc_date_in_early_morning():
    """KST 날짜는 UTC 날짜보다 앞서거나 같다 — 절대 뒤처지지 않는다."""
    from datetime import timezone

    kst = today_kst()
    utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert kst >= utc
