"""👍/👎 신호를 실제 선정에 반영하는 계층.

왜 필요한가:
    `UserProfile.liked_item_ids` / `disliked_item_ids`는 지금까지 **쓰기 전용**이었다.
    Telegram 버튼 → feedback.process_feedback → Supabase 저장까지는 갔지만, 그 뒤
    어디서도 읽지 않았다(전 소스 grep 확인). 즉 버튼을 눌러도 다음 큐레이션이
    바뀌지 않았다. 루프의 마지막 한 칸이 비어 있었다.

어디에 반영하고, 어디에 반영하지 않는가:
    반영하지 **않는** 곳 — 분석(analyze_content).
        actionability·depth는 "확보된 근거 안에 무엇이 있나"를 재는 축이다.
        여기에 취향을 주입하면 좋아하는 주제라서 점수가 오르고, 근거 게이트가
        무의미해진다. tests/test_evidence_gate.py가 이 경계를 지키고 있다.

    반영하는 곳 — 선정(selection).
        1) 상대 랭킹의 **동점 타이브레이크**. 점수를 덮어쓰지 않는다. 근거가
           같은 급일 때만 취향이 순서를 정한다. 실측상 153건이 5개 점수값에
           몰려 있어(IQR=1) 동점이 흔하므로 효과는 실질적이다.
        2) Stage 1 메타데이터 랭킹 프롬프트의 힌트. 이 단계는 애초에 "무엇을
           깊게 볼지" 고르는 취향 단계이고, keyword_requests가 이미 여기 들어간다.

URL이 아니라 무엇을 신호로 쓰는가:
    프로필에 쌓이는 건 개별 URL이다. 같은 URL이 다시 올 일은 없으므로(dedup)
    URL 자체는 미래에 쓸모가 없다. 쓸모 있는 건 그 URL이 **어떤 출처의, 어떤
    태그를 단 콘텐츠였나**다. 그래서 커밋된 정본(data/records/)에서 URL을
    역참조해 출처·태그로 일반화한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import structlog

# id 정의는 공개 계약(app/contract.py)이 소유한다. 여기서 다시 구현하면 두
# 정의가 갈라지는 순간 버튼이 가리키는 아이템과 계약이 가리키는 아이템이
# 달라진다. 재노출만 한다.
from app.contract import item_id

logger = structlog.get_logger()

__all__ = [
    "ItemFacts",
    "PreferenceSignal",
    "build_item_index",
    "build_signal",
    "describe_for_prompt",
    "item_id",
    "preference_score",
    "resolve_feedback_target",
]

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "data" / "records"

# 최근 N건만 본다. 오래된 취향이 영원히 남으면 프로필이 굳어서 새 주제가 못 들어온다.
RECENT_FEEDBACK_WINDOW = 30

# 타이브레이크 점수 상한. 취향이 근거를 이길 수 없게 좁게 묶는다.
_MAX_PREFERENCE_SCORE = 3


@dataclass
class ItemFacts:
    """정본에서 되살린 아이템 한 건의 식별 정보.

    quiz_answers는 문항 순서대로의 정답 인덱스다. 퀴즈 콜백은 사용자가 무엇을
    골랐는지만 실어 오므로(callback_data 64바이트), 채점하려면 정본에서 정답을
    되찾아야 한다.
    """

    url: str
    source_key: str | None
    tags: tuple[str, ...]
    quiz_answers: tuple[int, ...] = ()
    concepts: tuple[str, ...] = ()
    # 다음 날 퀴즈 결과를 되돌려줄 때 문항·선지·해설 원문이 필요하다.
    title: str = ""
    quiz: tuple[dict, ...] = ()


@dataclass
class PreferenceSignal:
    """👍/👎에서 일반화한 취향. 비어 있으면 모든 판정이 0이 되어 무해하다."""

    liked_sources: set[str] = field(default_factory=set)
    disliked_sources: set[str] = field(default_factory=set)
    liked_tags: set[str] = field(default_factory=set)
    disliked_tags: set[str] = field(default_factory=set)
    # 개념은 태그와 달리 재사용되므로 다음 콘텐츠에 실제로 걸린다(app/concepts.py).
    # 태그 신호는 남겨둔다 — 개념 어휘가 얇은 초기엔 태그라도 있는 편이 낫다.
    liked_concepts: set[str] = field(default_factory=set)
    disliked_concepts: set[str] = field(default_factory=set)
    # 퀴즈를 반복해서 틀린 개념(§4.2 ⑦). 좋아한 것이 아니라 **모르는 것**이라
    # 의미가 다르므로 liked와 섞지 않는다. 가산도 더 작다 — 사용자가 요청한
    # 적 없는 나의 판단이므로 취향보다 약하게 민다.
    review_concepts: set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not (
            self.liked_sources
            or self.disliked_sources
            or self.liked_tags
            or self.disliked_tags
            or self.liked_concepts
            or self.disliked_concepts
            or self.review_concepts
        )


def _normalize_tag(tag: Any) -> str:
    return str(tag).strip().lower()


def build_item_index(records_dir: Path | None = None) -> dict[str, ItemFacts]:
    """정본 전체를 훑어 {item_id: ItemFacts} 색인을 만든다.

    URL 키와 id 키를 모두 넣는다 — id 기반 버튼 이전에 쌓인 피드백은 프로필에
    URL로 남아 있으므로, 둘 다 조회되어야 과거 신호가 버려지지 않는다.
    """
    directory = records_dir or RECORDS_DIR
    index: dict[str, ItemFacts] = {}
    if not directory.is_dir():
        return index

    for path in sorted(directory.glob("digest_????-??-??.json")):
        try:
            with path.open("r", encoding="utf-8") as file:
                record = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue

        for entry in record.get("items") or []:
            raw = entry.get("raw") or {}
            analysis = entry.get("analysis") or {}
            url = str(raw.get("url") or "").strip()
            if not url:
                continue
            facts = ItemFacts(
                url=url,
                source_key=raw.get("source_key") or raw.get("source_name"),
                tags=tuple(
                    _normalize_tag(t) for t in (analysis.get("tags") or []) if str(t).strip()
                ),
                concepts=tuple(
                    str(c).strip() for c in (analysis.get("concepts") or []) if str(c).strip()
                ),
                quiz_answers=tuple(
                    int(q.get("answer_index"))
                    for q in (analysis.get("quiz") or [])
                    if isinstance(q, dict) and isinstance(q.get("answer_index"), int)
                ),
                title=str(raw.get("title") or ""),
                quiz=tuple(q for q in (analysis.get("quiz") or []) if isinstance(q, dict)),
            )
            index[item_id(url)] = facts
            index[url] = facts

    return index


def resolve_feedback_target(token: str, index: dict[str, ItemFacts] | None = None) -> str:
    """Telegram callback_data의 토큰(item_id 또는 과거의 URL)을 URL로 되돌린다.

    색인에 없으면 토큰을 그대로 돌려준다 — 아직 정본에 안 실린 당일 아이템이거나
    색인이 비어 있는 경우다. 프로필에는 어차피 문자열이 쌓이므로 유실은 없고,
    다음 색인 갱신 때 일반화된다.
    """
    lookup = index if index is not None else build_item_index()
    facts = lookup.get(token)
    return facts.url if facts else token


def build_signal(
    liked: Iterable[str],
    disliked: Iterable[str],
    index: dict[str, ItemFacts] | None = None,
    review: Iterable[str] | None = None,
) -> PreferenceSignal:
    """피드백 목록을 출처·태그 취향으로 일반화한다.

    같은 대상이 양쪽에 다 있으면(👍 뒤 👎) 최근 것이 이긴다고 볼 근거가 없으므로
    양쪽에서 빼 중립으로 둔다 — 애매한 신호로 순서를 흔드는 것보다 낫다.
    """
    liked_list, disliked_list = list(liked), list(disliked)
    signal = PreferenceSignal()
    # 복습 개념은 이미 개념명이라 역참조가 필요 없다. 퀴즈 응답에서 오므로
    # 👍/👎가 하나도 없어도 독립적으로 들어올 수 있다.
    signal.review_concepts = {_normalize_tag(c) for c in (review or []) if str(c).strip()}

    # 피드백이 하나도 없으면 정본 35개 파일을 훑을 이유가 없다. 매 런 두 번
    # 호출되는 경로라 빈 프로필에서 디스크를 건드리지 않는 편이 낫다.
    if not liked_list and not disliked_list:
        return signal

    lookup = index if index is not None else build_item_index()

    def collect(
        tokens: Iterable[str],
        sources: set[str],
        tags: set[str],
        concepts: set[str],
    ) -> None:
        for token in list(tokens)[-RECENT_FEEDBACK_WINDOW:]:
            facts = lookup.get(str(token).strip())
            if facts is None:
                continue
            if facts.source_key:
                sources.add(str(facts.source_key))
            tags.update(facts.tags)
            concepts.update(_normalize_tag(c) for c in facts.concepts)

    collect(
        liked_list, signal.liked_sources, signal.liked_tags, signal.liked_concepts
    )
    collect(
        disliked_list,
        signal.disliked_sources,
        signal.disliked_tags,
        signal.disliked_concepts,
    )

    contested_sources = signal.liked_sources & signal.disliked_sources
    signal.liked_sources -= contested_sources
    signal.disliked_sources -= contested_sources

    contested_tags = signal.liked_tags & signal.disliked_tags
    signal.liked_tags -= contested_tags
    signal.disliked_tags -= contested_tags

    contested_concepts = signal.liked_concepts & signal.disliked_concepts
    signal.liked_concepts -= contested_concepts
    signal.disliked_concepts -= contested_concepts

    return signal


def preference_score(
    signal: PreferenceSignal,
    source_key: str | None,
    tags: Iterable[str],
    concepts: Iterable[str] = (),
) -> int:
    """동점 타이브레이크용 점수. 합계는 ±3로 묶는다.

    가중치: 개념 ±2, 출처 ±2, 태그 ±1, 복습 개념 +1.

    개념과 출처를 태그보다 무겁게 두는 이유가 다르다. 개념은 어휘로 해소돼
    **재사용되는 단위**라 일치가 우연이 아니다. 출처는 사용자가 반복 소비하는
    채널이라 신호가 진하다. 반면 태그는 아이템당 5개까지 붙는 1회성 고유명사라
    일치해도 정보가 적다.
    """
    score = 0
    key = str(source_key) if source_key else None
    if key and key in signal.liked_sources:
        score += 2
    if key and key in signal.disliked_sources:
        score -= 2

    normalized_concepts = {_normalize_tag(c) for c in concepts}
    score += 2 * len(normalized_concepts & signal.liked_concepts)
    score -= 2 * len(normalized_concepts & signal.disliked_concepts)
    # 복습 가산(+1)은 선호 가산(+2)과 **겹쳐 쌓인다**. 그래서 §4.2 ⑦의
    # "습득도 낮고 선호 높은 개념"이 자동으로 +3을 받아 가장 앞선다 —
    # 조건을 따로 코딩하지 않아도 두 신호의 합에서 그 우선순위가 나온다.
    score += len(normalized_concepts & signal.review_concepts)

    normalized = {_normalize_tag(t) for t in tags}
    score += len(normalized & signal.liked_tags)
    score -= len(normalized & signal.disliked_tags)

    return max(-_MAX_PREFERENCE_SCORE, min(_MAX_PREFERENCE_SCORE, score))


def describe_for_prompt(signal: PreferenceSignal, limit: int = 5) -> tuple[str, str]:
    """Stage 1 랭킹 프롬프트에 넣을 (선호, 비선호) 문자열.

    개념을 앞에 두고 태그로 채운다. 출처 이름은 넣지 않는다 — 모델이 내용과
    무관하게 그 채널을 통째로 밀어올려 다양성이 죽는다. 출처 신호는 결정적
    타이브레이크로만 쓰는 편이 안전하다.
    """

    def render(concepts: set[str], tags: set[str]) -> str:
        merged = sorted(concepts) + [t for t in sorted(tags) if t not in concepts]
        return ", ".join(merged[:limit]) or "없음"

    return (
        render(signal.liked_concepts, signal.liked_tags),
        render(signal.disliked_concepts, signal.disliked_tags),
    )
