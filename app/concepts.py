"""개념 어휘 — 태그가 못 하는 일반화를 맡는다 (v2 §3.5 Layer 2).

왜 태그로는 안 되는가:
    §3.5대로 태그는 "본문에 실제 등장한 고유명사"다. 그래서 실측 태그가
    `glm-5.3-flash`, `artificial analysis intelligence index` 같은 1회성 문자열이
    된다. 같은 태그가 두 번 나올 일이 거의 없으니:

    - 퀴즈 정답률을 태그로 집계해도 표본이 1에 머물러 습득도가 안 쌓인다(⑥→⑦)
    - 👍를 태그로 일반화해도 다음 콘텐츠에 걸리지 않는다(③→⑤)

    루프의 두 되먹임이 같은 이유로 막혀 있다. 그 사이에 **재사용되는 단위**가
    필요하고 그게 개념이다. `glm-5.3-flash`는 태그로 남기되, 개념은
    `모델 벤치마킹` 처럼 다음에 또 나올 층위여야 한다.

엔티티 해소를 어떻게 하는가:
    §8.2는 3단(정규화 → 임베딩 후보 리콜 → 회색지대 LLM 판정)을 제시한다.
    여기서는 **1단(정규화 + 별칭)만 구현하고, 2·3단 대신 예방을 쓴다.**

    예방 = 어휘를 프롬프트에 넣어 재사용을 유도한다. 분석할 때 기존 개념 목록을
    보여주고 "여기 해당하는 게 있으면 그걸 쓰고, 정말 없을 때만 새로 만들라"고
    지시한다. 사후 병합보다 애초에 파편을 덜 만드는 쪽이 싸고 확실하다.
    임베딩 의존성도 임계값 두 개(τ_recall/τ_merge)의 튜닝 부담도 없다.

    한계는 정직하게 남긴다: 예방이 새는 만큼은 파편이 생긴다. 그걸 감시하는
    지표가 **개념 신규율**이고, §8.2 기준 80%를 넘으면 해소가 실패하는 중이다.
    그때가 2·3단을 들일 시점이다.

    과다 병합이 과소 병합보다 위험하다는 §8.2 원칙을 지켜, 자동 병합은 정규화가
    완전히 일치할 때만 한다. 애매한 건 새 개념으로 둔다 — 나중에 합칠 수 있다.

저장:
    data/concepts.json (커밋). 러너가 ephemeral이라 커밋하지 않으면 어휘가 매 런
    초기화되고, 그러면 모든 개념이 영원히 "신규"가 되어 존재 의의가 사라진다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent
CONCEPTS_PATH = ROOT / "data" / "concepts.json"
KST = ZoneInfo("Asia/Seoul")

VOCAB_VERSION = 1

# 프롬프트에 실을 개념 수. 어휘가 커질수록 토큰이 늘어나므로 상한을 둔다.
# 빈도순 상위만 보여줘도 재사용 유도 효과는 대부분 얻는다 — 자주 쓰이는 개념이
# 곧 다시 나올 개념이다.
PROMPT_VOCAB_LIMIT = 80

# 아이템당 개념 수. 태그(5개)보다 좁게 잡는다 — 개념은 상위 층위라 많을 이유가 없고,
# 많이 뽑게 두면 모델이 억지로 채우면서 파편이 늘어난다.
MAX_CONCEPTS_PER_ITEM = 3


def normalize(raw: object) -> str:
    """해소 1단: 표기 차이만 제거한다. 의미 판단은 하지 않는다.

    `MLOps` / `ml-ops` / `ML  Ops` → `mlops`
    한글은 NFC로 통일한다(자모 분리 입력이 섞이면 같은 글자가 달라 보인다).

    None은 빈 문자열로 본다. `str(None)`이 "None"이라, 이 방어가 없으면 모델이
    `"concepts": [null]`을 냈을 때 **"None"이라는 개념이 어휘에 등록된다**.
    """
    if raw is None:
        return ""
    text = unicodedata.normalize("NFC", str(raw)).strip().lower()
    # 하이픈·언더스코어·연속 공백을 하나의 구분자로 통일한 뒤 제거한다.
    text = re.sub(r"[\s_\-/]+", " ", text).strip()
    return text


def _canonical_key(raw: str) -> str:
    """병합 판정용 키. 공백까지 없애 `ml ops`와 `mlops`를 같게 본다."""
    return normalize(raw).replace(" ", "")


def load_vocabulary(path: Path | None = None) -> dict[str, Any]:
    """어휘 로드. 없거나 깨졌으면 빈 어휘로 시작한다(파이프라인은 멈추지 않는다)."""
    target = path or CONCEPTS_PATH
    if not target.exists():
        return {"version": VOCAB_VERSION, "updated_at": None, "concepts": {}}
    try:
        with target.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("concept_vocab_unreadable", error=str(error)[:200])
        return {"version": VOCAB_VERSION, "updated_at": None, "concepts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("concepts"), dict):
        return {"version": VOCAB_VERSION, "updated_at": None, "concepts": {}}
    return data


def save_vocabulary(vocab: dict[str, Any], path: Path | None = None) -> None:
    target = path or CONCEPTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    vocab["version"] = VOCAB_VERSION
    vocab["updated_at"] = datetime.now(KST).isoformat()
    target.write_text(
        json.dumps(vocab, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _key_index(vocab: dict[str, Any]) -> dict[str, str]:
    """정규화 키 → 표준 개념명. 별칭도 같은 표준명을 가리킨다."""
    index: dict[str, str] = {}
    for canonical, entry in (vocab.get("concepts") or {}).items():
        index[_canonical_key(canonical)] = canonical
        for alias in (entry or {}).get("aliases") or []:
            index[_canonical_key(alias)] = canonical
    return index


def resolve(raw: object, vocab: dict[str, Any]) -> str | None:
    """기존 개념으로 해소한다. 정규화 키가 완전히 일치할 때만 병합한다.

    부분 일치·유사도 병합을 하지 않는 이유는 §8.2의 원칙이다 — 과다 병합은
    원본 구분이 소실돼 되돌리기 어렵고, 과소 병합은 나중에 합칠 수 있다.
    """
    if raw is None or not str(raw).strip():
        return None
    return _key_index(vocab).get(_canonical_key(raw))


def register(
    raws: Iterable[str],
    vocab: dict[str, Any],
    date: str | None = None,
) -> tuple[list[str], list[str]]:
    """개념들을 어휘에 반영하고 (표준명 목록, 신규 표준명 목록)을 돌려준다.

    vocab을 제자리에서 수정한다. 호출부가 저장 시점을 정한다 — 아이템마다
    파일을 쓰면 런 한 번에 수십 번 디스크를 때린다.
    """
    concepts = vocab.setdefault("concepts", {})
    index = _key_index(vocab)
    today = date or datetime.now(KST).strftime("%Y-%m-%d")

    resolved: list[str] = []
    created: list[str] = []

    for raw in raws:
        if raw is None:
            continue
        display = unicodedata.normalize("NFC", str(raw)).strip()
        if not display:
            continue
        key = _canonical_key(display)
        if not key:
            continue

        canonical = index.get(key)
        if canonical is None:
            # 신규 개념. 표기는 사람이 읽을 원문 그대로 보존한다.
            canonical = display
            concepts[canonical] = {
                "aliases": [],
                "count": 0,
                "first_seen": today,
                "last_seen": today,
            }
            index[key] = canonical
            created.append(canonical)

        entry = concepts.setdefault(
            canonical,
            {"aliases": [], "count": 0, "first_seen": today, "last_seen": today},
        )
        # 표준명과 표기가 다르면 별칭으로 기록해 다음부터 바로 해소되게 한다.
        if display != canonical and display not in (entry.get("aliases") or []):
            entry.setdefault("aliases", []).append(display)
            index[_canonical_key(display)] = canonical
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_seen"] = today

        if canonical not in resolved:
            resolved.append(canonical)

    return resolved, created


def vocabulary_for_prompt(vocab: dict[str, Any], limit: int = PROMPT_VOCAB_LIMIT) -> str:
    """프롬프트에 실을 개념 목록. 빈도 내림차순 상위 limit개.

    비어 있으면 "(아직 없음)"을 돌려준다 — 빈 문자열을 넣으면 모델이 형식을
    오해해서 엉뚱한 걸 채운다.
    """
    concepts = vocab.get("concepts") or {}
    if not concepts:
        return "(아직 없음 — 자유롭게 만드세요)"
    ranked = sorted(
        concepts.items(),
        key=lambda kv: (-int((kv[1] or {}).get("count", 0)), kv[0]),
    )
    return ", ".join(name for name, _ in ranked[:limit])


def novelty_rate(resolved: list[str], created: list[str]) -> float:
    """개념 신규율 = 신규 / 전체 (§3.6 메타 루브릭).

    §8.2 기준으로 80%를 넘으면 해소가 실패하는 중이라는 뜻이다 — 어휘 예방이
    새고 있으니 임베딩 기반 2·3단을 들일 시점이다. 반대로 10% 미만이면 정체다.
    """
    if not resolved:
        return 0.0
    return round(len(created) / len(resolved), 4)
