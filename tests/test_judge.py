"""LLM 심판 모듈(evals/judge.py)의 단위 테스트. 네트워크 호출 없이 가짜 transport 로 돈다."""

from __future__ import annotations

from datetime import date
import io
import json
from pathlib import Path

import pytest

from evals import judge
from evals.judge import (
    PROMPT_HASH,
    JudgeError,
    analysis_hash,
    build_report,
    build_target,
    build_verdicts,
    call_judge,
    find_disagreements,
    load_cache,
    parse_json_response,
    run_judge,
    verify_quote,
)

BODY = (
    "TestPrism 데이터셋은 300개 과제와 3000개 구현을 제공한다. "
    "Joint Success Function 은 초기 상태에서 실패하고 유효 후보를 모두 통과시켜야 한다. "
    "TestHelix 는 이질적 합성과 피어 교차 검증을 쓴다."
)


def _analysis(**overrides):
    analysis = {
        "relevance_score": 8,
        "one_line_summary": "다중 정답을 반영해 테스트 품질을 측정한다",
        "tags": ["LLM"],
        "key_points": [
            {"point": "TestPrism 은 300개 과제를 제공한다", "timestamp": None},
            {"point": "JSF 는 초기 상태에서 실패해야 한다", "timestamp": None},
        ],
        "production_ideas": ["JSF 를 ETL 검증에 적용한다", "피어 교차 검증을 UI 테스트에 쓴다"],
        "quiz": [
            {
                "question": "TestPrism 의 과제 수는?",
                "options": ["100", "300", "3000"],
                "answer_index": 1,
                "explanation": "300개",
            }
        ],
        "evidence_level": "full",
        "skip_reason": None,
    }
    analysis.update(overrides)
    return analysis


def _entry(url="https://example.com/a", evidence_level="full", body=BODY, **analysis_overrides):
    return {
        "raw": {
            "source_type": "article",
            "source_name": "src",
            "source_key": "src",
            "source_label": "src",
            "title": "제목 " + url.rsplit("/", 1)[-1],
            "url": url,
            "published_at": "2026-10-12T00:00:00",
            "transcript": "",
            "body": body,
            "duration_seconds": None,
        },
        "analysis": _analysis(evidence_level=evidence_level, **analysis_overrides),
    }


def _write_record(records_dir: Path, day: str, entries: list[dict]) -> None:
    records_dir.mkdir(parents=True, exist_ok=True)
    record = {"date": day, "generated_at": f"{day}T00:00:00", "schema_version": 2, "items": entries}
    (records_dir / f"digest_{day}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def _good_answer(*, idea_quotes=None, kp_quotes=None, summary_quote=None, quiz_quote=None) -> dict:
    """본문에 실제로 있는 구절을 인용하는 모범 응답."""
    idea_quotes = idea_quotes or ["Joint Success Function 은 초기 상태에서 실패하고", "피어 교차 검증을 쓴다"]
    kp_quotes = kp_quotes or ["300개 과제와 3000개 구현을 제공한다", "초기 상태에서 실패하고"]
    return {
        "summary": {"faithful": True, "unsupported_claim": None, "quote": summary_quote or "300개 과제와 3000개 구현"},
        "key_points": [{"index": i, "supported": True, "quote": q} for i, q in enumerate(kp_quotes)],
        "production_ideas": [{"index": i, "grounded": True, "quote": q} for i, q in enumerate(idea_quotes)],
        "quiz": [{"index": 0, "answerable": True, "answer_correct": True, "quote": quiz_quote or "300개 과제"}],
    }


def _ok_response(content, usage=None) -> dict:
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {
        "status_code": 200,
        "json": {
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        },
        "text": "",
    }


class FakeTransport:
    """호출 순서대로 미리 준비한 응답을 돌려주고, 받은 요청을 기록한다."""

    def __init__(self, responses=None, *, answer=None):
        self.responses = list(responses or [])
        self.answer = answer
        self.requests: list[dict] = []

    def __call__(self, request: dict) -> dict:
        self.requests.append(json.loads(json.dumps(request)))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return _ok_response(self.answer or _good_answer())


def _no_sleep(_seconds: float) -> None:
    return None


def _run(judge_id, tmp_path, transport, **kwargs):
    out = io.StringIO()
    summary = run_judge(
        judge_id,
        transport=transport,
        cache_path=tmp_path / "cache" / "judgments.jsonl",
        records_dir=tmp_path / "records",
        sleep=_no_sleep,
        out=out,
        **kwargs,
    )
    return summary, out.getvalue()


# (1) 인용 검증 ------------------------------------------------------------


def test_verify_quote_tolerates_whitespace_and_rejects_missing():
    body = "Joint   Success\n Function 은 초기 상태에서\t실패한다."
    assert verify_quote("Joint Success Function 은 초기 상태에서 실패한다", body)
    assert verify_quote("  Joint Success Function  ", body)
    assert not verify_quote("Joint Success Function 은 항상 성공한다", body)
    assert not verify_quote(None, body)
    assert not verify_quote("", body)
    assert not verify_quote("   ", body)


def test_build_verdicts_marks_quote_verified_and_verified_separately():
    target = build_target("2026-10-12", _entry())
    parsed = _good_answer(idea_quotes=["Joint Success Function 은 초기 상태에서 실패하고", "본문에 없는 구절"])
    parsed["key_points"][1]["supported"] = False
    verdicts = build_verdicts(parsed, target)

    ideas = verdicts["production_ideas"]
    assert ideas[0]["grounded"] and ideas[0]["quote_verified"] and ideas[0]["verified"]
    # 심판은 grounded 라고 했지만 인용이 본문에 없다 → claimed 는 남고 verified 는 거짓
    assert ideas[1]["grounded"] and not ideas[1]["quote_verified"] and not ideas[1]["verified"]
    # 인용은 맞지만 심판이 supported=false 라고 했다 → verified 거짓
    kp = verdicts["key_points"][1]
    assert kp["quote_verified"] and not kp["supported"] and not kp["verified"]
    assert verdicts["summary"]["verified"]
    assert verdicts["quiz"][0]["verified_answerable"] and verdicts["quiz"][0]["verified_answer_correct"]


def test_build_verdicts_aligns_by_index_and_pads_missing():
    target = build_target("2026-10-12", _entry())
    parsed = _good_answer()
    parsed["key_points"] = [{"index": 1, "supported": True, "quote": "초기 상태에서 실패하고"}]  # 0번 누락
    parsed["quiz"] = "이상한 값"
    verdicts = build_verdicts(parsed, target)
    assert [e["index"] for e in verdicts["key_points"]] == [0, 1]
    assert not verdicts["key_points"][0]["supported"] and verdicts["key_points"][0]["quote"] is None
    assert verdicts["key_points"][1]["verified"]
    assert len(verdicts["quiz"]) == 1 and not verdicts["quiz"][0]["answerable"]


# (2) evidence_level 이 full 이 아니면 호출하지 않고 skipped 로 캐시 ------------


def test_non_full_items_are_skipped_without_calling(tmp_path):
    _write_record(
        tmp_path / "records",
        "2026-10-12",
        [
            _entry("https://example.com/full"),
            _entry("https://example.com/desc", evidence_level="description"),
            _entry("https://example.com/partial", evidence_level="partial"),
        ],
    )
    transport = FakeTransport()
    summary, _ = _run("deepseek", tmp_path, transport)

    assert len(transport.requests) == 1
    assert transport.requests[0]["json"]["messages"][1]["content"].count("example.com") == 0
    assert summary["succeeded"] == 1 and summary["skipped"] == 2 and summary["failed"] == 0

    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    skipped = {r["url"]: r for r in rows if r["skipped"]}
    assert set(skipped) == {"https://example.com/desc", "https://example.com/partial"}
    assert skipped["https://example.com/desc"]["reason"] == "evidence_level=description"
    assert skipped["https://example.com/desc"]["verdicts"] is None

    # 재실행: skipped 행도 캐시 적중이라 다시 기록하지 않고, 호출도 없다
    transport2 = FakeTransport()
    summary2, _ = _run("deepseek", tmp_path, transport2)
    assert transport2.requests == []
    assert summary2["skipped"] == 0 and summary2["cache_hits"] == 3
    assert len(load_cache(tmp_path / "cache" / "judgments.jsonl")) == 3


def test_truncation_flag_and_body_limit(tmp_path, monkeypatch):
    long_body = "가" * 50 + " 끝문장"
    target = build_target("2026-10-12", _entry(body=long_body), max_chars=20)
    assert target["truncated"] and len(target["body"]) == 20

    _write_record(tmp_path / "records", "2026-10-12", [_entry(body=long_body)])
    monkeypatch.setattr(judge, "JUDGE_MAX_CHARS", 20)
    transport = FakeTransport(answer={"summary": {"faithful": False, "unsupported_claim": "x", "quote": None}})
    _run("deepseek", tmp_path, transport)
    sent_body = transport.requests[0]["json"]["messages"][1]["content"]
    assert "끝문장" not in sent_body and "가" * 20 in sent_body
    row = load_cache(tmp_path / "cache" / "judgments.jsonl")[0]
    assert row["truncated"] is True
    assert row["verdicts"]["summary"]["unsupported_claim"] == "x" and row["verdicts"]["summary"]["verified"] is False


# (3) 캐시 적중과 error 재시도 -------------------------------------------------


def test_cache_hit_skips_transport_and_error_rows_are_retried(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry("https://example.com/a"), _entry("https://example.com/b")])
    cache = tmp_path / "cache" / "judgments.jsonl"

    first = FakeTransport()
    summary, _ = _run("deepseek", tmp_path, first)
    assert len(first.requests) == 2 and summary["succeeded"] == 2

    second = FakeTransport()
    summary, _ = _run("deepseek", tmp_path, second)
    assert second.requests == []
    assert summary["cache_hits"] == 2 and summary["to_call"] == 0

    # b 에 error 행을 덧붙이면(최신 행이 error) 그 건만 다시 호출한다
    rows = load_cache(cache)
    error_row = dict(next(r for r in rows if r["url"] == "https://example.com/b"))
    error_row.update({"error": "HTTP 401: nope", "verdicts": None, "usage": None})
    with cache.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(error_row, ensure_ascii=False) + "\n")

    third = FakeTransport()
    summary, _ = _run("deepseek", tmp_path, third)
    assert len(third.requests) == 1
    assert "https://example.com/b" not in third.requests[0]["json"]["messages"][1]["content"]
    assert summary["cache_hits"] == 1 and summary["succeeded"] == 1

    # 다른 심판으로 돌리면 키가 달라 다시 호출한다
    qwen = FakeTransport()
    summary, _ = _run("qwen", tmp_path, qwen)
    assert len(qwen.requests) == 2
    assert all(r["json"]["model"] == "qwen-flash" for r in qwen.requests)
    assert all("dashscope" in r["url"] for r in qwen.requests)


def test_cache_path_env_var_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_CACHE_PATH", str(tmp_path / "env" / "j.jsonl"))
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    run_judge("deepseek", transport=FakeTransport(), records_dir=tmp_path / "records", sleep=_no_sleep, out=io.StringIO())
    assert (tmp_path / "env" / "j.jsonl").exists()
    assert len(load_cache()) == 1


def test_dry_run_counts_without_calling(tmp_path):
    _write_record(
        tmp_path / "records",
        "2026-10-12",
        [_entry("https://example.com/a"), _entry("https://example.com/b", evidence_level="title_only")],
    )
    transport = FakeTransport()
    summary, output = _run("deepseek", tmp_path, transport, dry_run=True)
    assert transport.requests == []
    assert summary["targets"] == 2 and summary["full"] == 1 and summary["full_ratio"] == 0.5
    assert summary["to_call"] == 1 and summary["estimated_tokens"] == len(BODY) // 2
    assert "대상 2건" in output and "예상 입력 토큰" in output
    assert not (tmp_path / "cache" / "judgments.jsonl").exists()


def test_run_respects_since_until_and_limit(tmp_path):
    _write_record(tmp_path / "records", "2026-10-10", [_entry("https://example.com/old")])
    _write_record(tmp_path / "records", "2026-10-12", [_entry("https://example.com/a"), _entry("https://example.com/b")])
    transport = FakeTransport()
    summary, _ = _run("deepseek", tmp_path, transport, since=date(2026, 10, 11), limit=1)
    assert summary["targets"] == 2 and len(transport.requests) == 1


# (4) 코드펜스 JSON 파싱 ------------------------------------------------------


def test_parse_json_response_strips_code_fence():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('```\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('{"a": 1}') == {"a": 1}
    assert parse_json_response('결과입니다:\n{"a": {"b": 2}}\n끝') == {"a": {"b": 2}}
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_json_response("JSON 아님")
    with pytest.raises(ValueError):
        parse_json_response("[1, 2]")


def test_call_judge_parses_fenced_content():
    transport = FakeTransport([_ok_response("```json\n" + json.dumps(_good_answer()) + "\n```")])
    parsed, usage = call_judge(judge.JUDGES["deepseek"], "prompt", api_key="k", transport=transport, sleep=_no_sleep)
    assert parsed["summary"]["faithful"] is True
    assert usage["total_tokens"] == 120


# (5) 429 재시도와 4xx 즉시 실패 ---------------------------------------------


def test_call_judge_retries_on_429_and_5xx_then_succeeds():
    sleeps: list[float] = []
    transport = FakeTransport(
        [
            {"status_code": 429, "json": None, "text": "rate limited"},
            {"status_code": 503, "json": None, "text": "busy"},
            _ok_response(_good_answer()),
        ]
    )
    parsed, _ = call_judge(
        judge.JUDGES["deepseek"], "p", api_key="k", transport=transport, sleep=sleeps.append
    )
    assert parsed["summary"]["faithful"] is True
    assert len(transport.requests) == 3
    assert sleeps == [1.0, 2.0]  # 지수 백오프


def test_call_judge_gives_up_after_max_retries():
    transport = FakeTransport([{"status_code": 429, "json": None, "text": "x"}] * 10)
    with pytest.raises(JudgeError, match="HTTP 429"):
        call_judge(judge.JUDGES["deepseek"], "p", api_key="k", transport=transport, sleep=_no_sleep)
    assert len(transport.requests) == judge.MAX_RETRIES + 1


def test_call_judge_4xx_drops_response_format_once_then_fails():
    transport = FakeTransport(
        [
            {"status_code": 400, "json": None, "text": "response_format unsupported"},
            {"status_code": 401, "json": None, "text": "bad key"},
        ]
    )
    with pytest.raises(JudgeError, match="HTTP 401"):
        call_judge(judge.JUDGES["deepseek"], "p", api_key="k", transport=transport, sleep=_no_sleep)
    assert len(transport.requests) == 2
    assert "response_format" in transport.requests[0]["json"]
    assert "response_format" not in transport.requests[1]["json"]
    assert transport.requests[0]["json"]["temperature"] == 0
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer k"


def test_run_records_4xx_as_error_row_and_retries_next_time(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    failing = FakeTransport(
        [
            {"status_code": 400, "json": None, "text": "no"},
            {"status_code": 404, "json": None, "text": "not found"},
        ]
    )
    summary, output = _run("deepseek", tmp_path, failing)
    assert summary["failed"] == 1 and summary["succeeded"] == 0 and summary["aborted"] is None
    assert "error" in output
    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    assert len(rows) == 1 and rows[0]["error"].startswith("HTTP 404")
    assert rows[0]["verdicts"] is None and rows[0]["skipped"] is False

    retry = FakeTransport()
    summary, _ = _run("deepseek", tmp_path, retry)
    assert len(retry.requests) == 1 and summary["succeeded"] == 1
    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    assert len(rows) == 2 and rows[-1]["error"] is None
    assert rows[-1]["usage"]["total_tokens"] == 120


def test_run_row_shape(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    _run("deepseek", tmp_path, FakeTransport())
    row = load_cache(tmp_path / "cache" / "judgments.jsonl")[0]
    expected = {
        "judge_id", "model", "prompt_hash", "date", "url", "title", "analysis_hash", "judged_at",
        "skipped", "reason", "error", "truncated", "usage", "verdicts",
    }
    assert expected <= set(row)
    assert row["judge_id"] == "deepseek" and row["model"] == "deepseek-flash"
    assert row["prompt_hash"] == PROMPT_HASH and len(row["prompt_hash"]) == 12
    assert set(row["verdicts"]) == {"summary", "key_points", "production_ideas", "quiz"}


# (6) report 지표: verified 만 센다 ------------------------------------------


def test_report_counts_only_verified(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry("https://example.com/a")])
    # 아이디어 2개 모두 grounded=true 라고 주장하지만 두 번째 인용은 본문에 없다.
    # 퀴즈는 answerable/answer_correct 모두 true 인데 인용이 없다.
    answer = _good_answer(idea_quotes=["Joint Success Function 은 초기 상태에서 실패하고", "없는 구절"])
    answer["quiz"][0]["quote"] = None
    _run("deepseek", tmp_path, FakeTransport(answer=answer))

    report = build_report(load_cache(tmp_path / "cache" / "judgments.jsonl"), window_days=14, until=date(2026, 10, 14))
    block = report["judges"]["deepseek"]
    window = block["window"]
    assert window["judged_count"] == 1 and window["skipped_count"] == 0 and window["error_count"] == 0
    assert window["grounded_idea_rate"] == 0.5
    assert window["quiz_answerable_rate"] == 0.0 and window["quiz_answer_correct_rate"] == 0.0
    assert window["summary_faithful_rate"] == 1.0
    assert window["key_point_support_rate"] == 1.0
    # 인용 5개(요약 1 + 포인트 2 + 아이디어 2, 퀴즈는 null) 중 4개가 본문에 있다
    assert window["quote_verified_rate"] == 0.8
    assert "2026-W42" in block["weeks"]
    assert block["weeks"]["2026-W42"]["judged_count"] == 1
    assert report["agreement"]["overall"] is None


def test_report_window_excludes_old_rows_and_null_on_zero_denominator(tmp_path):
    _write_record(tmp_path / "records", "2026-09-01", [_entry("https://example.com/old", production_ideas=[], quiz=[])])
    _run("deepseek", tmp_path, FakeTransport(answer=_good_answer(idea_quotes=[])))
    report = build_report(load_cache(tmp_path / "cache" / "judgments.jsonl"), window_days=14, until=date(2026, 10, 14))
    block = report["judges"]["deepseek"]
    assert block["window"]["judged_count"] == 0
    assert block["window"]["summary_faithful_rate"] is None
    assert block["overall"]["judged_count"] == 1
    assert block["overall"]["grounded_idea_rate"] is None
    assert block["overall"]["quiz_answerable_rate"] is None


def test_report_uses_latest_row_per_key(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    failing = FakeTransport([{"status_code": 403, "json": None, "text": "x"}] * 2)
    _run("deepseek", tmp_path, failing)
    _run("deepseek", tmp_path, FakeTransport())
    report = build_report(load_cache(tmp_path / "cache" / "judgments.jsonl"), until=date(2026, 10, 14))
    overall = report["judges"]["deepseek"]["overall"]
    assert overall["judged_count"] == 1 and overall["error_count"] == 0


# (7) 두 심판 agreement 와 disagreements ---------------------------------------


def test_agreement_and_disagreements_between_two_judges(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry("https://example.com/a"), _entry("https://example.com/b")])
    _run("deepseek", tmp_path, FakeTransport(answer=_good_answer()))

    # qwen 은 두 아이템 모두 두 번째 아이디어를 grounded=false, 퀴즈 answer_correct=false 라고 본다
    qwen_answer = _good_answer()
    qwen_answer["production_ideas"][1] = {"index": 1, "grounded": False, "quote": None}
    qwen_answer["quiz"][0]["answer_correct"] = False
    _run("qwen", tmp_path, FakeTransport(answer=qwen_answer))

    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    report = build_report(rows, until=date(2026, 10, 14))
    agreement = report["agreement"]["overall"]
    assert agreement["pair"] == ["deepseek", "qwen"]
    assert agreement["items_both_judged"] == 2
    by_q = agreement["by_question"]
    assert by_q["summary"] == {"compared": 2, "agreed": 2, "rate": 1.0}
    assert by_q["key_points"] == {"compared": 4, "agreed": 4, "rate": 1.0}
    assert by_q["production_ideas"] == {"compared": 4, "agreed": 2, "rate": 0.5}
    assert by_q["quiz_answerable"] == {"compared": 2, "agreed": 2, "rate": 1.0}
    assert by_q["quiz_answer_correct"] == {"compared": 2, "agreed": 0, "rate": 0.0}
    assert agreement["overall_rate"] == round(10 / 14, 6)

    targets = judge.collect_targets(tmp_path / "records")
    items = find_disagreements(rows, targets)
    assert [i["url"] for i in items] == ["https://example.com/a", "https://example.com/b"]
    first = items[0]
    assert first["date"] == "2026-10-12" and first["title"] == "제목 a"
    kinds = [(q["question"], q["index"]) for q in first["questions"]]
    assert kinds == [("production_ideas", 1), ("quiz", 0)]
    idea_q = first["questions"][0]
    assert idea_q["text"] == "피어 교차 검증을 UI 테스트에 쓴다"
    assert idea_q["verdicts"]["deepseek"]["grounded"] is True
    assert idea_q["verdicts"]["deepseek"]["quote"] == "피어 교차 검증을 쓴다"
    assert idea_q["verdicts"]["qwen"]["grounded"] is False
    assert idea_q["verdicts"]["qwen"]["quote"] is None
    assert idea_q["human"] is None
    # 본문 발췌는 검증된 인용 주변을 담는다
    assert "피어 교차 검증을 쓴다" in idea_q["excerpt"]
    quiz_q = first["questions"][1]
    assert quiz_q["verdicts"]["qwen"]["answer_correct"] is False and quiz_q["verdicts"]["qwen"]["answerable"] is True


def test_disagreements_empty_when_judges_agree_or_only_one_judge(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    _run("deepseek", tmp_path, FakeTransport())
    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    assert find_disagreements(rows, judge.collect_targets(tmp_path / "records")) == []
    _run("qwen", tmp_path, FakeTransport())
    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    assert find_disagreements(rows, judge.collect_targets(tmp_path / "records")) == []


def test_body_excerpt_falls_back_to_head():
    body = "앞부분 " * 200
    excerpt = judge.body_excerpt(body, ["없는 구절", None])
    assert excerpt == judge.normalize_ws(body)[:600]
    assert judge.body_excerpt("", ["x"]) is None


# (8) analysis_hash ----------------------------------------------------------


def test_analysis_hash_is_order_insensitive_and_value_sensitive():
    a = {"one_line_summary": "요약", "tags": ["A", "B"], "quiz": [{"q": 1, "r": 2}]}
    b = {"quiz": [{"r": 2, "q": 1}], "tags": ["A", "B"], "one_line_summary": "요약"}
    assert analysis_hash(a) == analysis_hash(b)
    assert len(analysis_hash(a)) == 12
    c = dict(a, one_line_summary="요약!")
    assert analysis_hash(a) != analysis_hash(c)
    d = dict(a, tags=["B", "A"])  # 리스트 순서는 값이다
    assert analysis_hash(a) != analysis_hash(d)


def test_analysis_change_invalidates_cache(tmp_path):
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    _run("deepseek", tmp_path, FakeTransport())
    _write_record(tmp_path / "records", "2026-10-12", [_entry(one_line_summary="바뀐 요약")])
    transport = FakeTransport()
    summary, _ = _run("deepseek", tmp_path, transport)
    assert len(transport.requests) == 1 and summary["cache_hits"] == 0


# 프롬프트와 CLI ---------------------------------------------------------------


def test_prompt_hash_and_system_prompt_rules():
    assert len(PROMPT_HASH) == 12
    assert "글자 그대로 복사" in judge.SYSTEM_PROMPT
    assert "null" in judge.SYSTEM_PROMPT
    target = build_target("2026-10-12", _entry())
    prompt = judge.build_user_prompt(target)
    assert BODY in prompt
    assert "0. TestPrism 은 300개 과제를 제공한다" in prompt
    assert "제시된 정답: (1) 300" in prompt


def test_judges_definition_and_model_override(monkeypatch):
    assert set(judge.JUDGES) == {"deepseek", "qwen"}
    for entry in judge.JUDGES.values():
        assert {"id", "base_url", "model", "model_env", "api_key_env"} <= set(entry)
    monkeypatch.setenv("JUDGE_QWEN_MODEL", "qwen-plus")
    assert judge.resolve_model(judge.JUDGES["qwen"]) == "qwen-plus"
    monkeypatch.delenv("JUDGE_QWEN_MODEL")
    assert judge.resolve_model(judge.JUDGES["qwen"]) == "qwen-flash"


def test_run_without_api_key_and_transport_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    with pytest.raises(SystemExit):
        run_judge("deepseek", records_dir=tmp_path / "records", cache_path=tmp_path / "c.jsonl", out=io.StringIO())


def test_cli_report_and_disagreements_run_on_empty_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JUDGE_CACHE_PATH", str(tmp_path / "none.jsonl"))
    assert judge.main(["report"]) == 0
    assert "판정 행이 없다" in capsys.readouterr().out
    assert judge.main(["report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["judges"] == {}
    out_path = tmp_path / "d.json"
    assert judge.main(["disagreements", "--out", str(out_path)]) == 0
    assert json.loads(out_path.read_text(encoding="utf-8")) == []


# 독립 검증 반영 (수정 전 FAIL) ---------------------------------------------


def test_parse_json_response_keeps_fence_inside_quoted_string():
    # (1) 인용문 안에 ``` 가 들어간 순수 JSON. 펜스 정규식이 먼저 걸리면 깨진다.
    raw = json.dumps({"summary": {"faithful": True, "quote": "코드는 ```python\\nprint(1)``` 로 쓴다"}}, ensure_ascii=False)
    parsed = parse_json_response(raw)
    assert "```python" in parsed["summary"]["quote"]
    assert parse_json_response('{"a": "```"}') == {"a": "```"}


def test_parse_json_response_recovers_from_leading_prose_with_braces():
    # (1) 설명문에 중괄호가 있어도 마지막 JSON 객체를 복구한다
    assert parse_json_response('결과 {참고}: {"a": 1}') == {"a": 1}
    assert parse_json_response('설명 {x} 뒤\n```json\n{"b": [1, 2]}\n```\n끝 }') == {"b": [1, 2]}


def test_report_filters_other_prompt_and_stale_analysis_rows(tmp_path):
    # (2) 옛 prompt_hash 행과 analysis 가 바뀐 옛 행은 기본 보고에서 제외한다
    _write_record(tmp_path / "records", "2026-10-12", [_entry("https://example.com/a")])
    cache = tmp_path / "cache" / "judgments.jsonl"
    _run("deepseek", tmp_path, FakeTransport())
    rows = load_cache(cache)
    old_prompt = dict(rows[0], prompt_hash="000000000000")
    stale = dict(rows[0], analysis_hash="deadbeef0000", date="2026-10-11")
    with cache.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old_prompt, ensure_ascii=False) + "\n")
        handle.write(json.dumps(stale, ensure_ascii=False) + "\n")

    current = judge.current_item_keys(tmp_path / "records")
    assert current == {("https://example.com/a", rows[0]["analysis_hash"])}
    report = build_report(load_cache(cache), current_keys=current)
    assert report["prompt_hash"] == PROMPT_HASH
    assert report["judges"]["deepseek"]["overall"]["judged_count"] == 1
    assert report["filtered"] == {"other_prompt": 1, "stale_analysis": 1, "kept": 1}

    # --all-prompts: 프롬프트 필터만 풀린다(정본 교차는 유지)
    report_all = build_report(load_cache(cache), current_keys=current, prompt_hash=None)
    assert report_all["prompt_hash"] is None
    assert report_all["judges"]["deepseek"]["overall"]["judged_count"] == 2
    assert report_all["filtered"]["other_prompt"] == 0 and report_all["filtered"]["stale_analysis"] == 1


def test_same_url_with_two_analyses_in_records_keeps_both_rows(tmp_path):
    # (2b) 정본에 같은 url 이 다른 날짜에 다른 analysis 로 들어간 경우 키는 (url, analysis_hash) 쌍
    url = "https://example.com/dup"
    _write_record(tmp_path / "records", "2026-10-10", [_entry(url)])
    _write_record(tmp_path / "records", "2026-10-12", [_entry(url, one_line_summary="다른 요약")])
    _run("deepseek", tmp_path, FakeTransport())
    current = judge.current_item_keys(tmp_path / "records")
    assert len(current) == 2
    report = build_report(load_cache(tmp_path / "cache" / "judgments.jsonl"), current_keys=current)
    assert report["judges"]["deepseek"]["overall"]["judged_count"] == 2
    assert report["filtered"]["stale_analysis"] == 0


def test_agreement_does_not_compare_across_prompt_hashes(tmp_path):
    # (2c) 다른 프롬프트로 판정한 두 심판은 비교하지 않는다
    _write_record(tmp_path / "records", "2026-10-12", [_entry()])
    cache = tmp_path / "cache" / "judgments.jsonl"
    _run("deepseek", tmp_path, FakeTransport())
    _run("qwen", tmp_path, FakeTransport())
    rows = load_cache(cache)
    rows[1] = dict(rows[1], prompt_hash="000000000000")
    assert judge.compute_agreement(rows) is None
    assert find_disagreements(rows, judge.collect_targets(tmp_path / "records")) == []


def test_report_window_is_anchored_on_last_judged_date(tmp_path):
    # (3) 창 기준은 오늘이 아니라 데이터의 마지막 날짜. 마지막 판정일 09-20, 오늘 10-14 → 창에 1건
    _write_record(tmp_path / "records", "2026-09-20", [_entry()])
    _run("deepseek", tmp_path, FakeTransport())
    report = build_report(load_cache(tmp_path / "cache" / "judgments.jsonl"), window_days=14)
    assert report["window"] == {"days": 14, "since": "2026-09-07", "until": "2026-09-20"}
    assert report["judges"]["deepseek"]["window"]["judged_count"] == 1


def test_bool_parsing_rejects_string_false_and_odd_types():
    # (4)
    assert judge.as_bool(True) is True and judge.as_bool(False) is False
    assert judge.as_bool("true") is True and judge.as_bool("Yes") is True
    assert judge.as_bool("false") is False and judge.as_bool("no") is False
    assert judge.as_bool(1) is False and judge.as_bool("maybe") is False and judge.as_bool(None) is False
    target = build_target("2026-10-12", _entry())
    parsed = _good_answer()
    parsed["key_points"][0]["supported"] = "false"
    parsed["summary"]["faithful"] = "true"
    verdicts = build_verdicts(parsed, target)
    assert verdicts["key_points"][0]["supported"] is False and verdicts["key_points"][0]["verified"] is False
    assert verdicts["summary"]["faithful"] is True


def test_append_cache_repairs_missing_trailing_newline(tmp_path):
    # (5)
    cache = tmp_path / "j.jsonl"
    cache.write_text('{"judge_id": "deepseek", "url": "u", "analysis_hash": "a", "prompt_hash": "p"}', encoding="utf-8")
    judge.append_cache({"judge_id": "qwen", "url": "u", "analysis_hash": "a", "prompt_hash": "p"}, cache)
    rows = load_cache(cache)
    assert [r["judge_id"] for r in rows] == ["deepseek", "qwen"]


def test_run_aborts_on_first_auth_error(tmp_path):
    # (6) 401/403 은 키 문제. 첫 건에서 전체 실행을 멈춘다
    _write_record(
        tmp_path / "records",
        "2026-10-12",
        [_entry("https://example.com/a"), _entry("https://example.com/b"), _entry("https://example.com/c")],
    )
    transport = FakeTransport([{"status_code": 401, "json": None, "text": "bad key"}])
    summary, output = _run("deepseek", tmp_path, transport, max_workers=1)
    assert len(transport.requests) == 1
    assert summary["failed"] == 1 and summary["succeeded"] == 0
    assert summary["aborted"] and "401" in summary["aborted"]
    assert "중단" in output
    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    assert len(rows) == 1 and rows[0]["error"].startswith("HTTP 401")


def test_call_judge_drops_response_format_only_for_400_and_422():
    # (6) 400/422 는 response_format 없이 1회 재시도, 404 는 즉시 error, 401 은 즉시 인증 오류
    for status in (400, 422):
        transport = FakeTransport([{"status_code": status, "json": None, "text": "bad"}, _ok_response(_good_answer())])
        parsed, _ = call_judge(judge.JUDGES["deepseek"], "p", api_key="k", transport=transport, sleep=_no_sleep)
        assert parsed["summary"]["faithful"] is True
        assert len(transport.requests) == 2
        assert "response_format" not in transport.requests[1]["json"]

    transport = FakeTransport([{"status_code": 404, "json": None, "text": "nope"}, _ok_response(_good_answer())])
    with pytest.raises(JudgeError, match="HTTP 404"):
        call_judge(judge.JUDGES["deepseek"], "p", api_key="k", transport=transport, sleep=_no_sleep)
    assert len(transport.requests) == 1

    transport = FakeTransport([{"status_code": 403, "json": None, "text": "forbidden"}, _ok_response(_good_answer())])
    with pytest.raises(judge.JudgeAuthError, match="HTTP 403"):
        call_judge(judge.JUDGES["deepseek"], "p", api_key="k", transport=transport, sleep=_no_sleep)
    assert len(transport.requests) == 1


def test_metrics_report_rates_excluding_truncated_items(tmp_path, monkeypatch):
    # (7) truncated 아이템을 뺀 비율을 함께 낸다
    monkeypatch.setattr(judge, "JUDGE_MAX_CHARS", len(BODY))
    # 긴 본문은 앞에 표식을 두어 잘린 뒤에도 어느 아이템인지 알 수 있게 한다
    long_body = "LONGITEM " + BODY + " 상한을 넘기는 꼬리"
    _write_record(
        tmp_path / "records",
        "2026-10-12",
        [_entry("https://example.com/short"), _entry("https://example.com/long", body=long_body)],
    )
    bad = _good_answer(idea_quotes=["없는 구절", "없는 구절"])

    class Routing(FakeTransport):
        def __call__(self, request):
            self.requests.append(request)
            content = request["json"]["messages"][1]["content"]
            return _ok_response(bad if "LONGITEM" in content else _good_answer())

    transport = Routing()
    _run("deepseek", tmp_path, transport, max_workers=1)
    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    assert sorted(r["truncated"] for r in rows) == [False, True]
    long_row = next(r for r in rows if r["url"].endswith("long"))
    assert all(not e["verified"] for e in long_row["verdicts"]["production_ideas"])
    metrics = judge.compute_metrics(rows)
    assert metrics["truncated_count"] == 1
    assert metrics["grounded_idea_rate"] == 0.5
    excl = metrics["excl_truncated"]
    assert excl["judged_count"] == 1 and excl["grounded_idea_rate"] == 1.0


def test_prompt_example_mixes_true_and_false():
    # (8) 응답 예시가 전부 true 면 앵커링 우려
    template = judge.USER_PROMPT_TEMPLATE
    assert '"grounded": false' in template and '"quote": null' in template
    assert '"supported": true' in template or '"faithful": true' in template


def test_user_prompt_placeholders_do_not_cascade():
    # (9) 본문에 플레이스홀더 문자열이 있어도 다른 섹션 값으로 치환되지 않는다
    body = "본문에 <<SUMMARY>> 와 <<QUIZ>> 라는 글자가 그대로 있다. " + BODY
    target = build_target("2026-10-12", _entry(body=body))
    prompt = judge.build_user_prompt(target)
    assert prompt.count("<<SUMMARY>>") == 1 and prompt.count("<<QUIZ>>") == 1
    assert prompt.count(target["analysis"]["one_line_summary"]) == 1
    assert body in prompt


# 실행 편의: .env 로드, 사람 표시 페이지, 라벨 반입 -----------------------------


def test_load_project_env_calls_dotenv_without_override(tmp_path, monkeypatch):
    calls: list[tuple] = []

    def fake_load_dotenv(path, override):
        calls.append((Path(path), override))
        return True

    monkeypatch.setattr(judge, "load_dotenv", fake_load_dotenv)
    env_file = tmp_path / ".env"
    # 파일이 없으면 부르지 않는다
    assert judge.load_project_env(env_file) is False and calls == []
    env_file.write_text("X=1\n", encoding="utf-8")
    assert judge.load_project_env(env_file) is True
    assert calls == [(env_file, False)]


def test_load_project_env_keeps_existing_variables(tmp_path, monkeypatch):
    # 실제 python-dotenv 로 돈다. 이미 있는 변수는 그대로, 없는 변수만 채운다
    monkeypatch.setenv("JUDGE_TEST_EXISTING", "keep")
    monkeypatch.delenv("JUDGE_TEST_NEW", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 주석\nJUDGE_TEST_EXISTING=overwritten\nexport JUDGE_TEST_NEW=\"filled\"\n", encoding="utf-8"
    )
    assert judge.load_project_env(env_file) is True
    assert judge.os.environ["JUDGE_TEST_EXISTING"] == "keep"
    assert judge.os.environ["JUDGE_TEST_NEW"] == "filled"
    monkeypatch.delenv("JUDGE_TEST_NEW", raising=False)


def test_main_loads_env_on_entry_only(tmp_path, monkeypatch, capsys):
    calls: list[int] = []
    monkeypatch.setattr(judge, "load_project_env", lambda *a, **k: calls.append(1))
    monkeypatch.setenv("JUDGE_CACHE_PATH", str(tmp_path / "none.jsonl"))
    monkeypatch.setenv("JUDGE_LABELS_PATH", str(tmp_path / "labels.jsonl"))
    assert judge.main(["report"]) == 0
    capsys.readouterr()
    assert calls == [1]


def _fake_disagreements() -> list[dict]:
    """가짜 disagreements 2건(아이템 2, 질문 3). 본문에 HTML 특수문자를 섞는다."""
    return [
        {
            "date": "2026-10-12",
            "url": "https://example.com/a?x=1&y=2",
            "title": "제목 <A> & \"B\"",
            "analysis_hash": "aaaaaaaaaaaa",
            "prompt_hash": PROMPT_HASH,
            "judges": ["deepseek", "qwen"],
            "questions": [
                {
                    "id": "id-a-idea-1",
                    "question": "production_ideas",
                    "index": 1,
                    "text": "피어 교차 검증을 UI 테스트에 쓴다",
                    "verdicts": {
                        "deepseek": {"grounded": True, "quote": "피어 교차 검증을 쓴다", "quote_verified": True},
                        "qwen": {"grounded": False, "quote": None, "quote_verified": False},
                    },
                    "excerpt": "<script>alert(\"x\")</script> & 본문 발췌 피어 교차 검증을 쓴다",
                    "human": None,
                },
                {
                    "id": "id-a-quiz-0",
                    "question": "quiz",
                    "index": 0,
                    "text": "TestPrism 의 과제 수는?",
                    "verdicts": {
                        "deepseek": {"answerable": True, "answer_correct": True, "quote": "300개 과제", "quote_verified": True},
                        "qwen": {"answerable": True, "answer_correct": False, "quote": "300개 과제", "quote_verified": True},
                    },
                    "excerpt": "300개 과제와 3000개 구현",
                    "human": None,
                },
            ],
        },
        {
            "date": "2026-10-11",
            "url": "https://example.com/b",
            "title": "제목 b",
            "analysis_hash": "bbbbbbbbbbbb",
            "prompt_hash": PROMPT_HASH,
            "judges": ["deepseek", "qwen"],
            "questions": [
                {
                    "question": "summary",  # id 없는 옛 형식: 페이지가 결정적 id 를 계산한다
                    "index": 0,
                    "text": "다중 정답을 반영해 테스트 품질을 측정한다",
                    "verdicts": {
                        "deepseek": {"faithful": True, "quote": "300개", "quote_verified": True, "unsupported_claim": None},
                        "qwen": {"faithful": False, "quote": None, "quote_verified": False, "unsupported_claim": "다중 정답"},
                    },
                    "excerpt": None,
                    "human": None,
                }
            ],
        },
    ]


def test_labeling_html_renders_items_radios_and_escapes():
    page = judge.build_labeling_html(_fake_disagreements())
    assert page.count('class="q"') == 3  # 질문 단위 섹션
    assert page.count('type="radio"') == 9  # 질문당 3개
    assert page.count('class="note"') == 3
    for label in ("근거 있음", "근거 없음", "판단 보류"):
        assert page.count(f"> {label}</label>") == 3
    assert "결과 JSON 복사" in page and 'id="fallback"' in page
    assert "피어 교차 검증을 UI 테스트에 쓴다" in page and "TestPrism 의 과제 수는?" in page
    assert "deepseek" in page and "qwen" in page
    assert "근거 없다고 본 주장: 다중 정답" in page
    assert 'data-id="id-a-idea-1"' in page
    # 옛 형식(id 없음)은 결정적 id 를 만든다
    expected = judge.question_id("https://example.com/b", "bbbbbbbbbbbb", PROMPT_HASH, "summary", 0)
    assert f'data-id="{expected}"' in page
    # 이스케이프: 본문의 스크립트가 살아 있으면 안 된다
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; 본문 발췌" in page
    assert "제목 &lt;A&gt; &amp; &quot;B&quot;" in page
    assert 'href="https://example.com/a?x=1&amp;y=2"' in page
    # 외부 리소스 없음
    assert "http://" not in page.replace("https://example.com", "")
    assert "<link" not in page and 'src="' not in page


def test_labeling_html_empty_input():
    page = judge.build_labeling_html([])
    assert "엇갈린 항목이 없다" in page
    assert 'type="radio"' not in page and "결과 JSON 복사" not in page
    assert page.startswith("<!doctype html>")


def test_question_id_is_deterministic_and_distinct():
    a = judge.question_id("u", "h", "p", "quiz", 0)
    assert a == judge.question_id("u", "h", "p", "quiz", 0) and len(a) == 16
    assert a != judge.question_id("u", "h", "p", "quiz", 1)
    assert a != judge.question_id("u2", "h", "p", "quiz", 0)


def test_import_labels_and_report_human_agreement(tmp_path, monkeypatch):
    _write_record(tmp_path / "records", "2026-10-12", [_entry("https://example.com/a")])
    _run("deepseek", tmp_path, FakeTransport(answer=_good_answer()))
    qwen_answer = _good_answer()
    qwen_answer["production_ideas"][1] = {"index": 1, "grounded": False, "quote": None}
    qwen_answer["quiz"][0]["answer_correct"] = False
    _run("qwen", tmp_path, FakeTransport(answer=qwen_answer))
    rows = load_cache(tmp_path / "cache" / "judgments.jsonl")
    items = find_disagreements(rows, judge.collect_targets(tmp_path / "records"))
    questions = items[0]["questions"]
    assert all(q["id"] for q in questions)
    idea_q, quiz_q = questions[0], questions[1]
    assert idea_q["question"] == "production_ideas" and quiz_q["question"] == "quiz"

    def label(q, human, note=""):
        return {
            "id": q["id"], "url": items[0]["url"], "analysis_hash": items[0]["analysis_hash"],
            "prompt_hash": items[0]["prompt_hash"], "question": q["question"], "index": q["index"],
            "human": human, "note": note,
        }

    labels_path = tmp_path / "labels.jsonl"
    monkeypatch.setenv("JUDGE_LABELS_PATH", str(labels_path))
    first = tmp_path / "first.json"
    # 아이디어는 처음에 보류, 퀴즈는 근거 없음(qwen 쪽 손), 깨진 레코드 하나
    first.write_text(
        json.dumps([label(idea_q, "unsure"), label(quiz_q, "unsupported", "정답이 틀렸다"), {"id": "x", "human": "maybe"}]),
        encoding="utf-8",
    )
    assert judge.import_labels(first) == {"imported": 2, "rejected": 1}
    loaded = judge.load_labels()
    assert set(loaded) == {idea_q["id"], quiz_q["id"]}
    assert loaded[quiz_q["id"]]["note"] == "정답이 틀렸다" and "labeled_at" in json.loads(labels_path.read_text().splitlines()[0])

    report = build_report(rows, until=date(2026, 10, 14), labels=loaded)
    human = report["human"]
    # 보류 1건은 분모에서 빠진다. 퀴즈: deepseek 은 둘 다 참(긍정) → 불일치, qwen 은 answer_correct 거짓 → 일치
    assert human["labels"] == 2 and human["unsure"] == 1
    assert human["by_judge"]["deepseek"] == {"compared": 1, "agreed": 0, "rate": 0.0}
    assert human["by_judge"]["qwen"] == {"compared": 1, "agreed": 1, "rate": 1.0}
    text = judge.format_report(report)
    assert "[사람 일치율] 표시 2건 (보류 1건은 분모 제외)" in text
    assert "deepseek 0.0% (0/1)" in text and "qwen 100.0% (1/1)" in text

    # 같은 id 를 다시 반입하면 마지막 것이 이긴다: 아이디어를 근거 있음으로 확정
    second = tmp_path / "second.json"
    second.write_text(json.dumps({"labels": [label(idea_q, "supported")]}), encoding="utf-8")
    assert judge.import_labels(second) == {"imported": 1, "rejected": 0}
    loaded = judge.load_labels()
    assert len(loaded) == 2 and loaded[idea_q["id"]]["human"] == "supported"
    report = build_report(rows, until=date(2026, 10, 14), labels=loaded)
    assert report["human"]["unsure"] == 0
    assert report["human"]["by_judge"]["deepseek"] == {"compared": 2, "agreed": 1, "rate": 0.5}
    assert report["human"]["by_judge"]["qwen"] == {"compared": 2, "agreed": 1, "rate": 0.5}

    # 라벨이 없으면 human 은 None 이고 보고 텍스트에도 줄이 없다
    assert build_report(rows, until=date(2026, 10, 14))["human"] is None
    assert "[사람 일치율]" not in judge.format_report(build_report(rows, until=date(2026, 10, 14)))

    # 매칭은 id 로만 한다: url 등 키 필드가 없는 라벨도 일치율에 들어간다
    id_only = {idea_q["id"]: {"id": idea_q["id"], "human": "unsupported", "note": ""}}
    report = build_report(rows, until=date(2026, 10, 14), labels=id_only)
    assert report["human"]["by_judge"]["deepseek"] == {"compared": 1, "agreed": 0, "rate": 0.0}
    assert report["human"]["by_judge"]["qwen"] == {"compared": 1, "agreed": 1, "rate": 1.0}
    # 심판 행에 없는 id 는 어느 심판에서도 세지 않는다
    stray = {"nope": {"id": "nope", "human": "supported", "note": ""}}
    report = build_report(rows, until=date(2026, 10, 14), labels=stray)
    assert report["human"]["labels"] == 1
    assert report["human"]["by_judge"]["deepseek"]["compared"] == 0


def test_labeling_html_skips_duplicate_ids():
    items = _fake_disagreements()
    duplicate = dict(items[0]["questions"][0], text="같은 id 의 두 번째 항목")
    items[1]["questions"].append(duplicate)
    cleaned, skipped = judge.dedupe_label_questions(items)
    assert skipped == 1
    assert sum(len(i["questions"]) for i in cleaned) == 3
    page = judge.build_labeling_html(items)
    assert page.count('name="h-id-a-idea-1"') == 3  # 라디오 3개, 그룹 하나
    assert "같은 id 의 두 번째 항목" not in page
    assert page.count('class="q"') == 3
    # 중복이 없으면 그대로
    assert judge.dedupe_label_questions(_fake_disagreements())[1] == 0


def test_cli_labeling_and_import_labels_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(judge, "load_project_env", lambda *a, **k: False)
    monkeypatch.setattr(judge, "RECORDS_DIR", tmp_path / "records")
    cache = tmp_path / "cache" / "judgments.jsonl"
    monkeypatch.setenv("JUDGE_CACHE_PATH", str(cache))
    monkeypatch.setenv("JUDGE_LABELS_PATH", str(tmp_path / "labels.jsonl"))

    # 캐시가 비어 있으면 빈 페이지
    empty_out = tmp_path / "empty.html"
    assert judge.main(["labeling", "--out", str(empty_out)]) == 0
    capsys.readouterr()
    assert "엇갈린 항목이 없다" in empty_out.read_text(encoding="utf-8")

    # 두 심판이 퀴즈 정답일치에서 엇갈리는 캐시를 만든다
    _write_record(tmp_path / "records", "2026-10-12", [_entry("https://example.com/a")])
    _run("deepseek", tmp_path, FakeTransport(answer=_good_answer()))
    qwen_answer = _good_answer()
    qwen_answer["quiz"][0]["answer_correct"] = False
    _run("qwen", tmp_path, FakeTransport(answer=qwen_answer))

    source = tmp_path / "d.json"
    assert judge.main(["disagreements", "--out", str(source)]) == 0
    capsys.readouterr()
    items = json.loads(source.read_text(encoding="utf-8"))
    assert len(items) == 1 and [q["question"] for q in items[0]["questions"]] == ["quiz"]
    quiz_id = items[0]["questions"][0]["id"]

    out = tmp_path / "pages" / "labeling.html"
    assert judge.main(["labeling", "--out", str(out), "--in", str(source)]) == 0
    assert "질문 1건 (아이템 1건)" in capsys.readouterr().out
    page = out.read_text(encoding="utf-8")
    assert page.count('class="q"') == 1 and f'data-id="{quiz_id}"' in page

    # 페이지에서 복사한 결과 중 id 와 human 만 있어도 반입·대조된다
    labels = tmp_path / "copied.json"
    labels.write_text(json.dumps([{"id": quiz_id, "human": "unsupported", "note": "메모"}]), encoding="utf-8")
    assert judge.main(["import-labels", str(labels)]) == 0
    assert "라벨 1건 반입 (거부 0건)" in capsys.readouterr().out
    assert len(judge.load_labels()) == 1
    assert judge.main(["report", "--json"]) == 0
    human = json.loads(capsys.readouterr().out)["human"]
    assert human["labels"] == 1 and human["unsure"] == 0
    # deepseek 은 answerable·answer_correct 둘 다 참(긍정) → 사람과 불일치, qwen 은 일치
    assert human["by_judge"]["deepseek"] == {"compared": 1, "agreed": 0, "rate": 0.0}
    assert human["by_judge"]["qwen"] == {"compared": 1, "agreed": 1, "rate": 1.0}
    assert judge.main(["report"]) == 0
    assert "deepseek 0.0% (0/1)" in capsys.readouterr().out

    # 중복 id 가 든 입력은 건너뛴 수를 알린다
    items[0]["questions"].append(dict(items[0]["questions"][0]))
    source.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    assert judge.main(["labeling", "--out", str(out), "--in", str(source)]) == 0
    assert "중복이라 건너뛰었다" in capsys.readouterr().out

    # 깨진 파일은 2 로 끝난다
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert judge.main(["import-labels", str(bad)]) == 2
    assert "라벨 반입 실패" in capsys.readouterr().err
