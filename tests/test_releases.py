"""릴리스 추적 - 전이 판정과 알림 문안.

픽스처는 2026-09-03 GitHub Actions 프로브가 받은 HF 실응답을 줄인 것이다
(tests/fixtures/hf/*.json). 로컬 망에서 huggingface.co 가 차단돼 있어 실응답
픽스처가 곧 유일한 개발 근거다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.releases import (
    BlogObservation,
    HFObservation,
    Org,
    ReleaseEvent,
    Watchlist,
    append_events,
    build_tracker,
    detect_announcements,
    detect_transitions,
    format_alert,
    load_events,
    load_states,
    load_watchlist,
    observation_from_hf,
    save_states,
)

FIX = Path(__file__).parent / "fixtures" / "hf"
NOW = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)


def _raw(org: str) -> list[dict]:
    return json.loads((FIX / f"{org}.json").read_text(encoding="utf-8"))


def _obs(org: str, series: list[str] | None = None) -> list[HFObservation]:
    o = Org(key=org, label=org, kind="weights-open", hf_org=org, series=series or [])
    out = []
    for m in _raw(org):
        if not o.matches(m["id"].split("/", 1)[1]):
            continue
        ob = observation_from_hf(org, m)
        if ob:
            out.append(ob)
    return out


def _wl(*orgs: Org) -> Watchlist:
    return Watchlist(orgs=list(orgs))


# ---------------------------------------------------------------------------
# 관측 파싱
# ---------------------------------------------------------------------------
def test_observation_reads_transition_facts_from_real_response():
    obs = {o.repo_id: o for o in _obs("qwen")}
    drive = obs["Qwen/Qwen-Drive-1.0-4B"]
    assert drive.has_weights and drive.has_card
    assert drive.license == "apache-2.0"
    assert "2609.00111" in drive.arxiv_ids
    assert drive.derivative == "finetune"
    assert "Qwen/Qwen3.5-4B" in drive.base_models
    assert drive.gated == "false"
    assert drive.created_at.tzinfo is not None


def test_quantized_derivatives_are_recognized():
    kinds = {o.repo_id: o.derivative for o in _obs("google-open", ["gemma"])}
    quantized = [r for r, k in kinds.items() if k == "quantized"]
    assert quantized, "픽스처에 양자화 파생이 있어야 한다 (gguf/qat 계열)"
    assert all("gguf" in r.lower() or "qat" in r.lower() or "fp8" in r.lower() or "w4a16" in r.lower()
               for r in quantized), quantized


def test_gated_is_read_as_string():
    gated = {o.repo_id: o.gated for o in _obs("meta", ["Llama"])}
    assert set(gated.values()) <= {"false", "auto", "manual"}
    assert any(v != "false" for v in gated.values()), "meta 픽스처에는 gated 리포가 있다"


def test_series_match_is_substring_not_prefix():
    org = Org(key="g", label="g", kind="weights-open", series=["gemma"])
    assert org.matches("diffusiongemma-26B-A4B-it")
    assert org.matches("gemma-4-12B-it")
    assert not org.matches("timesfm-3.0-pytorch")


def test_watchlist_file_parses():
    wl = load_watchlist()
    assert len(wl.orgs) >= 10
    assert wl.org("qwen") is not None
    assert all(o.kind in ("weights-open", "closed") for o in wl.orgs)


# ---------------------------------------------------------------------------
# 전이 판정
# ---------------------------------------------------------------------------
def test_bootstrap_records_everything_but_alerts_nothing():
    obs = _obs("qwen")
    events, states = detect_transitions(obs, {}, now=NOW)
    tracked = [o for o in obs if o.derivative != "quantized" and not o.private]
    assert len(states) == len(tracked)
    assert all(e.transition == "repo_created" and e.bootstrap for e in events)
    assert format_alert(events, _wl(Org(key="qwen", label="Qwen", kind="weights-open")), "u") is None
    # bootstrap 에서는 리포 생성 시각을 사실로 쓴다
    s = states["Qwen/Qwen-Drive-1.0-4B"]
    assert s.weights_at == s.created_at and s.report_at == s.created_at


def test_quantized_repos_are_not_tracked():
    obs = _obs("google-open", ["gemma"])
    _, states = detect_transitions(obs, {}, now=NOW)
    assert all(s.derivative != "quantized" for s in states.values())
    assert not any("gguf" in r.lower() for r in states)


def test_same_observation_twice_is_idempotent():
    obs = _obs("qwen")
    _, states = detect_transitions(obs, {}, now=NOW)
    events2, states2 = detect_transitions(obs, states, now=NOW + timedelta(days=1))
    assert events2 == []
    assert set(states2) == set(states)
    assert states2["Qwen/Qwen-Drive-1.0-4B"].first_seen == NOW


def _one(repo="Org/Model-1", **over) -> HFObservation:
    base = dict(
        org="org", repo_id=repo,
        created_at=NOW - timedelta(days=10), last_modified=NOW - timedelta(days=1),
        has_weights=False, has_card=False, gated="false", license=None,
        arxiv_ids=[], derivative=None, base_models=[], private=False,
    )
    base.update(over)
    return HFObservation(**base)


def test_announce_then_weights_pattern():
    """README 만 올리고 가중치는 나중에 - 제품이 잡으려는 바로 그 패턴."""
    _, states = detect_transitions([_one(has_card=True)], {}, now=NOW)
    assert states["Org/Model-1"].weights_at is None
    later = NOW + timedelta(days=3)
    events, states = detect_transitions([_one(has_card=True, has_weights=True)], states, now=later)
    assert [e.transition for e in events] == ["weights_released"]
    assert events[0].confidence == "unverified"
    assert states["Org/Model-1"].weights_at == later


def test_report_license_gating_transitions():
    _, states = detect_transitions([_one(has_weights=True, has_card=True, gated="manual")], {}, now=NOW)
    later = NOW + timedelta(days=2)
    events, _ = detect_transitions(
        [_one(has_weights=True, has_card=True, gated="false",
              license="apache-2.0", arxiv_ids=["2609.12345"])],
        states, now=later,
    )
    kinds = {e.transition: e for e in events}
    assert set(kinds) == {"report_published", "license_changed", "gating_removed"}
    assert kinds["report_published"].detail["arxiv_ids"] == ["2609.12345"]
    assert kinds["license_changed"].detail == {"before": None, "after": "apache-2.0"}
    assert kinds["gating_removed"].detail == {"before": "manual"}


def test_license_removed_is_not_a_transition():
    _, states = detect_transitions([_one(license="mit")], {}, now=NOW)
    events, _ = detect_transitions([_one(license=None)], states, now=NOW + timedelta(days=1))
    assert events == []


def test_new_repo_after_bootstrap_is_live_event():
    _, states = detect_transitions([_one()], {}, now=NOW)
    events, _ = detect_transitions([_one(), _one("Org/Model-2", has_weights=True)], states,
                                   now=NOW + timedelta(days=1))
    assert len(events) == 1 and events[0].transition == "repo_created" and not events[0].bootstrap


def test_blog_announcements_dedupe_by_url_and_match_series():
    wl = _wl(Org(key="qwen", label="Qwen", kind="weights-open", series=["Qwen"]))
    posts = [
        BlogObservation(org="qwen", title="Qwen3.9 is here", url="https://q/a", published_at=NOW),
        BlogObservation(org="qwen", title="Hiring update", url="https://q/b", published_at=NOW),
        BlogObservation(org="qwen", title="Qwen old", url="https://q/seen", published_at=NOW),
    ]
    events = detect_announcements(posts, wl, known_urls={"https://q/seen"}, now=NOW)
    assert [e.repo_id for e in events] == ["https://q/a"]
    assert events[0].transition == "announced" and events[0].method == "official_rss"


# ---------------------------------------------------------------------------
# 알림 문안
# ---------------------------------------------------------------------------
def _wl_prio() -> Watchlist:
    return _wl(
        Org(key="b", label="B사", kind="weights-open", priority=2),
        Org(key="a", label="A사", kind="weights-open", priority=1),
    )


def _ev(org: str, repo: str, transition="repo_created", **detail) -> ReleaseEvent:
    return ReleaseEvent(org=org, repo_id=repo, transition=transition, observed_at=NOW,
                        source_url=f"https://huggingface.co/{repo}", detail=detail)


def test_alert_groups_by_priority_and_marks_unverified():
    events = [
        _ev("b", "b/x", weights=True, card=True, license="mit"),
        ReleaseEvent(org="a", repo_id="a/y", transition="report_published", observed_at=NOW,
                     source_url="https://huggingface.co/a/y", confidence="unverified",
                     detail={"arxiv_ids": ["2609.1"]}),
    ]
    text = format_alert(events, _wl_prio(), "https://t")
    assert text is not None
    assert text.index("A사") < text.index("B사")
    assert "[미확인]" in text and "arxiv.org/abs/2609.1" in text
    assert "전이 2건" in text and "https://t" in text


def test_alert_truncates_and_counts_rest():
    events = [_ev("a", f"a/m{i}", weights=True, card=True) for i in range(30)]
    text = format_alert(events, _wl_prio(), "https://t", limit=5)
    assert "외 25건" in text
    assert len(text) <= 1900


def test_alert_ignores_bootstrap_events():
    e = _ev("a", "a/x"); e.bootstrap = True
    assert format_alert([e], _wl_prio(), "u") is None


# ---------------------------------------------------------------------------
# 파일 I/O 와 보드
# ---------------------------------------------------------------------------
def test_states_and_events_roundtrip(tmp_path: Path):
    obs = _obs("qwen")
    events, states = detect_transitions(obs, {}, now=NOW)
    sp, ep = tmp_path / "s.json", tmp_path / "e.jsonl"
    save_states(states, sp); append_events(events, ep)
    assert load_states(sp) == states
    assert load_events(ep) == events
    # append 는 덧붙인다
    append_events(events[:1], ep)
    assert len(load_events(ep)) == len(events) + 1


def test_tracker_json_shape():
    wl = _wl(Org(key="qwen", label="Qwen", kind="weights-open", hf_org="Qwen", priority=1),
             Org(key="anthropic", label="Anthropic", kind="closed", priority=1))
    events, states = detect_transitions(_obs("qwen"), {}, now=NOW)
    t = build_tracker(wl, states, events, generated_at=NOW)
    assert t["tracker_version"] == 1
    assert [o["key"] for o in t["orgs"]] == ["anthropic", "qwen"]
    q = next(o for o in t["orgs"] if o["key"] == "qwen")
    assert q["tracked"] == len(states) and q["models"]
    m = q["models"][0]
    assert {"repo_id", "url", "created_at", "weights_at", "report_at", "license", "gated", "arxiv_ids"} <= set(m)
    # 최신순
    assert q["models"] == sorted(q["models"], key=lambda x: x["created_at"], reverse=True)
    assert t["counts"]["events"] == len(events)
    json.dumps(t)  # 직렬화 가능


# ---------------------------------------------------------------------------
# 독립 검증자가 찾은 결함의 회귀 테스트 (2026-09-03)
# ---------------------------------------------------------------------------
def test_old_repo_entering_window_is_backfill_not_alert():
    """관측 창(정렬별 상위 100건)에 오래된 리포가 처음 들어오는 두 경우.
    손댄 지 오래된 리포는 backfill(무음). 최근 손댔고 가중치가 있으면 "처음 관측"
    으로 알리되 생성일을 문안에 드러낸다. README 수정과 private->public 은
    API 로 구분되지 않으므로 판단을 사용자에게 넘긴다."""
    _, states = detect_transitions([_one("Org/Seed")], {}, now=NOW)
    dormant = _one("Org/Dormant", created_at=NOW - timedelta(days=900),
                   last_modified=NOW - timedelta(days=400), has_weights=True, has_card=True)
    touched = _one("Org/Touched", created_at=NOW - timedelta(days=900),
                   last_modified=NOW - timedelta(days=1), has_weights=True, has_card=True)
    fresh = _one("Org/Fresh", created_at=NOW - timedelta(days=3), has_weights=True, has_card=True)
    events, _ = detect_transitions([dormant, touched, fresh], states, now=NOW + timedelta(days=1))
    by = {e.repo_id: e for e in events}
    assert by["Org/Dormant"].backfill and not by["Org/Dormant"].alertable
    assert by["Org/Touched"].alertable and by["Org/Touched"].detail["surfaced"]
    assert by["Org/Fresh"].alertable and not by["Org/Fresh"].detail["surfaced"]
    text = format_alert(events, _wl(Org(key="org", label="O", kind="weights-open")), "u")
    assert "Fresh" in text and "Dormant" not in text
    assert "처음 관측" in text and "리포 생성 " in text and "[미확인]" in text


def test_partial_bootstrap_then_recovered_org_does_not_flood():
    """bootstrap 날 한 조직 수집이 실패했다가 다음 날 복구돼도 오래된 리포는 알리지 않는다."""
    _, states = detect_transitions(_obs("qwen"), {}, now=NOW)
    events, _ = detect_transitions(_obs("meta", ["Llama"]), states, now=NOW + timedelta(days=1))
    assert events and all(e.backfill for e in events), "meta 픽스처는 전부 2025년 생성이다"


def test_blog_title_needs_word_boundary_and_release_cue():
    gpt = Org(key="openai", label="OpenAI", kind="closed", series=["GPT", "o1", "o3", "o4"])
    assert not gpt.matches_title("ChatGPT for Teachers")
    assert not gpt.matches_title("ChatGPT Ads expands across Europe")
    assert not gpt.matches_title("Photo4Life partnership")
    assert gpt.matches_title("Introducing GPT-6")
    assert gpt.matches_title("o4-mini is now available")
    claude = Org(key="anthropic", label="Anthropic", kind="closed", series=["Claude"])
    assert not claude.matches_title("Claude Shannon: a biography")
    assert claude.matches_title("Introducing Claude Opus 6")
    assert claude.matches_title("claude opus 6 released")  # 대소문자는 무시한다 (Olmo/OLMo)


def test_missing_siblings_means_unknown_not_absent():
    raw = [dict(m) for m in _raw("qwen")]
    for m in raw:
        m.pop("siblings", None)
    obs = [o for o in (observation_from_hf("qwen", m) for m in raw) if o]
    assert all(o.has_weights is None for o in obs)
    _, states = detect_transitions(obs, {}, now=NOW)
    assert all(s.weights_at is None and s.has_weights is None for s in states.values())
    # 다음 날 siblings 가 정상으로 오면 weights_released 가 쏟아지면 안 된다...
    # 는 아니다. 처음 본 가중치이므로 전이는 맞다. 다만 bootstrap 이 "모름" 이었다는
    # 것이 상태에 남아야 하고(None), 이 테스트는 그것을 못박는다.


def test_weights_removed_keeps_first_seen_but_flags_current():
    _, states = detect_transitions([_one(has_weights=True)], {}, now=NOW)
    later = NOW + timedelta(days=1)
    events, states = detect_transitions([_one(has_weights=False)], states, now=later)
    assert events == []
    s = states["Org/Model-1"]
    assert s.weights_at == NOW - timedelta(days=10) and s.has_weights is False
    t = build_tracker(_wl(Org(key="org", label="O", kind="weights-open")), states, [], generated_at=later)
    m = t["orgs"][0]["models"][0]
    assert m["weights_at"] and m["has_weights"] is False


def test_license_blip_does_not_retrigger():
    _, states = detect_transitions([_one(license="mit")], {}, now=NOW)
    _, states = detect_transitions([_one(license=None)], states, now=NOW + timedelta(days=1))
    assert states["Org/Model-1"].license == "mit"
    events, _ = detect_transitions([_one(license="mit")], states, now=NOW + timedelta(days=2))
    assert events == []


def test_duplicate_repo_in_one_batch_counted_once():
    events, states = detect_transitions([_one(), _one()], {}, now=NOW)
    assert len(events) == 1 and len(states) == 1


def test_weird_card_values_do_not_raise():
    m = dict(_raw("qwen")[0])
    m["cardData"] = {"license": 2024, "base_model": {"x": 1}}
    m["siblings"] = ["README.md", {"rfilename": "model.safetensors"}]
    m["gated"] = True
    m["tags"] = ["license:apache-2.0", 42, "arxiv:2609.1"]
    o = observation_from_hf("qwen", m)
    assert o.license == "2024" and o.has_weights and o.gated == "manual" and o.arxiv_ids == ["2609.1"]


def test_load_events_skips_unknown_transition(tmp_path: Path):
    p = tmp_path / "e.jsonl"
    good = _ev("a", "a/x")
    p.write_text(good.model_dump_json() + "\n"
                 + good.model_dump_json().replace("repo_created", "future_kind") + "\n"
                 + "{not json\n", encoding="utf-8")
    assert load_events(p) == [good]


def test_alert_truncation_keeps_footer_and_whole_lines():
    events = [_ev("a", "a/" + "m" * 90 + str(i), weights=True, card=True, license="apache-2.0")
              for i in range(12)]
    text = format_alert(events, _wl_prio(), "https://board", limit=12, max_chars=600)
    assert len(text) <= 600
    assert text.rstrip().endswith("<https://board>")
    assert "생략" in text
    for line in text.splitlines():
        assert not line.startswith("<https://huggingface.co/") or line.endswith(">")


def test_announced_title_is_sanitized():
    e = ReleaseEvent(org="a", repo_id="https://x", transition="announced", observed_at=NOW,
                     source_url="https://x", detail={"title": "@everyone **Claude** <b>6</b>"})
    from app.releases import format_event_line
    line = format_event_line(e)
    assert "@" not in line.split("\n")[0] and "**" not in line and "<b>" not in line


def test_collector_skips_broken_repo_but_keeps_org():
    """리포 하나의 파싱 실패가 조직 전체 관측을 0건으로 만들면 안 된다."""
    import asyncio
    from app.collectors_release import fetch_hf_org

    rows = _raw("qwen")[:3]
    rows[1] = dict(rows[1]); rows[1]["createdAt"] = "not-a-date"      # None 반환 경로
    rows[2] = dict(rows[2]); rows[2]["createdAt"] = 12345                # 예외 경로 (int.replace)

    class Resp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    class Client:
        async def get(self, url, params=None):
            return Resp(rows)

    org = Org(key="qwen", label="Qwen", kind="weights-open", hf_org="Qwen", series=["Qwen"])
    got = asyncio.run(fetch_hf_org(Client(), org, delay=0))
    assert [o.repo_id for o in got] == [rows[0]["id"]]


# ---------------------------------------------------------------------------
# 2차 검증 (재현율 쪽 결함)
# ---------------------------------------------------------------------------
REAL_TITLES_HIT = [
    ("qwen", ["Qwen", "QwQ"], "Qwen3: Think Deeper, Act Faster"),
    ("qwen", ["Qwen", "QwQ"], "Qwen3-Coder: Agentic Coding in the World"),
    ("qwen", ["Qwen", "QwQ"], "QwQ-32B: Embracing the Power of Reinforcement Learning"),
    ("qwen", ["Qwen", "QwQ"], "Qwen2.5-Max: Exploring the Intelligence of Large-scale MoE Model"),
    ("anthropic", ["Claude"], "Claude Sonnet 4.5"),
    ("anthropic", ["Claude"], "Claude 3.7 Sonnet and Claude Code"),
    ("anthropic", ["Claude"], "Introducing Claude Opus 6"),
    ("google-closed", ["Gemini"], "Gemini 2.5: Our most intelligent AI model"),
    ("meta", ["Llama"], "The Llama 4 herd: The beginning of a new era of natively multimodal AI innovation"),
    ("zai", ["GLM"], "GLM-4.5: Reasoning, Coding, and Agentic Abilities"),
    ("moonshot", ["Kimi"], "Kimi K2: Open Agentic Intelligence"),
    ("xai", ["Grok"], "Grok 4"),
    ("mistral", ["Mistral", "Magistral"], "Mistral Small 3"),
    ("lgai", ["EXAONE"], "EXAONE 4.0: Unified Reasoning and Non-reasoning"),
    ("deepseek", ["DeepSeek"], "DeepSeek-V3.2-Exp: Boosting Long-Context Efficiency"),
    ("allenai", ["OLMo"], "Olmo 3: Charting a path through the model flow"),
    ("openai-open", ["gpt-oss"], "Introducing gpt-oss"),
    ("google-open", ["gemma", "paligemma", "embeddinggemma"], "Introducing EmbeddingGemma"),
    ("google-open", ["gemma", "paligemma"], "PaliGemma 2: Powerful Vision-Language Models"),
]
REAL_TITLES_MISS = [
    ("openai", ["GPT", "o1", "o3", "o4"], "ChatGPT for Teachers"),
    ("openai", ["GPT", "o1", "o3", "o4"], "ChatGPT Ads expands across Europe"),
    ("openai", ["GPT", "o1", "o3", "o4"], "ATV Big Air Tour turned 3 days of work into 3 hours with ChatGPT"),
    ("openai", ["GPT", "o1", "o3", "o4"], "Healthcare organizations can now connect EHR and additional industry data to ChatGPT"),
    ("openai", ["GPT", "o1", "o3", "o4"], "Photo4Life partnership"),
    ("anthropic", ["Claude"], "Claude Shannon: a biography"),
    ("anthropic", ["Claude"], "How teams use Claude for customer support"),
    ("qwen", ["Qwen"], "Qwen team is hiring"),
]


@pytest.mark.parametrize("key,series,title", REAL_TITLES_HIT)
def test_real_release_titles_are_matched(key, series, title):
    assert Org(key=key, label=key, kind="closed", series=series).matches_title(title), title


@pytest.mark.parametrize("key,series,title", REAL_TITLES_MISS)
def test_non_release_titles_are_not_matched(key, series, title):
    assert not Org(key=key, label=key, kind="closed", series=series).matches_title(title), title


def test_private_then_public_repo_is_surfaced_not_silenced():
    """createdAt 은 가시성과 무관하다. 몇 주 전 만든 리포를 발표일에 공개하면
    "생성은 오래됐는데 최근 수정됐고 가중치가 있다" 로 보인다. 알려야 한다."""
    _, states = detect_transitions([_one("Org/Seed")], {}, now=NOW)
    pub = _one("Org/Big", created_at=NOW - timedelta(days=30), last_modified=NOW - timedelta(hours=2),
               has_weights=True, has_card=True)
    events, states = detect_transitions([pub], states, now=NOW + timedelta(days=1))
    e = next(x for x in events if x.repo_id == "Org/Big")
    assert e.alertable and not e.backfill and e.confidence == "unverified" and e.detail["surfaced"]
    # 같은 관측을 다시 넣으면 조용하다
    events, _ = detect_transitions([pub], states, now=NOW + timedelta(days=2))
    assert events == []


def test_old_untouched_repo_is_still_backfill():
    _, states = detect_transitions([_one("Org/Seed")], {}, now=NOW)
    old = _one("Org/Old", created_at=NOW - timedelta(days=400), last_modified=NOW - timedelta(days=300),
               has_weights=True)
    events, _ = detect_transitions([old], states, now=NOW + timedelta(days=1))
    assert events[0].backfill and not events[0].alertable


def test_unknown_then_known_weights_is_not_a_release():
    """siblings 가 하루 빠졌다 돌아오면 565건이 쏟아지면 안 된다."""
    raw = [dict(m) for m in _raw("qwen")]
    for m in raw:
        m.pop("siblings", None)
    obs0 = [o for o in (observation_from_hf("qwen", m) for m in raw) if o]
    _, states = detect_transitions(obs0, {}, now=NOW)
    events, states = detect_transitions(_obs("qwen"), states, now=NOW + timedelta(days=1))
    assert not [e for e in events if e.transition == "weights_released"]
    assert any(s.weights_at for s in states.values())  # 시각은 채워진다
    # 이제 진짜 "없다가 생김" 은 여전히 잡힌다
    _, st = detect_transitions([_one(has_weights=False)], {}, now=NOW)
    ev, _ = detect_transitions([_one(has_weights=True)], st, now=NOW + timedelta(days=1))
    assert [e.transition for e in ev] == ["weights_released"]


def test_license_dict_uses_name():
    m = dict(_raw("qwen")[0]); m["cardData"] = {"license": {"name": "mit"}}
    assert observation_from_hf("qwen", m).license == "mit"
    m["cardData"] = {"license": {"x": 1}}; m["tags"] = []
    assert observation_from_hf("qwen", m).license is None


def test_alert_truncation_respects_limit_and_priority():
    wl = _wl_prio()
    events = ([_ev("b", "b/" + "m" * 60 + str(i), weights=True, card=True) for i in range(6)]
              + [_ev("a", "a/" + "n" * 60 + str(i), weights=True, card=True) for i in range(6)])
    for n in (300, 450, 600, 900):
        text = format_alert(events, wl, "https://board", limit=12, max_chars=n)
        assert len(text) <= n, (n, len(text))
        assert text.rstrip().endswith("<https://board>")
        # 우선순위 1(A사) 이 먼저 실리고, B사가 실렸다면 A사 6건이 전부 실려 있어야 한다
        if "B사" in text:
            assert text.count("nnnn") == 6
