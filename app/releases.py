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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, Field

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "data" / "watchlist.yaml"
STATES_PATH = ROOT / "data" / "release_states.json"
EVENTS_PATH = ROOT / "data" / "releases.jsonl"

# 가중치 파일로 인정하는 확장자. README 만 있는 리포와 구분하는 기준이다.
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth", ".ckpt", ".msgpack", ".h5", ".onnx")

# "처음 보는 리포" 가 곧 "새 리포" 는 아니다. HF 조회는 정렬별 상위 100건만 보므로
# 2년 된 리포가 README 한 줄 고치면 관측 창에 처음 들어온다. 생성이 이 창보다
# 오래된 리포는 알림 없이 적재만 한다(backfill).
NEW_REPO_WINDOW_DAYS = 14

# 블로그 제목에서 시리즈 이름만으로는 발표를 가려낼 수 없다("ChatGPT for Teachers").
# 시리즈 토큰이 단어 경계로 있고, 아래 둘 중 하나가 같이 있어야 발표로 본다.
#   (1) 릴리스 단서 ("Introducing ...", "... technical report")
#   (2) 버전 패턴: 토큰 근처에 숫자가 있고 제목이 토큰으로 시작하거나 그 뒤에 ':' 가 온다
#       ("Qwen3: Think Deeper", "GLM-4.5: Reasoning ...", "Claude Sonnet 4.5", "Grok 4",
#        "The Llama 4 herd: ...")
# 단서만 쓰면 실제 Qwen 피드 44건 중 5건만 잡혔다(검증자 실측). 최근 발표 제목은
# "Introducing" 을 거의 쓰지 않는다.
# 강한 단서는 부정 단서와 무관하게 발표로 본다. 약한 단서는 부정 단서가 있으면
# 닫힌다 ("Claude 3.5 Sonnet is now available on Amazon Bedrock" 은 유통 소식이다).
_STRONG_CUE = re.compile(
    r"(\bintroducing\b|technical report|model card|system card|open[- ]?weights?|"
    r"open[- ]?sourc|unveil)",
    re.I,
)
_WEAK_CUE = re.compile(
    r"(\bannounc|\breleas(e|ed|es|ing)\b|\blaunch|\bpreview|now available|\bis here\b)",
    re.I,
)
_VERSION_NEAR = r"[^:\n]{0,15}?\d"
# 버전 패턴 경로에만 적용하는 부정 단서. "Gemini 2.5 Pro pricing update",
# "Qwen3 fine-tuning guide: 5 tips" 처럼 모델명으로 시작하는 비발표 글을 걸러낸다.
# 3차 검증 실측: 비발표 제목 36건 중 24건이 버전 패턴에 걸렸다.
_NEGATIVE_CUE = re.compile(
    r"(pricing|\bprices?\b|\bguide\b|cookbook|benchmark|outage|postmortem|bounty|"
    r"best practices|partnership|available (on|in|via)|\btips?\b|recipes?|hiring|"
    r"\braises\b|tutorial|how to|case stud|jailbreak|red[- ]?team|evaluation|"
    r"training data|\bwins?\b|\bapi\b|\bupdate\b|\bnotes\b|copilot|bedrock|"
    r"vertex|azure|\bsdk\b|webinar|workshop|survey|deprecat|\bfaq\b)",
    re.I,
)

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

    def matches_title(self, title: str) -> bool:
        """블로그 제목이 이 조직의 모델 발표인가.

        리포명 매칭과 달리 제목은 자유 문장이라 포함 매칭이 오탐을 낸다.
        실측: openai 피드 30건 중 "GPT" 포함 매칭 10건, 그중 8건이 제품 소식.
        단어 경계는 지키되("ChatGPT" 는 "GPT" 와 경계가 없다) 대소문자는 무시한다
        ("Olmo 3", "gpt-oss" 는 시리즈 표기와 대소문자가 다르다).

        두 경로가 있다.
          (1) 릴리스 단서: 강한 단서("Introducing X", "X technical report")는 무조건,
              약한 단서("released", "now available")는 부정 단서가 없을 때만
          (2) 버전 패턴: 토큰 뒤 15자 안에 숫자가 있고, (제목이 토큰으로 시작하며
              6단어 이하 | 숫자 뒤에 ':'). 부정 단서(pricing, guide, outage ...)가
              있으면 이 경로는 닫힌다. "Grok 4", "Claude Sonnet 4.5", "Qwen3: ..." 는
              살고 "GPT-5 jailbreak red-teaming results" 는 빠진다.
        """
        if not self.series:
            return False
        negative = bool(_NEGATIVE_CUE.search(title))
        cue = bool(_STRONG_CUE.search(title)) or (bool(_WEAK_CUE.search(title)) and not negative)
        words = len(title.split())
        for tok in self.series:
            pat = rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![a-z])"
            m = re.search(pat, title, re.I)
            if not m:
                continue
            if cue:
                return True
            if negative:
                continue
            tail = title[m.end():]
            v = re.match(_VERSION_NEAR, tail)
            if v and ((m.start() == 0 and words <= 6) or ":" in tail[v.end():]):
                return True
        return False


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
    has_weights: bool | None        # None = siblings 가 응답에 없어 판단 불가
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

    tags = [t for t in (m.get("tags") or []) if isinstance(t, str)]
    # siblings 키 자체가 없으면 "가중치 없음" 이 아니라 "모름" 이다. 둘을 같은
    # 값으로 접으면 응답이 한 번 빈 날 다음 날 weights_released 가 쏟아진다.
    raw_siblings = m.get("siblings")
    siblings = ([str(x.get("rfilename", "")) if isinstance(x, dict) else str(x) for x in raw_siblings]
                if isinstance(raw_siblings, list) else None)
    card = m.get("cardData")
    card = card if isinstance(card, dict) else {}

    license_ = card.get("license")
    if not license_:
        lic_tags = [t.split(":", 1)[1] for t in tags if t.startswith("license:")]
        license_ = lic_tags[0] if lic_tags else None
    if isinstance(license_, list):
        license_ = license_[0] if license_ else None
    if isinstance(license_, dict):
        license_ = license_.get("name") or license_.get("id")
    # 카드 프런트매터는 사용자 입력이라 `license: 2024` 같은 값이 온다
    license_ = str(license_) if isinstance(license_, (str, int, float)) and str(license_) else None

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
    if not gated_raw:
        gated = "false"
    elif str(gated_raw).lower() in ("auto", "manual"):
        gated = str(gated_raw).lower()
    else:
        gated = "manual"  # 알 수 없는 참값은 "막혀 있다" 쪽으로 보수적으로

    return HFObservation(
        org=org_key,
        repo_id=repo_id,
        created_at=created,
        last_modified=modified,
        has_weights=(None if siblings is None
                     else any(f.lower().endswith(WEIGHT_SUFFIXES) for f in siblings)),
        has_card=bool(card) or bool(siblings and "README.md" in siblings),
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
    weights_at: datetime | None = None   # 처음 가중치를 본 시각. 이후 내려가도 유지
    has_weights: bool | None = None      # 지금 가중치가 있는가. 보드는 이걸 본다
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
    bootstrap: bool = False        # 첫 적재. 알림 대상 아님
    backfill: bool = False         # 생성이 오래된 리포가 관측 창에 처음 들어옴. 알림 대상 아님
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def alertable(self) -> bool:
        return not (self.bootstrap or self.backfill)


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
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(ReleaseEvent.model_validate_json(line))
        except Exception as e:  # noqa: BLE001
            # append-only 자산 파일은 한 줄 때문에 통째로 못 읽으면 안 된다.
            # 전이 종류를 추가했다가 코드를 되돌린 경우가 정확히 이 경로다.
            logger.warning("release_event_skipped", line=n, error=str(e)[:120])
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
    첫 관측에서는 리포 생성 시각을 그대로 쓴다 - 대부분의 리포는 생성과
    동시에 가중치·카드가 올라오므로 그 편이 관측 시각보다 진실에 가깝다.

    "처음 본 시각"(weights_at)과 "지금 있는가"(has_weights)를 따로 둔다.
    게이팅 강화로 파일이 내려간 리포는 전자는 남고 후자만 False 가 된다.
    """
    seed = obs.created_at if prior is None else now
    weights_known = obs.has_weights is not None
    return ModelState(
        org=obs.org,
        repo_id=obs.repo_id,
        created_at=obs.created_at,
        last_modified=obs.last_modified,
        weights_at=(prior.weights_at if prior and prior.weights_at
                    else (seed if obs.has_weights else None)),
        has_weights=(obs.has_weights if weights_known else (prior.has_weights if prior else None)),
        model_card_at=(prior.model_card_at if prior and prior.model_card_at
                       else (seed if obs.has_card else None)),
        report_at=(prior.report_at if prior and prior.report_at
                   else (seed if obs.arxiv_ids else None)),
        # 라이선스가 잠깐 비는 것(README 를 내렸다 올림)은 변경이 아니다
        license=obs.license if obs.license else (prior.license if prior else None),
        gated=obs.gated,
        arxiv_ids=sorted(set(obs.arxiv_ids) | set(prior.arxiv_ids if prior else [])),
        derivative=obs.derivative,
        base_models=obs.base_models,
        first_seen=prior.first_seen if prior else now,
        last_seen=now,
    )


def detect_transitions(
    observations: list[HFObservation],
    states: dict[str, ModelState],
    now: datetime | None = None,
    new_repo_window_days: int = NEW_REPO_WINDOW_DAYS,
) -> tuple[list[ReleaseEvent], dict[str, ModelState]]:
    """관측과 이전 상태를 비교해 새 이벤트와 갱신된 상태를 돌려준다.

    순수 함수다. 파일을 읽거나 쓰지 않는다. `states` 가 비어 있으면 bootstrap 으로
    간주해 모든 리포를 `repo_created(bootstrap=True)` 로 기록한다 - 알림 대상이
    아니라 적재 기록이다.

    같은 관측을 두 번 넣으면 두 번째는 이벤트가 0건이어야 한다(멱등). 한 배치에
    같은 리포가 두 번 와도 한 번만 처리한다. 이게 깨지면 매일 같은 알림이 온다.
    """
    now = now or datetime.now(timezone.utc)
    bootstrap = not states
    new_states = dict(states)
    events: list[ReleaseEvent] = []
    window = timedelta(days=new_repo_window_days)

    for obs in observations:
        if obs.private or obs.derivative == "quantized":
            continue
        prior = new_states.get(obs.repo_id)
        state = _state_from(obs, now, prior)
        new_states[obs.repo_id] = state

        if prior is None:
            # 처음 본다고 새 리포는 아니다. 생성이 오래됐으면 조용히 적재한다.
            # 단, 대형 릴리스는 리포를 몇 주 전에 private 으로 만들어 두고 발표일에
            # 공개하는 관행이 있다. createdAt 은 가시성과 무관하므로 그런 리포는
            # "생성은 오래됐는데 최근에 수정됐고 가중치가 있다" 로 보인다. 이 경우는
            # 처음 관측으로 알리되 시각은 추정이라 unverified 로 둔다.
            old = now - obs.created_at > window
            recently_touched = now - obs.last_modified <= window
            surfaced = (not bootstrap) and old and recently_touched and bool(obs.has_weights)
            backfill = (not bootstrap) and old and not surfaced
            events.append(ReleaseEvent(
                org=obs.org, repo_id=obs.repo_id, transition="repo_created",
                observed_at=now, at=(obs.last_modified if surfaced else obs.created_at),
                source_url=obs.url,
                confidence="unverified" if surfaced else "verified",
                bootstrap=bootstrap, backfill=backfill,
                detail={
                    "weights": obs.has_weights, "card": obs.has_card,
                    "arxiv_ids": obs.arxiv_ids, "license": obs.license,
                    "gated": obs.gated, "derivative": obs.derivative,
                    "base_models": obs.base_models, "surfaced": surfaced,
                    "created_at": obs.created_at.isoformat(),
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

        # "몰랐다가 알게 됨"(prior None) 은 "없다가 생김" 이 아니다. 응답이 하루 빈 뒤
        # 돌아오면 565건이 쏟아진다. 시각만 채우고 이벤트는 내지 않는다.
        if (obs.has_weights and not prior.weights_at and prior.has_weights is False):
            events.append(ev("weights_released"))
        if not prior.model_card_at and obs.has_card:
            events.append(ev("model_card_added"))
        new_arxiv = sorted(set(obs.arxiv_ids) - set(prior.arxiv_ids))
        if new_arxiv:
            events.append(ev("report_published", arxiv_ids=new_arxiv))
        if obs.license and prior.license != obs.license:
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
        if org is None or not org.matches_title(p.title):
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


def _plain(text: str) -> str:
    """외부 문자열을 Discord 문안에 넣기 전에 서식·멘션 문자를 지운다.

    공식 블로그 제목이라 악의는 낮지만 `**`, `__`, `<>` 는 서식을 깨고
    `@everyone` 은 실제 멘션이 된다.
    """
    return re.sub(r"[*_`<>@\\]", "", text).strip()


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
        return f"{label}{tag} {_plain(str(e.detail.get('title', '')))}\n<{e.source_url}>"
    if e.transition == "repo_created":
        if e.detail.get("surfaced"):
            # 생성은 오래됐는데 지금 처음 보였다. private 에서 공개된 것일 수도,
            # 옛 리포가 손질된 것일 수도 있다. API 로는 구분이 안 되니 생성일을
            # 같이 보여 주고 판단을 사용자에게 넘긴다.
            created = str(e.detail.get("created_at", ""))[:10] or "?"
            return (f"👀 처음 관측{tag} **{_short(e.repo_id)}** (리포 생성 {created}) · "
                    f"{_facts(e.detail)}\n<{e.source_url}>")
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
    live = [e for e in events if e.alertable]
    if not live:
        return None

    def prio(e: ReleaseEvent) -> tuple[int, str]:
        org = watchlist.org(e.org)
        return (org.priority if org else 9, e.org)

    live.sort(key=prio)

    # 대형 조직이 옛 리포를 일괄 손질한 날은 "처음 관측" 이 10~30건 나온다
    # (3차 검증 추정). 그대로 실으면 그날의 진짜 전이가 뒤로 밀린다.
    # 조직당 5건을 넘으면 한 줄로 접는다.
    from collections import Counter
    surfaced_by_org = Counter(e.org for e in live if e.detail.get("surfaced"))
    folded = {org for org, n in surfaced_by_org.items() if n > 5}
    folded_done: set[str] = set()

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
        if e.detail.get("surfaced") and e.org in folded:
            if e.org in folded_done:
                continue
            folded_done.add(e.org)
            lines.append(f"👀 처음 관측 {surfaced_by_org[e.org]}건 [미확인] · "
                         f"오래된 리포 일괄 갱신. 보드에서 확인")
            shown += 1
            continue
        lines.append(format_event_line(e))
        shown += 1
    if shown < len(live) - sum(surfaced_by_org[o] - 1 for o in folded):
        lines.append(f"\n외 {len(live) - shown - sum(surfaced_by_org[o] - 1 for o in folded)}건")
    footer = f"\n전체 보드: <{tracker_url}>"

    # 줄 단위로 줄인다. 글자 단위로 자르면 URL 중간이 끊기고 보드 링크가 사라진다.
    # 넘치는 순간 멈춘다(continue 가 아니라 break). 긴 줄을 건너뛰고 뒤의 짧은 줄을
    # 실으면 우선순위 낮은 조직이 높은 조직을 밀어낸다.
    omitted_note = f"(… {len(live)}건 생략)"  # 최악 길이로 미리 잡아둔다
    budget = max_chars - len(footer) - 1 - len(omitted_note) - 1
    body: list[str] = []
    used = 0
    kept_lines = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        body.append(line)
        used += len(line) + 1
        kept_lines += 1
    if kept_lines < len(lines):
        shown_events = sum(1 for l in body if l and not l.startswith(("🔔", "__", "\n")))
        body.append(f"(… {max(len(live) - shown_events, 1)}건 생략)")
    return "\n".join(body + [footer])


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
                    "has_weights": s.has_weights,
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
