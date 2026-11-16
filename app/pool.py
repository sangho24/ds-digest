"""분석이 끝난 후보 풀을 로컬 JSON으로 저장하고 다시 읽는다.

분석(LLM)은 모든 사용자가 공유하고 선정만 사용자별로 다시 하려면, 선정에서 탈락한
후보의 분석 결과도 남아 있어야 한다. 정본(app/records.py)은 발송된 건만 담으므로
이 모듈이 후보 전체를 data/pool/pool_{date}.json 에 따로 둔다.

저장소가 커지지 않도록 transcript 와 body 는 빼고, POOL_RETENTION_DAYS 보다 오래된
파일은 저장할 때 지운다. 선정(app/analyzer.py 의 select_for_user)은 분석 결과만
보므로 원문이 없어도 같은 결과를 낸다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from app.models import ContentAnalysis, DigestItem, RawContent

logger = structlog.get_logger()

POOL_SCHEMA_VERSION = 1
# 풀 파일 보존 기간(일). 저장 시 이보다 오래된 파일을 지운다.
POOL_RETENTION_DAYS = 30
# 풀에 싣지 않는 raw 필드. 원문은 아이템당 수십 KB라 저장소를 금방 키운다.
POOL_RAW_EXCLUDE = {"transcript", "body"}

_POOL_PREFIX = "pool_"


@dataclass
class ScoredCandidate:
    """분석이 끝난 후보 한 건과, 선정에 필요한데 DigestItem 에 없는 값.

    absolute_score 는 상대 평가가 섞이기 전의 절대 점수다. 바닥값 판정은 이 값으로
    한다(app/analyzer.py 의 select_for_user). selected_default 는 풀을 저장한 런에서
    default 사용자에게 선정됐는지의 기록이며 선정 계산에는 쓰지 않는다.

    relative_rating 은 상대 평가가 이 후보에 준 원점수(0~10)다. 상대 평가가 없었거나
    이 후보가 응답에서 빠졌으면 None 이다. item 의 relevance_score 는 기본 배합으로
    섞은 공용 값이고, 난이도 지시가 있는 사용자의 선정 점수는 이 값으로 그 사람의
    배합을 다시 섞어 만든다(select_for_user). 이 필드가 생기기 전의 풀 파일에는 없다.
    """

    item: DigestItem
    absolute_score: int
    selected_default: bool = False
    relative_rating: int | None = None


def pool_dir(base_dir: Path | None = None) -> Path:
    """풀 디렉토리. base_dir 를 주면 그 아래 data/pool 이다(app/records.py 와 같은 규칙)."""
    root = base_dir if base_dir is not None else Path(__file__).parent.parent
    return root / "data" / "pool"


def pool_path(date_str: str, base_dir: Path | None = None) -> Path:
    return pool_dir(base_dir) / f"{_POOL_PREFIX}{date_str}.json"


def snapshot_pool(
    candidates: list[ScoredCandidate], selected: list[DigestItem]
) -> list[dict[str, Any]]:
    """후보 목록을 JSON 으로 쓸 수 있는 dict 목록으로 굳힌다.

    선정 직후, 발송용 후처리(조정 노트 추가, 개념 표준명 치환)가 아이템을 바꾸기
    **전에** 불러야 한다. 그래야 풀을 다시 읽어 선정했을 때 같은 입력이 된다.
    """
    selected_ids = {id(d) for d in selected}
    return [
        {
            "raw": c.item.raw.model_dump(mode="json", exclude=POOL_RAW_EXCLUDE),
            "analysis": c.item.analysis.model_dump(mode="json"),
            "absolute_score": c.absolute_score,
            "selected_default": id(c.item) in selected_ids,
            "relative_rating": c.relative_rating,
        }
        for c in candidates
    ]


def prune_pool(
    date_str: str,
    base_dir: Path | None = None,
    retention_days: int = POOL_RETENTION_DAYS,
) -> list[Path]:
    """date_str 기준 retention_days 보다 오래된 풀 파일을 지우고 지운 경로를 돌려준다.

    파일 이름의 날짜로 판단한다. 날짜로 읽히지 않는 파일은 건드리지 않는다.
    """
    cutoff = date.fromisoformat(date_str) - timedelta(days=retention_days)
    removed: list[Path] = []
    for path in sorted(pool_dir(base_dir).glob(f"{_POOL_PREFIX}*.json")):
        try:
            file_date = date.fromisoformat(path.stem[len(_POOL_PREFIX):])
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def save_pool(
    entries: list[dict[str, Any]],
    date_str: str,
    base_dir: Path | None = None,
) -> Path | None:
    """snapshot_pool 의 결과를 data/pool/pool_{date}.json 에 저장한다.

    풀은 정본이 아니라 재선정용 부산물이다. 저장이나 정리가 실패해도 발송을 막지
    않도록 경고 로그만 남긴다(fail-open). 저장하지 못했으면 None 을 돌려준다.
    """
    path = pool_path(date_str, base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": date_str,
            "generated_at": datetime.now().isoformat(),
            "schema_version": POOL_SCHEMA_VERSION,
            "items": entries,
        }
        # 들여쓰기 없이 쓴다. 매일 커밋되는 파일이라 크기가 곧 저장소 크기다.
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("pool_save_failed", path=str(path), error=str(e))
        return None

    try:
        removed = prune_pool(date_str, base_dir)
        if removed:
            logger.info("pool_pruned", removed=[p.name for p in removed])
    except Exception as e:
        logger.warning("pool_prune_failed", error=str(e))
    return path


def candidates_from_entries(entries: list[dict[str, Any]]) -> list[ScoredCandidate]:
    """snapshot_pool 이 만든 dict 목록을 select_for_user 입력으로 되돌린다.

    파일에서 읽은 풀(load_pool)과 런 메모리에 있는 스냅샷(구독자 발송)이 같은 변환을
    거치게 한다. 순서는 그대로 둔다.
    """
    return [
        ScoredCandidate(
            item=DigestItem(
                raw=RawContent.model_validate(entry["raw"]),
                analysis=ContentAnalysis.model_validate(entry["analysis"]),
            ),
            absolute_score=entry["absolute_score"],
            selected_default=bool(entry.get("selected_default", False)),
            relative_rating=entry.get("relative_rating"),
        )
        for entry in entries
    ]


def load_pool(date_str: str, base_dir: Path | None = None) -> list[ScoredCandidate]:
    """저장된 풀을 읽어 select_for_user 에 그대로 넣을 수 있는 후보 목록으로 돌려준다.

    순서는 저장된(= 분석된) 순서 그대로다. 선정의 동점 처리가 입력 순서에 기대므로
    바꾸면 안 된다. 파일이 없으면 FileNotFoundError.
    """
    path = pool_path(date_str, base_dir)
    if not path.exists():
        raise FileNotFoundError(f"풀 파일이 없다: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return candidates_from_entries(data.get("items", []))
