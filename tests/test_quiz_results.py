"""퀴즈 정답 수집 — 이 시스템의 유일한 ground truth (§4.2 ⑥, §7.5).

나머지 신호는 전부 취향이라 맞고 틀림이 없다. 퀴즈만 채점 가능하고, 그래서
개념별 습득도를 계산할 수 있는 유일한 입력이다. 지금까지는 스포일러로 정답을
보여주고 사용자가 맞혔는지는 버렸다.

실행: pytest tests/test_quiz_results.py -v
"""

from __future__ import annotations

import json

import pytest

from app.contract import item_id
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    QuizItem,
    RawContent,
    SourceType,
)
from app.preferences import build_item_index
from app.quiz_results import (
    accuracy_by_tag,
    encode_callback,
    load_results,
    parse_callback,
    record_answer,
)
from app.deliverers.telegram import _format_quiz, _quiz_keyboard

URL = "https://youtu.be/QUIZVID"
TELEGRAM_CALLBACK_DATA_LIMIT = 64


@pytest.fixture
def records_dir(tmp_path):
    """퀴즈 2문항짜리 정본 한 건. 정답은 각각 2번, 0번."""
    record = {
        "date": "2026-08-01",
        "generated_at": "2026-08-01T07:10:00",
        "schema_version": 2,
        "items": [
            {
                "raw": {"url": URL, "source_key": "yt_alpha", "source_name": "Alpha"},
                "analysis": {
                    "tags": ["MLOps", "Kubernetes"],
                    "quiz": [
                        {"question": "q1", "options": ["a", "b", "c", "d"], "answer_index": 2},
                        {"question": "q2", "options": ["a", "b", "c", "d"], "answer_index": 0},
                    ],
                },
            }
        ],
    }
    (tmp_path / "digest_2026-08-01.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


# ──────────────────────────────────────────────
# callback_data 인코딩
# ──────────────────────────────────────────────

def test_callback_roundtrip():
    data = encode_callback("abc123def456", 1, 3)

    assert parse_callback(data) == ("abc123def456", 1, 3)


def test_callback_within_telegram_limit():
    """64바이트를 넘으면 Telegram이 키보드째 거부한다."""
    data = encode_callback(item_id(URL), 9, 3)

    assert len(data.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_LIMIT


@pytest.mark.parametrize(
    "bad",
    [
        "like|abc123def456",          # 다른 액션
        "quiz|abc|1",                 # 필드 부족
        "quiz|abc|1|2|3",             # 필드 초과
        "quiz||1|2",                  # 빈 id
        "quiz|abc|x|2",               # 정수 아님
        "quiz|abc|-1|2",              # 음수 문항
        "quiz|abc|1|-2",              # 음수 선택
        "",
    ],
)
def test_parse_callback_rejects_malformed(bad):
    """봇 토큰만 알면 누구나 임의 callback_data를 보낼 수 있으므로 방어적으로 판다."""
    assert parse_callback(bad) is None


# ──────────────────────────────────────────────
# 채점 · 기록
# ──────────────────────────────────────────────

def test_record_answer_marks_correct(records_dir, tmp_path):
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"

    result = record_answer(item_id(URL), 0, 2, index=index, path=out)

    assert result["correct"] is True
    assert result["answer_index"] == 2
    assert result["choice_index"] == 2
    assert result["url"] == URL
    assert result["tags"] == ["mlops", "kubernetes"]


def test_record_answer_marks_incorrect_and_keeps_choice(records_dir, tmp_path):
    """어떤 오답을 골랐는지가 남아야 한다 — 맞음/틀림 2진보다 정보가 많다."""
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"

    result = record_answer(item_id(URL), 0, 1, index=index, path=out)

    assert result["correct"] is False
    assert result["choice_index"] == 1
    assert result["answer_index"] == 2


def test_record_answer_unknown_item_returns_none(records_dir, tmp_path):
    """정답을 못 찾으면 기록하지 않는다 — 채점 불가한 줄은 라벨이 아니라 잡음이다."""
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"

    assert record_answer("없는아이템", 0, 1, index=index, path=out) is None
    assert not out.exists()


def test_record_answer_out_of_range_question_returns_none(records_dir, tmp_path):
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"

    assert record_answer(item_id(URL), 9, 1, index=index, path=out) is None


def test_record_answer_appends(records_dir, tmp_path):
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"

    record_answer(item_id(URL), 0, 2, index=index, path=out)
    record_answer(item_id(URL), 1, 0, index=index, path=out)

    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2


# ──────────────────────────────────────────────
# 읽기
# ──────────────────────────────────────────────

def test_load_results_takes_last_answer_per_question(records_dir, tmp_path):
    """답을 바꾸면 줄이 쌓인다. 최종 판단은 마지막 것이다."""
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"

    record_answer(item_id(URL), 0, 1, index=index, path=out)   # 오답
    record_answer(item_id(URL), 0, 2, index=index, path=out)   # 정답으로 정정

    results = load_results(out)

    assert len(results) == 1
    assert results[0]["correct"] is True


def test_load_results_skips_broken_line(records_dir, tmp_path):
    """한 줄 깨졌다고 라벨 전체를 잃으면 안 된다."""
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"
    record_answer(item_id(URL), 0, 2, index=index, path=out)
    with out.open("a", encoding="utf-8") as f:
        f.write("{ 깨진 줄\n")

    assert len(load_results(out)) == 1


def test_load_results_missing_file_is_empty(tmp_path):
    assert load_results(tmp_path / "없음.jsonl") == []


def test_accuracy_by_tag(records_dir, tmp_path):
    """정답률이 낮은 태그가 곧 복습이 필요한 영역이다(습득도의 첫 근사)."""
    index = build_item_index(records_dir)
    out = tmp_path / "results.jsonl"
    record_answer(item_id(URL), 0, 2, index=index, path=out)   # 정답
    record_answer(item_id(URL), 1, 3, index=index, path=out)   # 오답

    stats = accuracy_by_tag(out)

    assert stats["mlops"] == {"answered": 2, "correct": 1, "accuracy": 0.5}
    assert stats["kubernetes"]["accuracy"] == 0.5


# ──────────────────────────────────────────────
# 발송 — 번호와 버튼의 대응
# ──────────────────────────────────────────────

def _item_with_quiz(url: str, count: int) -> DigestItem:
    return DigestItem(
        raw=RawContent(
            source_type=SourceType.YOUTUBE,
            source_name="채널",
            source_key="yt",
            title="제목",
            url=url,
            body="본문",
        ),
        analysis=ContentAnalysis(
            relevance_score=7,
            one_line_summary="요약",
            tags=["MLOps"],
            evidence_level=EvidenceLevel.FULL,
            domain=["ai-ml"],
            content_type="tutorial",
            half_life="durable",
            actionability=7,
            depth=7,
            key_points=[],
            production_ideas=[],
            quiz=[
                QuizItem(
                    question=f"문항 {i}",
                    options=["a", "b", "c", "d"],
                    answer_index=i % 4,
                    explanation="해설",
                )
                for i in range(count)
            ],
        ),
    )


def test_quiz_keyboard_rows_match_question_numbers():
    """버튼 라벨의 번호가 본문의 Q번호와 맞아야 어느 문항인지 알 수 있다."""
    items = [_item_with_quiz("https://a.com/1", 2), _item_with_quiz("https://a.com/2", 1)]

    text = _format_quiz(items)
    keyboard = _quiz_keyboard(items)

    assert "Q1." in text and "Q2." in text and "Q3." in text
    rows = keyboard["inline_keyboard"]
    assert len(rows) == 3
    assert [b["text"] for b in rows[0]] == ["Q1A", "Q1B", "Q1C", "Q1D"]
    assert [b["text"] for b in rows[2]] == ["Q3A", "Q3B", "Q3C", "Q3D"]


def test_quiz_keyboard_callbacks_point_at_right_item_and_question():
    """두 번째 아이템의 첫 문항은 question_index 0이어야 한다(전역 번호가 아님)."""
    items = [_item_with_quiz("https://a.com/1", 2), _item_with_quiz("https://a.com/2", 1)]

    rows = _quiz_keyboard(items)["inline_keyboard"]

    assert parse_callback(rows[2][0]["callback_data"]) == (
        item_id("https://a.com/2"), 0, 0,
    )
    assert parse_callback(rows[1][3]["callback_data"]) == (
        item_id("https://a.com/1"), 1, 3,
    )


def test_quiz_keyboard_respects_button_cap():
    """100개를 넘기면 Telegram이 키보드째 거부한다. 넘치는 문항은 읽기 전용으로.

    모델이 아이템당 퀴즈를 3개로 제한하고 다이제스트가 5건이라 정상 경로에서는
    최대 60개다(닿지 않는다). 여기서는 상한 로직 자체를 검증한다.
    """
    items = [_item_with_quiz(f"https://a.com/{i}", 3) for i in range(10)]

    rows = _quiz_keyboard(items)["inline_keyboard"]

    assert sum(len(r) for r in rows) <= 100
    assert len(rows) == 25  # 100 / 선지 4개


def test_quiz_keyboard_none_when_no_quiz():
    assert _quiz_keyboard([_item_with_quiz("https://a.com/1", 0)]) is None
