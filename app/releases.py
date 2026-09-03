"""릴리스 추적 - 워치리스트, 관측, 상태, 전이 판정, 알림 문안.

이 모듈은 네트워크를 모른다. 수집(`collectors_release`)이 만든 관측을 받아
이전 상태와 비교해 "무엇이 새로 일어났는가"만 판정한다. 그래서 전부 픽스처로
테스트할 수 있다 - 로컬 망에서 huggingface.co 가 차단돼 있어 이 분리가
편의가 아니라 필수다.

파일 두 개를 쓴다.
    data/release_states.json   리포별 최신 관측 스냅샷. 다음 실행의 비교 기준
    data/releases.jsonl        전이 이벤트 로그. append-only. 이 제품의 고유 자산

상태 스냅샷을 이벤트에서 되감아 복원하지 않고 따로 두는 이유: 비교 기준은
"마지막으로 본 모습" 하나면 충분하고, 이벤트 로그는 사람이 읽는 이력이라
서로 요구가 다르다. 둘을 하나로 합치면 어느 쪽도 단순해지지 않는다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "data" / "watchlist.yaml"
STATES_PATH = ROOT / "data" / "release_states.json"
EVENTS_PATH = ROOT / "data" / "releases.jsonl"

# 가중치 파일로 인정하는 확장자. README 만 있는 리포와 구분하는 기준이다.
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth", ".ckpt", ".msgpack", ".h5", ".onnx")

Transition = Literal[
    "repo_created",       # 처음 보는 리포. 관측 스냅샷을 detail 에 담는다
    "weights_released",   # 가중치 파일이 없다가 생겼다
    "model_card_added",   # 모델 카드가 없다가 생겼다
    "report_published",   # arXiv 태그가 새로 붙었다
    "license_changed",    # 라이선스가 바뀌었다
    "gating_removed",     # gated 가 풀렸다
    "announced",          # 공식 블로그에 시리즈 이름이 실렸다
]


# ---------------------------------------------------------------------------
# 워치리스트
# ---------------------------------------------------------------------------
class Org(BaseModel):
    key: str
    label: str
    kind: Literal["weights-open", "closed"]
    hf_org: str | None = None
    series: list[str] = Field(default_factory=list)
    blog: str | None = None
    priority: int = 3
    note: str | None = None
    observed_90d: str | None = None

    def matches(self, repo_name: str) -> bool:
        """리포 이름(org/ 이후)에 시리즈 토큰이 들어 있는가.

        접두사가 아니라 포함으로 본다. `diffusiongemma`, `t5gemma` 처럼 시리즈
        이름 앞에 수식어가 붙는 관행이 있어 접두사 매칭은 이런 걸 놓친다.
        조직 안에서만 비교하므로 포함 매칭의 오탐 위험은 낮다.
        """
        name = repo_name.lower()
        return not self.series or any(s.lower() in name for s in self.series)


class Watchlist(BaseModel):
    version: int = 1
    updated_at: str = ""
    orgs: list[Org]
    transitions: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)

    def org(self, key: str) -> Org | None:
        return next((o for o in self.orgs if o.key == key), None)


def load_watchlist(path: Path = WATCHLIST_PATH) -> Watchlist:
    return Watchlist.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# 관측 (수집기가 만든다)
# ---------------------------------------------------------------------------
def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class HFObservation(BaseModel):
    """HF Hub list API 의 모델 한 건을 전이 판정에 필요한 사실로 줄인 것."""

    org: str
    repo_id: str
    created_at: datetime
    last_modified: datetime
    has_weights: bool
    has_card: bool
    gated: str                      # "false" | "auto" | "manual"
    license: str | None
    arxiv_ids: list[str]
    derivative: str | None          # None | "quantized" | "finetune"
    base_models: list[str]
    private: bool

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo_id}"


def observation_from_hf(org_key: str, m: dict[str, Any]) -> HFObservation | None:
    """HF API 응답 한 건 -> 관측. 필수 필드가 없으면 None."""
    repo_id = m.get("id") or m.get("modelId")
    created = _parse_ts(m.get("createdAt"))
    if not repo_id or not created:
        return None
    modified = _parse_ts(m.get("lastModified")) or created

    tags: list[str] = m.get("tags") or []
    siblings = [s.get("rfilename", "") for s in (m.get("siblings") or [])]
    card = m.get("cardData") or {}

    license_ = card.get("license")
    if not license_:
        lic_tags = [t.split(":", 1)[1] for t in tags if t.startswith("license:")]
        license_ = lic_tags[0] if lic_tags else None
    if isinstance(license_, list):
        license_ = license_[0] if license_ else None

    arxiv_ids = sorted({t.split(":", 1)[1] for t in tags if t.startswith("arxiv:")})

    # HF 가 카드 메타데이터에서 파생 관계를 태그로 뽑아준다. 양자화 파생은
    # 새 모델이 아니라 같은 모델의 다른 포장이라 추적 대상에서 뺀다.
    derivative = None
    if any(t.startswith("base_model:quantized:") for t in tags):
        derivative = "quantized"
    elif any(t.startswith("base_model:finetune:") for t in tags):
        derivative = "finetune"
    base_models = [t.split(":", 1)[1] for t in tags
                   if t.startswith("base_model:") and t.count(":") == 1]

    gated_raw = m.get("gated")
    gated = "false" if not gated_raw else str(gated_raw).lower()

    return HFObservation(
        org=org_key,
        repo_id=repo_id,
        created_at=created,
        last_modified=modified,
        has_weights=any(f.lower().endswith(WEIGHT_SUFFIXES) for f in siblings),
        has_card=bool(card) or "README.md" in siblings,
        gated=gated,
        license=license_,
        arxiv_ids=arxiv_ids,
        derivative=derivative,
        base_models=base_models,
        private=bool(m.get("private")),
    )


class BlogObservation(BaseModel):
    org: str
    title: str
    url: str
    published_at: datetime | None = None


# ---------------------------------------------------------------------------
# 상태와 이벤트 (파일에 남는다)
# ---------------------------------------------------------------------------
class ModelState(BaseModel):
    """리포 하나의 마지막 관측 모습. 다음 실행의 비교 기준."""

    org: str
    repo_id: str
    created_at: datetime
    last_modified: datetime
    weights_at: datetime | None = None
    model_card_at: datetime | None = None
    report_at: datetime | None = None
    license: str | None = None
    gated: str = "false"
    arxiv_ids: list[str] = Field(default_factory=list)
    derivative: str | None = None
    base_models: list[str] = Field(default_factory=list)
    first_seen: datetime
    last_seen: datetime

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo_id}"


class ReleaseEvent(BaseModel):
    org: str
    repo_id: str                   # 블로그 발표는 URL 을 키로 쓴다
    transition: Transition
    observed_at: datetime
    at: datetime | None = None     # 실제 발생 시각 추정. 모르면 None
    source_url: str
    method: Literal["hf_api", "official_rss", "manual"] = "hf_api"
    # 1차 출처가 시각을 말해주면 verified, 우리가 관측 시점으로 추정하면 unverified.
    # 표면에서 unverified 는 [미확인] 으로 표시한다.
    confidence: Literal["verified", "unverified"] = "verified"
    bootstrap: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


def load_states(path: Path = STATES_PATH) -> dict[str, ModelState]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: ModelState.model_validate(v) for k, v in raw.get("states", {}).items()}


def save_states(states: dict[str, ModelState], path: Path = STATES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "states": {k: v.model_dump(mode="json") for k, v in sorted(states.items())},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def load_events(path: Path = EVENTS_PATH) -> list[ReleaseEvent]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(ReleaseEvent.model_validate_json(line))
    return events


def append_events(events: list[ReleaseEvent], path: Path = EVENTS_PATH) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for e in events:
            f.write(e.model_dump_json() + "\n")


# ---------------------------------------------------------------------------
# 전이 판정
# ---------------------------------------------------------------------------
def _state_from(obs: HFObservation, now: datetime, prior: ModelState | None) -> ModelState:
    """관측을 상태로 바꾼다. 이전 상태가 있으면 이미 확정된 시각은 유지한다.

    시각의 신뢰 등급이 다르다. `created_at` 은 HF 가 말해주는 사실이고,
    weights/card/report 가 처음 관측된 시각은 우리가 본 시점일 뿐이다.
    첫 관측(bootstrap)에서는 리포 생성 시각을 그대로 쓴다 - 대부분의 리포는
    생성과 동시에 가중치·카드가 올라오므로 그 편이 관측 시각보다 진실에 가깝다.
    """
    seed = obs.created_at if prior is None else now
    return ModelState(
        org=obs.org,
        repo_id=obs.repo_id,
        created_at=obs.created_at,
        last_modified=obs.last_modified,
        weights_at=(prior.weights_at if prior and prior.weights_at else (seed if obs.has_weights else None)),
        model_card_at=(prior.model_card_at if prior and prior.model_card_at else (seed if obs.has_card else None)),
        report_at=(prior.report_at if prior and prior.report_at else (seed if obs.arxiv_ids else None)),
        license=obs.license,
        gated=obs.gated,
        arxiv_ids=obs.arxiv_ids,
        derivative=obs.derivative,
        base_models=obs.base_models,
        first_seen=prior.first_seen if prior else now,
        last_seen=now,
    )


def detect_transitions(
    observations: list[HFObservation],
    states: dict[str, ModelState],
    now: datetime | None = None,
) -> tuple[list[ReleaseEvent], dict[str, ModelState]]:
    """관측과 이전 상태를 비교해 새 이벤트와 갱신된 상태를 돌려준다.

    순수 함수다. 파일을 읽거나 쓰지 않는다. `states` 가 비어 있으면 bootstrap 으로
    간주해 모든 리포를 `repo_created(bootstrap=True)` 로 기록한다 - 알림 대상이
    아니라 적재 기록이다.

    같은 관측을 두 번 넣으면 두 번째는 이벤트가 0건이어야 한다(멱등). 이게
    깨지면 매일 같은 알림이 온다.
    """
    now = now or datetime.now(timezone.utc)
    bootstrap = not states
    new_states = dict(states)
    events: list[ReleaseEvent] = []

    for obs in observations:
        if obs.private or obs.derivative == "quantized":
            continue
        prior = states.get(obs.repo_id)
        state = _state_from(obs, now, prior)
        new_states[obs.repo_id] = state

        if prior is None:
            events.append(ReleaseEvent(
                org=obs.org, repo_id=obs.repo_id, transition="repo_created",
                observed_at=now, at=obs.created_at, source_url=obs.url,
                confidence="verified", bootstrap=bootstrap,
                detail={
                    "weights": obs.has_weights, "card": obs.has_card,
                    "arxiv_ids": obs.arxiv_ids, "license": obs.license,
                    "gated": obs.gated, "derivative": obs.derivative,
                    "base_models": obs.base_models,
                },
            ))
            continue

        def ev(transition: Transition, **detail: Any) -> ReleaseEvent:
            # 변화를 "지금" 봤을 뿐 언제 일어났는지는 모른다. lastModified 가
            # 힌트지만 다른 수정일 수도 있어 unverified 로 둔다.
            return ReleaseEvent(
                org=obs.org, repo_id=obs.repo_id, transition=transition,
                observed_at=now, at=obs.last_modified, source_url=obs.url,
                confidence="unverified", detail=detail,
            )

        if not prior.weights_at and obs.has_weights:
            events.append(ev("weights_released"))
        if not prior.model_card_at and obs.has_card:
            events.append(ev("model_card_added"))
        new_arxiv = sorted(set(obs.arxiv_ids) - set(prior.arxiv_ids))
        if new_arxiv:
            events.append(ev("report_published", arxiv_ids=new_arxiv))
        if prior.license != obs.license and obs.license:
            events.append(ev("license_changed", before=prior.license, after=obs.license))
        if prior.gated != "false" and obs.gated == "false":
            events.append(ev("gating_removed", before=prior.gated))

    return events, new_states


def detect_announcements(
    posts: list[BlogObservation],
    watchlist: Watchlist,
    known_urls: set[str],
    now: datetime | None = None,
) -> list[ReleaseEvent]:
    """블로그 글 중 시리즈 이름이 제목에 있고 처음 보는 URL 만 `announced` 로."""
    now = now or datetime.now(timezone.utc)
    events = []
    for p in posts:
        if p.url in known_urls:
            continue
        org = watchlist.org(p.org)
        if org is None or not org.matches(p.title):
            continue
        events.append(ReleaseEvent(
            org=p.org, repo_id=p.url, transition="announced",
            observed_at=now, at=p.published_at, source_url=p.url,
            method="official_rss",
            confidence="verified" if p.published_at else "unverified",
            detail={"title": p.title},
        ))
    return events


# ---------------------------------------------------------------------------
# 알림 문안
# ---------------------------------------------------------------------------
_LABEL = {
    "repo_created": "🆕 새 리포",
    "weights_released": "⚖️ 가중치 공개",
    "model_card_added": "📄 모델 카드 추가",
    "report_published": "📑 리포트 공개",
    "license_changed": "📜 라이선스 변경",
    "gating_removed": "🔓 게이팅 해제",
    "announced": "📣 발표",
}


def _short(repo_id: str) -> str:
    return repo_id.split("/", 1)[-1]


def _facts(d: dict[str, Any]) -> str:
    parts = []
    parts.append("가중치 ✓" if d.get("weights") else "가중치 ✗")
    parts.append("카드 ✓" if d.get("card") else "카드 ✗")
    if d.get("arxiv_ids"):
        parts.append("arXiv " + ", ".join(d["arxiv_ids"][:2]))
    if d.get("license"):
        parts.append(str(d["license"]))
    if d.get("gated") and d["gated"] != "false":
        parts.append(f"gated:{d['gated']}")
    if d.get("derivative"):
        parts.append(f"파생:{d['derivative']}")
    return " · ".join(parts)


def format_event_line(e: ReleaseEvent) -> str:
    label = _LABEL.get(e.transition, e.transition)
    tag = "" if e.confidence == "verified" else " [미확인]"
    if e.transition == "announced":
        return f"{label}{tag} {e.detail.get('title', '')}\n<{e.source_url}>"
    if e.transition == "repo_created":
        return f"{label}{tag} **{_short(e.repo_id)}** · {_facts(e.detail)}\n<{e.source_url}>"
    if e.transition == "report_published":
        ids = ", ".join(e.detail.get("arxiv_ids", []))
        links = " ".join(f"<https://arxiv.org/abs/{i}>" for i in e.detail.get("arxiv_ids", [])[:2])
        return f"{label}{tag} **{_short(e.repo_id)}** · arXiv {ids}\n{links}"
    if e.transition == "license_changed":
        return (f"{label}{tag} **{_short(e.repo_id)}** · "
                f"{e.detail.get('before') or '없음'} → {e.detail.get('after')}\n<{e.source_url}>")
    return f"{label}{tag} **{_short(e.repo_id)}**\n<{e.source_url}>"


def format_alert(
    events: list[ReleaseEvent],
    watchlist: Watchlist,
    tracker_url: str,
    limit: int = 12,
    max_chars: int = 1900,
) -> str | None:
    """전이 이벤트를 조직별로 묶어 한 메시지로. 없으면 None.

    상한을 두는 이유: 조직 하나가 하루에 리포 20개를 올리는 날이 있다. 전부
    나열하면 정작 중요한 한 줄이 묻힌다. 우선순위 높은 조직부터 싣고 나머지는
    건수와 링크로 갈음한다.
    """
    live = [e for e in events if not e.bootstrap]
    if not live:
        return None

    def prio(e: ReleaseEvent) -> tuple[int, str]:
        org = watchlist.org(e.org)
        return (org.priority if org else 9, e.org)

    live.sort(key=prio)
    header = f"🔔 **릴리스 감시** · 전이 {len(live)}건"
    lines = [header]
    shown = 0
    current_org = None
    for e in live:
        if shown >= limit:
            break
        if e.org != current_org:
            org = watchlist.org(e.org)
            lines.append(f"\n__{org.label if org else e.org}__")
            current_org = e.org
        lines.append(format_event_line(e))
        shown += 1
    if shown < len(live):
        lines.append(f"\n외 {len(live) - shown}건")
    lines.append(f"\n전체 보드: <{tracker_url}>")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# 공개 산출물 (docs/tracker.json)
# ---------------------------------------------------------------------------
TRACKER_VERSION = 1


def build_tracker(
    watchlist: Watchlist,
    states: dict[str, ModelState],
    events: list[ReleaseEvent],
    generated_at: datetime | None = None,
    per_org: int = 40,
    recent: int = 60,
) -> dict[str, Any]:
    """정적 보드가 읽는 JSON. 계약 v1(docs/{date}.json)과 별개 버전을 가진다.

    기존 계약에 필드를 얹지 않고 파일을 따로 두는 이유: 기존 소비자가
    contract_version 을 검사하고 있을 수 있다. 다른 관심사를 같은 버전 번호에
    묶으면 한쪽 변경이 다른 쪽 소비자를 깨뜨린다.
    """
    generated_at = generated_at or datetime.now(timezone.utc)
    by_org: dict[str, list[ModelState]] = {}
    for s in states.values():
        by_org.setdefault(s.org, []).append(s)

    orgs_out = []
    for org in sorted(watchlist.orgs, key=lambda o: (o.priority, o.key)):
        models = sorted(by_org.get(org.key, []), key=lambda s: s.created_at, reverse=True)
        orgs_out.append({
            "key": org.key,
            "label": org.label,
            "kind": org.kind,
            "priority": org.priority,
            "hf_org": org.hf_org,
            "tracked": len(models),
            "models": [
                {
                    "repo_id": s.repo_id,
                    "url": s.url,
                    "created_at": s.created_at.isoformat(),
                    "last_modified": s.last_modified.isoformat(),
                    "weights_at": s.weights_at.isoformat() if s.weights_at else None,
                    "model_card_at": s.model_card_at.isoformat() if s.model_card_at else None,
                    "report_at": s.report_at.isoformat() if s.report_at else None,
                    "arxiv_ids": s.arxiv_ids,
                    "license": s.license,
                    "gated": s.gated,
                    "derivative": s.derivative,
                }
                for s in models[:per_org]
            ],
        })

    recent_events = sorted(events, key=lambda e: e.observed_at, reverse=True)[:recent]
    return {
        "tracker_version": TRACKER_VERSION,
        "generated_at": generated_at.isoformat(),
        "orgs": orgs_out,
        "recent_events": [e.model_dump(mode="json") for e in recent_events],
        "counts": {
            "orgs": len(orgs_out),
            "models": sum(o["tracked"] for o in orgs_out),
            "events": len(events),
        },
    }


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")
