"""Discord 발송·수거 — 게이트웨이 없이 REST 배치로 도는지 검증한다.

실제 Discord API는 호출하지 않는다. httpx 레벨에서 끊고 프롬프트 조립·페이로드
구성·응답 파싱은 전부 실제 코드로 돌린다.

핵심 검증 대상:
  - 봇이 👍👎를 미리 달아두므로 **카운트 2 이상**이 사용자 입력이다
  - Poll answer_id는 1부터, 우리 선지 인덱스는 0부터 — 이 오프셋이 틀리면
    정답/오답이 통째로 뒤집힌다
  - 메시지 ↔ 아이템 매핑이 없으면 피드백을 귀속시킬 수 없다

실행: pytest tests/test_discord.py -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import app.deliverers.discord as dc
import app.deliverers.discord_polling as dp
from app.contract import item_id
from app.models import (
    ContentAnalysis,
    DigestItem,
    EvidenceLevel,
    QuizItem,
    RawContent,
    SourceType,
)

CHANNEL = "999"


def _settings(**over):
    base = dict(
        discord_bot_token="tok", discord_channel_id=CHANNEL, dry_run=False,
        max_quiz_per_digest=5,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _item(title="제목", url="https://youtu.be/A", quiz_count=1) -> DigestItem:
    return DigestItem(
        raw=RawContent(
            source_type=SourceType.YOUTUBE, source_name="채널", source_key="yt",
            title=title, url=url, body="본문",
        ),
        analysis=ContentAnalysis(
            relevance_score=7, one_line_summary="요약", tags=["T"], concepts=["개념"],
            evidence_level=EvidenceLevel.FULL, domain=["ai-ml"],
            content_type="tutorial", half_life="durable",
            actionability=7, depth=7, key_points=[], production_ideas=[],
            quiz=[
                QuizItem(question=f"문항{i}", options=["가", "나", "다", "라"],
                         answer_index=2, explanation="해설")
                for i in range(quiz_count)
            ],
        ),
    )


class _Resp:
    def __init__(self, code, payload):
        self.status_code, self._p, self.text = code, payload, json.dumps(payload)

    def json(self):
        return self._p


@pytest.fixture
def discord(monkeypatch, tmp_path):
    """REST를 대역으로 두고 상태 파일을 tmp로 격리한다."""
    sent: list[dict] = []
    reactions: list[str] = []
    counter = {"n": 0}

    async def _post(self, url, headers=None, json=None, **kw):
        counter["n"] += 1
        sent.append({"url": url, "payload": json})
        return _Resp(200, {"id": str(1000 + counter["n"])})

    async def _put(self, url, headers=None, **kw):
        reactions.append(url)
        return _Resp(204, {})

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(httpx.AsyncClient, "put", _put)
    monkeypatch.setattr(dc, "get_settings", lambda: _settings())
    monkeypatch.setattr(dp, "get_settings", lambda: _settings())
    monkeypatch.setattr(dc, "MESSAGE_MAP_PATH", tmp_path / "map.jsonl")
    monkeypatch.setattr(dp, "CURSOR_PATH", tmp_path / "cursor.json")
    return SimpleNamespace(sent=sent, reactions=reactions, tmp=tmp_path)


# ──────────────────────────────────────────────
# 발송
# ──────────────────────────────────────────────

def test_send_posts_header_items_and_polls(discord):
    ok = asyncio.run(dc.send_discord_digest([_item(quiz_count=2)]))

    assert ok is True
    kinds = ["poll" if "poll" in s["payload"] else "text" for s in discord.sent]
    assert kinds == ["text", "text", "poll", "poll"]  # 헤더 + 아이템 + 퀴즈 2


def test_bot_preseeds_both_reactions(discord):
    """봇이 미리 달아둬야 사용자가 한 번만 눌러도 된다."""
    asyncio.run(dc.send_discord_digest([_item(quiz_count=0)]))

    assert len(discord.reactions) == 2
    assert any("%F0%9F%91%8D" in r or "👍" in r for r in discord.reactions)


def test_message_map_records_item_and_quiz(discord):
    """매핑이 없으면 피드백을 어느 아이템에 귀속시킬지 알 수 없다."""
    asyncio.run(dc.send_discord_digest([_item(quiz_count=1)]))

    mapping = dc.load_message_map(discord.tmp / "map.jsonl")
    kinds = {v["kind"] for v in mapping.values()}
    assert kinds == {"item", "quiz"}
    quiz = [v for v in mapping.values() if v["kind"] == "quiz"][0]
    assert quiz["question_index"] == 0
    assert quiz["item_id"] == item_id("https://youtu.be/A")


def test_poll_truncates_long_answers(discord):
    """Discord 선지 상한은 55자다. 넘기면 400이 난다."""
    item = _item(quiz_count=1)
    item.analysis.quiz[0].options = ["가" * 200, "나", "다"]

    asyncio.run(dc.send_discord_digest([item]))

    poll = [s["payload"]["poll"] for s in discord.sent if "poll" in s["payload"]][0]
    for ans in poll["answers"]:
        assert len(ans["poll_media"]["text"]) <= dc.MAX_POLL_ANSWER


def test_send_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(dc, "get_settings", lambda: _settings(discord_bot_token=""))

    assert asyncio.run(dc.send_discord_digest([_item()])) is False


def test_dry_run_sends_nothing(discord, monkeypatch):
    monkeypatch.setattr(dc, "get_settings", lambda: _settings(dry_run=True))

    assert asyncio.run(dc.send_discord_digest([_item()])) is True
    assert discord.sent == []


# ──────────────────────────────────────────────
# 수거 — 리액션
# ──────────────────────────────────────────────

def _msg(mid, *, reactions=None, poll=None, content="", bot=True):
    m = {"id": str(mid), "author": {"bot": bot}, "content": content}
    if reactions:
        m["reactions"] = [
            {"emoji": {"name": e}, "count": c} for e, c in reactions.items()
        ]
    if poll:
        m["poll"] = poll
    return m


def test_reaction_count_one_is_bot_only(discord, monkeypatch):
    """봇이 단 1개만 있는 상태는 사용자 입력이 아니다."""
    captured = []
    monkeypatch.setattr(dp, "process_feedback", lambda p: captured.append(p))
    mapping = {"1": {"kind": "item", "url": "https://a.com/1"}}

    asyncio.run(dp._collect_reactions(
        [_msg(1, reactions={"👍": 1, "👎": 1})], mapping,
        {"likes": 0, "dislikes": 0},
    ))

    assert captured == []


def test_reaction_count_two_is_user(discord, monkeypatch):
    captured = []
    monkeypatch.setattr(dp, "process_feedback", lambda p: captured.append(p))
    mapping = {"1": {"kind": "item", "url": "https://a.com/1"}}
    summary = {"likes": 0, "dislikes": 0}

    asyncio.run(dp._collect_reactions(
        [_msg(1, reactions={"👍": 2, "👎": 1})], mapping, summary,
    ))

    assert summary["likes"] == 1 and summary["dislikes"] == 0
    assert captured[0].action == "like"


# ──────────────────────────────────────────────
# 수거 — 퀴즈 Poll
# ──────────────────────────────────────────────

def test_poll_answer_id_offset(discord, monkeypatch):
    """Discord answer_id는 1부터, 우리 인덱스는 0부터.

    이 오프셋이 틀리면 정답/오답이 통째로 뒤집힌다.
    """
    recorded = []

    def _rec(item, q, choice):
        recorded.append((item, q, choice))
        return {"correct": choice == 2}

    monkeypatch.setattr(dp, "record_answer", _rec)
    monkeypatch.setattr(dp, "load_results", lambda *a, **k: [])
    mapping = {"1": {"kind": "quiz", "item_id": "abc", "question_index": 0}}
    poll = {"results": {"answer_counts": [
        {"id": 1, "count": 0}, {"id": 2, "count": 0}, {"id": 3, "count": 1},
    ]}}
    summary = {"quiz_answers": 0, "quiz_correct": 0}

    asyncio.run(dp._collect_poll_votes(None, [_msg(1, poll=poll)], mapping, summary))

    assert recorded == [("abc", 0, 2)]      # answer_id 3 → choice_index 2
    assert summary["quiz_correct"] == 1


def test_poll_vote_not_recorded_twice(discord, monkeypatch):
    """최근 메시지를 매번 다시 읽으므로 같은 답이 반복 기록되면 안 된다."""
    monkeypatch.setattr(dp, "record_answer", lambda *a: pytest.fail("중복 기록"))
    monkeypatch.setattr(dp, "load_results", lambda *a, **k: [
        {"item_id": "abc", "question_index": 0, "choice_index": 2},
    ])
    mapping = {"1": {"kind": "quiz", "item_id": "abc", "question_index": 0}}
    poll = {"results": {"answer_counts": [{"id": 3, "count": 1}]}}

    asyncio.run(dp._collect_poll_votes(
        None, [_msg(1, poll=poll)], mapping, {"quiz_answers": 0, "quiz_correct": 0},
    ))


def test_poll_zero_votes_ignored(discord, monkeypatch):
    monkeypatch.setattr(dp, "record_answer", lambda *a: pytest.fail("표 없는데 기록"))
    monkeypatch.setattr(dp, "load_results", lambda *a, **k: [])
    mapping = {"1": {"kind": "quiz", "item_id": "abc", "question_index": 0}}
    poll = {"results": {"answer_counts": [{"id": 1, "count": 0}]}}

    asyncio.run(dp._collect_poll_votes(
        None, [_msg(1, poll=poll)], mapping, {"quiz_answers": 0, "quiz_correct": 0},
    ))


# ──────────────────────────────────────────────
# 수거 — 자연어 지시 · 커서
# ──────────────────────────────────────────────

def _poll_once(monkeypatch, messages, captured_directives):
    monkeypatch.setattr(dp, "capture_directive",
                        lambda t: captured_directives.append(t) or True)
    monkeypatch.setattr(dp, "process_feedback", lambda p: None)
    monkeypatch.setattr(dp, "load_results", lambda *a, **k: [])
    monkeypatch.setattr(dp, "load_message_map", lambda *a, **k: {})

    async def _get(client, **params):
        return messages

    monkeypatch.setattr(dp, "_get_messages", _get)
    return asyncio.run(dp.poll_once(None))


def test_human_message_becomes_directive(discord, monkeypatch):
    got = []
    summary = _poll_once(monkeypatch, [
        _msg(10, content="논문 말고 실무 사례 위주로", bot=False),
    ], got)

    assert got == ["논문 말고 실무 사례 위주로"]
    assert summary["directives"]


def test_bot_message_is_not_a_directive(discord, monkeypatch):
    """봇 자기 발송을 지시로 삼으면 매일 자기 말을 학습한다."""
    got = []
    _poll_once(monkeypatch, [_msg(10, content="📬 DS Digest", bot=True)], got)

    assert got == []


def test_slash_commands_are_not_directives(discord, monkeypatch):
    got = []
    summary = _poll_once(monkeypatch, [
        _msg(10, content="/help", bot=False),
        _msg(11, content="/keyword 인과추론", bot=False),
        _msg(12, content="/알수없음", bot=False),
    ], got)

    assert got == []
    assert summary["help_requested"] is True
    assert summary["keywords"] == ["인과추론"]


def test_directives_processed_in_chronological_order(discord, monkeypatch):
    """나중 지시가 앞선 지시를 뒤집으므로 순서가 의미를 가진다.

    Discord는 최신순으로 주므로 뒤집어 처리해야 한다.
    """
    got = []
    _poll_once(monkeypatch, [
        _msg(12, content="셋째", bot=False),
        _msg(11, content="둘째", bot=False),
        _msg(10, content="첫째", bot=False),
    ], got)

    assert got == ["첫째", "둘째", "셋째"]


def test_cursor_advances(discord, monkeypatch):
    got = []
    _poll_once(monkeypatch, [_msg(42, content="지시", bot=False)], got)

    assert json.loads((discord.tmp / "cursor.json").read_text())["last_message_id"] == "42"


def test_poll_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(dp, "get_settings", lambda: _settings(discord_channel_id=""))

    summary = asyncio.run(dp.poll_once(None))

    assert summary["directives"] == [] and summary["likes"] == 0


# ── 레이트리밋 ─────────────────────────────────────────────────────────────
# 2026-09-02 첫 실전 발송에서 한 건이 429로 조용히 사라졌다. 다이제스트는
# 헤더 1 + 아이템 5 + 퀴즈 여러 건을 연달아 쏘므로 정상적으로 걸린다.
# 아이템 메시지가 사라지면 매핑도 안 남아 그 아이템의 피드백은 영영 귀속되지 않는다.

class _RateLimitedThenOK:
    """첫 호출은 429, 다음은 성공."""

    def __init__(self):
        self.calls = 0

    async def post(self, url, headers=None, json=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return _Resp(429, {"message": "You are being rate limited.",
                               "retry_after": 0.686, "global": False})
        return _Resp(200, {"id": "999"})


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.headers = code, body, {}
        self.text = str(body)

    def json(self):
        return self._b


def test_post_retries_on_rate_limit(monkeypatch):
    import asyncio as _a
    import app.deliverers.discord as d

    monkeypatch.setattr(d, "get_settings", lambda: _settings())

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(d.asyncio, "sleep", _no_sleep)

    client = _RateLimitedThenOK()
    result = _a.run(d._post(client, {"content": "hi"}))
    assert result == {"id": "999"}
    assert client.calls == 2, "429를 재시도하지 않으면 메시지가 조용히 사라진다"


def test_post_gives_up_after_repeated_rate_limits(monkeypatch):
    import asyncio as _a
    import app.deliverers.discord as d

    monkeypatch.setattr(d, "get_settings", lambda: _settings())

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(d.asyncio, "sleep", _no_sleep)

    class _Always429:
        calls = 0

        async def post(self, *a, **k):
            _Always429.calls += 1
            return _Resp(429, {"retry_after": 0.1})

    assert _a.run(d._post(_Always429(), {"content": "hi"})) is None
    assert _Always429.calls == 3      # 무한 재시도는 하지 않는다


# ── 퀴즈 상한과 정답 노출 ──────────────────────────────────────────────────
# Discord Poll은 어느 선지가 정답인지 알려주지 않는다. 투표 결과만 보여준다.
# 정답을 따로 붙이지 않으면 맞았는지 확인할 방법이 없고, 학습용 퀴즈가 설문이 된다.

def test_quiz_caption_hides_answer_behind_spoiler():
    import app.deliverers.discord as d
    item = _item(quiz_count=1)
    caption = d._quiz_caption(item, 0, 3)
    assert caption.startswith("🧠 **퀴즈 3**")
    assert "||정답:" in caption and caption.rstrip().endswith("||")
    # 스포일러 밖에 정답이 새면 안 된다.
    before = caption.split("||")[0]
    assert item.analysis.quiz[0].options[item.analysis.quiz[0].answer_index] not in before


def test_quiz_selection_round_robins_items():
    """상한이 걸려도 모든 아이템이 최소 한 문항은 낸다."""
    import app.deliverers.discord as d
    items = [_item(title=f"i{i}", url=f"https://e.com/{i}", quiz_count=3) for i in range(4)]
    picked = d._select_quiz(items, limit=5)
    assert len(picked) == 5
    titles = {it.raw.title for it, _ in picked}
    assert titles == {"i0", "i1", "i2", "i3"}, titles
    # 앞에서부터 다 채우면 i0가 3문항을 먹고 i3는 0문항이 된다.
    assert [q for _, q in picked[:4]] == [0, 0, 0, 0]


def test_quiz_selection_respects_short_pools():
    import app.deliverers.discord as d
    items = [_item(title="a", url="https://e.com/a", quiz_count=1)]
    assert len(d._select_quiz(items, limit=5)) == 1
    assert d._select_quiz(items, limit=0) == []


def test_quiz_caption_truncation_keeps_spoiler_closed():
    """해설이 길어 잘려도 닫는 || 는 살아야 한다 — 열리면 정답이 그대로 보인다."""
    import app.deliverers.discord as d
    item = _item(quiz_count=1)
    item.analysis.quiz[0].explanation = "해설 " * 2000
    caption = d._quiz_caption(item, 0, 1)
    assert len(caption) <= d.MAX_CONTENT
    assert caption.endswith("||") and caption.count("||") == 2


# ── 지면과 Discord를 같은 물건으로 (2026-09-03) ────────────────────────────
# Discord는 HTML을 렌더링하지 않으므로 렌더러가 두 벌이다. 한쪽만 고치면 매일
# 읽는 화면과 아카이브가 조용히 갈린다.

def test_gauge_fills_proportionally():
    import app.deliverers.discord as d
    assert d._gauge("실행", 10) == "`실행 ██████████ 10`"
    assert d._gauge("깊이", 0) == "`깊이 ░░░░░░░░░░ 0`"
    assert d._gauge("실행", 7) == "`실행 ███████░░░ 7`"
    # 눈금 수는 값과 무관하게 일정해야 자리가 맞는다.
    for value in range(0, 11):
        bar = d._gauge("x", value).strip("`").split()[1]
        assert len(bar) == d._GAUGE_TICKS, (value, bar)
    # 범위 밖 값이 와도 막대가 깨지지 않는다.
    assert d._gauge("x", 99).count("█") == d._GAUGE_TICKS
    assert d._gauge("x", -5).count("░") == d._GAUGE_TICKS


def test_header_lists_today_concepts_as_chips():
    import app.deliverers.discord as d
    items = [_item(title=f"i{i}", url=f"https://e.com/{i}") for i in range(3)]
    for i, item in enumerate(items):
        item.analysis.concepts = [f"개념{i}", "공통개념"]

    header = d._format_header(items)
    assert "**오늘의 개념**" in header
    assert "`개념0`" in header and "`공통개념`" in header
    assert header.count("`공통개념`") == 1, "중복 개념은 한 번만"


def test_header_caps_concepts_and_survives_empty():
    import app.deliverers.discord as d
    many = [_item(title=f"i{i}", url=f"https://e.com/{i}") for i in range(6)]
    for i, item in enumerate(many):
        item.analysis.concepts = [f"개념{i}a", f"개념{i}b", f"개념{i}c"]
    assert len(d._today_concepts(many)) == d.MAX_HEADER_CONCEPTS

    bare = [_item()]
    bare[0].analysis.concepts = []
    header = d._format_header(bare)
    assert "**오늘의 개념**" not in header, "개념이 없으면 빈 제목을 남기지 않는다"
    assert "DS Digest" in header


def test_item_shows_two_axes_and_todo_not_relevance():
    """지면에서 관련도 점수를 빼고 두 축만 보이기로 했으므로 여기서도 뺀다."""
    import app.deliverers.discord as d
    item = _item()
    item.analysis.actionability = 8
    item.analysis.depth = 3
    item.analysis.production_ideas = ["카프카 컨슈머 배치 크기를 조정해본다"]
    item.analysis.positioning = "기존 접근은 파일 단위였고 이 논문은 토큰 단위다."

    text = d._format_item(item, 1)
    assert "`실행 ████████░░ 8`" in text and "`깊이 ███░░░░░░░ 3`" in text
    assert "관련도" not in text
    assert "**To do**" in text and "□ 카프카 컨슈머" in text
    assert "적용 아이디어" not in text
    assert "-# 배경과 위치 — 기존 접근은" in text
    # 순서: 요약 → 배경과 위치 → 계기 → 핵심 → To do
    assert text.index("배경과 위치") < text.index("`실행") < text.index("**To do**")


def test_item_without_optional_blocks_stays_clean():
    import app.deliverers.discord as d
    item = _item()
    item.analysis.production_ideas = []
    item.analysis.key_points = []
    item.analysis.positioning = None
    text = d._format_item(item, 2)
    assert "**To do**" not in text and "배경과 위치" not in text
    assert "`실행" in text, "계기는 항상 있다 — 두 축은 모든 아이템이 갖는다"
    assert "\n\n\n" not in text, "빈 블록이 빈 줄만 남기면 안 된다"
