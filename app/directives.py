"""자연어 지시 — 봇에게 한국말로 말하면 다음 날 큐레이션이 바뀐다.

지금까지의 한계:
    자유 텍스트를 보내면 [polling.py]가 `/keyword`로 시작하지 않는 한 **버리고**
    업데이트를 acknowledge까지 했다. 텔레그램 서버에서도 지워지므로 흔적이 없다.
    `/keyword`도 표현력이 "X를 더" 하나뿐이라 "Y는 그만", "이 채널 빼줘",
    "논문보다 실무 사례" 같은 건 담을 수 없었다.

두 갈래로 적용하는 이유:
    "arxiv 그만"은 **코드로 확정적으로** 막아야 한다. 프롬프트에 부탁하면 지킬
    때도 안 지킬 때도 있다. 반대로 "요약을 더 짧게", "논문보다 실무 사례 위주로"
    는 코드로 표현할 방법이 없으니 프롬프트에 넣어야 한다. 한쪽만으로는 못 한다.

        drop_sources ─► 후보에서 제외        (코드, 확정적)
        suppress ─────► 타이브레이크 감점    (코드, 확정적)
        boost ────────► 타이브레이크 가점    (코드, 확정적)
        standing_note ► 랭킹·분석 프롬프트   (프롬프트, 재량적)

만료가 왜 필수인가:
    3개월 전 "arxiv 그만"이 영원히 살아 있으면 왜 arxiv가 안 오는지 아무도
    모르게 된다. 안 보이는 상태값이 조용히 쌓이는 게 이런 시스템에서 가장
    위험하다. 그래서 원문마다 TTL을 박고, 매일 피드백 요약에 **현재 적용 중인
    지시**를 같이 보낸다.

해석 시점:
    런 시작 시 1회. 만료되지 않은 원문 전체를 시간순으로 LLM에 넘겨 구조화한다.
    누적 원문을 매번 다시 해석하므로 별도 상태 파일이 없다 — 원문이 유일한 정본이고
    모순("arxiv 그만" 뒤 "arxiv 다시")은 모델이 순서를 보고 알아서 정리한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent
DIRECTIVES_PATH = ROOT / "data" / "directives.jsonl"
KST = ZoneInfo("Asia/Seoul")

# 지시의 기본 수명. 짧으면 자꾸 다시 말해야 하고, 길면 잊힌 지시가 큐레이션을
# 조용히 끌고 간다. 2주면 "요즘"의 범위와 얼추 맞는다.
DEFAULT_TTL_DAYS = 14

# 해석에 넘길 최대 원문 수. 오래된 것부터 잘린다.
MAX_RAW_MESSAGES = 30

# standing_note 길이 상한. 프롬프트에 그대로 들어가므로 무한정 자라면 안 된다.
MAX_NOTE_CHARS = 300


@dataclass
class Directive:
    """해석된 지시. 전부 비어 있으면 아무것도 바꾸지 않는다."""

    boost: set[str] = field(default_factory=set)
    suppress: set[str] = field(default_factory=set)
    drop_sources: set[str] = field(default_factory=set)
    standing_note: str = ""

    def is_empty(self) -> bool:
        return not (
            self.boost or self.suppress or self.drop_sources or self.standing_note
        )

    def describe(self) -> str:
        """사용자에게 보여줄 한 줄 요약. 보이지 않는 상태값을 만들지 않기 위함."""
        parts: list[str] = []
        if self.boost:
            parts.append(f"➕ {', '.join(sorted(self.boost))}")
        if self.suppress:
            parts.append(f"➖ {', '.join(sorted(self.suppress))}")
        if self.drop_sources:
            parts.append(f"🚫 {', '.join(sorted(self.drop_sources))}")
        if self.standing_note:
            parts.append(f"📌 {self.standing_note}")
        return " · ".join(parts)


def _normalize(value: Any) -> str:
    return str(value).strip().lower()


# ──────────────────────────────────────────────
# 원문 수집
# ──────────────────────────────────────────────

def capture(
    text: str,
    ttl_days: int = DEFAULT_TTL_DAYS,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """자유 텍스트 한 건을 원문 그대로 쌓는다. 해석은 나중에 한 번에 한다.

    받는 즉시 해석하지 않는 이유: 지시는 서로를 뒤집는다("arxiv 그만" → 다음 날
    "역시 arxiv 살려"). 개별 해석 결과를 병합하는 것보다 누적 원문을 한 번에
    읽히는 쪽이 모순 해소가 정확하다.
    """
    cleaned = str(text).strip()
    if not cleaned:
        return None

    stamp = now or datetime.now(KST)
    entry = {
        "text": cleaned[:1000],
        "received_at": stamp.isoformat(),
        "expires_at": (stamp + timedelta(days=ttl_days)).isoformat(),
    }

    target = path or DIRECTIVES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    logger.info("directive_captured", text=cleaned[:80])
    return entry


def load_raw(path: Path | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """만료되지 않은 원문을 시간순으로 돌려준다. 깨진 줄은 건너뛴다."""
    target = path or DIRECTIVES_PATH
    if not target.exists():
        return []

    reference = now or datetime.now(KST)
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or not row.get("text"):
                continue
            try:
                if datetime.fromisoformat(str(row["expires_at"])) <= reference:
                    continue
            except (KeyError, TypeError, ValueError):
                # 만료 시각을 못 읽으면 살아 있는 것으로 본다. 지시를 조용히
                # 잃는 것보다 낫다 — 어차피 사용자에게 매일 보여준다.
                pass
            rows.append(row)

    return rows[-MAX_RAW_MESSAGES:]


# ──────────────────────────────────────────────
# 해석
# ──────────────────────────────────────────────

INTERPRET_PROMPT = """\
당신은 개인 뉴스레터 큐레이션 시스템의 설정 해석기입니다.
사용자가 봇에게 보낸 자연어 메시지들을 읽고, 큐레이션에 적용할 지시로 바꾸세요.

## 사용자 메시지 (오래된 것 → 최신 순)
{messages}

## 시스템이 아는 개념
{vocabulary}

## 시스템이 아는 출처 식별자
{sources}

## 규칙
- 나중 메시지가 앞선 메시지를 뒤집으면 **나중 것만** 반영하세요.
  (예: "arxiv 그만" 뒤에 "arxiv 다시 봐도 될 듯" → drop_sources에서 제외)
- boost / suppress에는 **개념 수준의 말**만 넣으세요. 위 개념 목록에 있는 표현이
  들어맞으면 그대로 쓰고, 없으면 짧은 일반 명사구로 만드세요.
  제품명·버전 같은 고유명사는 넣지 마세요.
- drop_sources에는 **위 출처 식별자 목록에 실제로 있는 값만** 넣으세요.
  목록에 없으면 넣지 말고, 대신 suppress나 standing_note로 표현하세요.
- standing_note에는 위 세 항목으로 표현할 수 없는 지시만 한국어 한두 문장으로
  적으세요 (예: "논문보다 실무 사례를 우선", "요약을 더 짧게").
  적을 게 없으면 빈 문자열로 두세요. {note_limit}자를 넘기지 마세요.
- 큐레이션과 무관한 잡담은 전부 무시하고 빈 값으로 두세요.

반드시 아래 JSON 구조로만 응답하세요.
{{"boost": [], "suppress": [], "drop_sources": [], "standing_note": ""}}
"""

_INTERPRET_SCHEMA: dict = {
    "name": "curation_directive",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "boost": {"type": "array", "items": {"type": "string"}},
            "suppress": {"type": "array", "items": {"type": "string"}},
            "drop_sources": {"type": "array", "items": {"type": "string"}},
            "standing_note": {"type": "string"},
        },
        "required": ["boost", "suppress", "drop_sources", "standing_note"],
        "additionalProperties": False,
    },
}


def parse_directive(data: dict, known_sources: Iterable[str] | None = None) -> Directive:
    """LLM 응답 → Directive. 방어적으로 판다.

    drop_sources는 **알려진 출처 식별자에만** 대응시킨다. 모델이 없는 이름을
    지어내도 조용히 무시되게 하려는 것이다 — 하드 필터라 오작동 비용이 크다.
    """
    known = {_normalize(s): str(s) for s in (known_sources or [])}

    def as_set(key: str) -> set[str]:
        raw = (data or {}).get(key)
        if not isinstance(raw, list):
            return set()
        return {_normalize(v) for v in raw if str(v).strip()}

    drops: set[str] = set()
    for candidate in as_set("drop_sources"):
        if candidate in known:
            drops.add(known[candidate])
        else:
            logger.warning("directive_unknown_source_ignored", source=candidate)

    note = str((data or {}).get("standing_note") or "").strip()[:MAX_NOTE_CHARS]

    return Directive(
        boost=as_set("boost"),
        suppress=as_set("suppress"),
        drop_sources=drops,
        standing_note=note,
    )


async def interpret(
    call_llm,
    vocabulary: str,
    known_sources: Iterable[str],
    path: Path | None = None,
    now: datetime | None = None,
) -> Directive:
    """만료 전 원문을 구조화된 지시로 바꾼다. 원문이 없으면 LLM을 부르지 않는다.

    call_llm은 `(prompt, title, json_schema=...) -> dict` 시그니처를 기대한다
    (analyzer._call_llm_with_fallback). 의존성을 주입받아 이 모듈이 analyzer를
    import하지 않게 한다 — analyzer가 이 모듈을 쓰므로 순환이 된다.
    """
    raws = load_raw(path, now)
    if not raws:
        return Directive()

    sources = sorted({str(s) for s in known_sources if str(s).strip()})
    prompt = INTERPRET_PROMPT.format(
        messages="\n".join(f"- {r['text']}" for r in raws),
        vocabulary=vocabulary or "(아직 없음)",
        sources=", ".join(sources) or "(없음)",
        note_limit=MAX_NOTE_CHARS,
    )

    try:
        data = await call_llm(prompt, "directive-interpretation", json_schema=_INTERPRET_SCHEMA)
    except Exception as error:
        # 해석이 실패해도 다이제스트는 나가야 한다. 지시가 반영되지 않을 뿐이다.
        logger.warning("directive_interpret_failed", error=str(error)[:200])
        return Directive()

    directive = parse_directive(data, sources)
    logger.info(
        "directive_interpreted",
        messages=len(raws),
        boost=sorted(directive.boost),
        suppress=sorted(directive.suppress),
        drop_sources=sorted(directive.drop_sources),
        has_note=bool(directive.standing_note),
    )
    return directive


# ──────────────────────────────────────────────
# 적용
# ──────────────────────────────────────────────

def filter_sources(items: list, directive: Directive) -> list:
    """drop_sources에 해당하는 아이템을 후보에서 뺀다.

    **안전판**: 제외 후 후보가 하나도 안 남으면 제외를 포기한다. 상대 랭킹 설계
    자체가 "빈 다이제스트를 구조적으로 없앤다"는 목적이었는데, 지시 한 줄이
    그걸 되돌리면 안 된다. 잘못된 지시의 최대 피해를 "한 판 어색한 큐레이션"으로
    묶어두고 "발송 없음"까지는 가지 않게 한다.
    """
    if not directive.drop_sources:
        return items

    drops = {_normalize(s) for s in directive.drop_sources}
    kept = [i for i in items if _normalize(getattr(i, "source_key", "")) not in drops]

    if not kept:
        logger.warning(
            "directive_drop_skipped",
            reason="제외하면 후보가 0건이 되어 다이제스트가 비어버린다",
            drop_sources=sorted(directive.drop_sources),
        )
        return items

    if len(kept) != len(items):
        logger.info(
            "directive_sources_dropped",
            removed=len(items) - len(kept),
            drop_sources=sorted(directive.drop_sources),
        )
    return kept


# 지시 가산/감산은 취향(±2)보다 무겁게 둔다. 사용자가 명시적으로 말한 것이므로
# 추론된 취향보다 우선해야 한다.
DIRECTIVE_WEIGHT = 3


def directive_score(
    directive: Directive,
    tags: Iterable[str],
    concepts: Iterable[str],
) -> int:
    """boost/suppress에 걸리면 타이브레이크 가감점. 걸리는 게 없으면 0.

    개념과 태그를 모두 본다 — 사용자가 "쿠버네티스 그만"이라고 할 때 그게 개념일지
    태그일지 미리 알 수 없다.
    """
    if not directive.boost and not directive.suppress:
        return 0

    haystack = {_normalize(t) for t in tags} | {_normalize(c) for c in concepts}
    if not haystack:
        return 0

    score = 0
    for term in directive.boost:
        if any(term in straw or straw in term for straw in haystack):
            score += DIRECTIVE_WEIGHT
            break
    for term in directive.suppress:
        if any(term in straw or straw in term for straw in haystack):
            score -= DIRECTIVE_WEIGHT
            break
    return score
