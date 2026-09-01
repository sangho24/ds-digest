"""소스별 수집→발송 퍼널 기록 — 지표의 사각지대를 메운다.

문제:
    `evals.metrics.source_reach`는 **발송된 아이템**에서 소스 목록을 만든다.
    그래서 한 번도 발송되지 않은 소스는 목록에 아예 없고, "장기 미등장"으로
    잡힐 수도 없다. 가장 나쁜 실패 모드 — 피드가 죽어서 아무것도 안 나오는 것,
    또는 수집은 되는데 매번 선정에서 탈락하는 것 — 를 구조적으로 못 본다.

    실측: arXiv는 `ARXIV_CATEGORIES=cs.LG,stat.ML`로 매일 수집되는데 40일간
    발송 0건이었다. 그런데 지표는 "소스 18개 / 장기 미등장 1개"라고 보고했다.
    arXiv는 그 18개에 들어 있지도 않다. 투명인간이다.

    v2 §3.6은 이 지표를 "소스별 **(발송 / 수집)** 비율"로 정의했다. 분모인
    수집량이 어디에도 기록되지 않아 실제로는 분자만 세고 있었다.

해법:
    런마다 소스별 퍼널 3단(수집 → 후보 → 발송)을 남긴다. 그러면 분모가 생겨
    도달률을 실제로 계산할 수 있고, "수집되는데 한 번도 안 나가는 소스"가
    이름을 갖고 드러난다.

        collected  수집기가 가져온 건수 (dedup 전)
        candidates dedup·채널캡을 통과해 분석에 들어간 건수
        delivered  최종 다이제스트에 실린 건수

    수집과 후보를 나누는 이유: 도달률이 0일 때 원인이 갈린다. candidates가 0이면
    중복 제거에서 다 걸린 것이고(소스가 오래된 것만 낸다), candidates는 있는데
    delivered가 0이면 채점·선정에서 밀린 것이다. 처방이 다르다.

저장:
    data/source_stats.jsonl (append-only, 커밋)
    러너가 ephemeral이라 커밋하지 않으면 매 런 사라진다.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent
STATS_PATH = ROOT / "data" / "source_stats.jsonl"
KST = ZoneInfo("Asia/Seoul")


def _source_key(item: Any) -> str:
    return str(getattr(item, "source_key", "") or getattr(item, "source_name", "") or "(소스 없음)")


def _count(items: Iterable[Any]) -> Counter:
    return Counter(_source_key(i) for i in items)


def build_funnel(
    collected: Iterable[Any],
    candidates: Iterable[Any],
    delivered: Iterable[Any],
) -> dict[str, dict[str, int]]:
    """소스별 3단 퍼널을 만든다. 어느 단계에서든 등장한 소스는 전부 포함한다."""
    c, n, d = _count(collected), _count(candidates), _count(delivered)
    return {
        key: {"collected": c.get(key, 0), "candidates": n.get(key, 0), "delivered": d.get(key, 0)}
        for key in sorted(set(c) | set(n) | set(d))
    }


def record(
    collected: Iterable[Any],
    candidates: Iterable[Any],
    delivered: Iterable[Any],
    date: str | None = None,
    path: Path | None = None,
) -> dict[str, dict[str, int]]:
    """이번 런의 퍼널을 한 줄로 덧붙인다."""
    funnel = build_funnel(collected, candidates, delivered)
    entry = {
        "date": date or datetime.now(KST).strftime("%Y-%m-%d"),
        "recorded_at": datetime.now(KST).isoformat(),
        "sources": funnel,
    }

    target = path or STATS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    starved = [k for k, v in funnel.items() if v["collected"] and not v["delivered"]]
    logger.info(
        "source_funnel_recorded",
        sources=len(funnel),
        collected=sum(v["collected"] for v in funnel.values()),
        delivered=sum(v["delivered"] for v in funnel.values()),
        starved_today=starved,
    )
    return funnel


def load(path: Path | None = None, since: str | None = None) -> list[dict[str, Any]]:
    """기록을 읽는다. 깨진 줄은 건너뛴다."""
    target = path or STATS_PATH
    if not target.exists():
        return []

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
            if not isinstance(row, dict) or not isinstance(row.get("sources"), dict):
                continue
            if since and str(row.get("date", "")) < since:
                continue
            rows.append(row)
    return rows


def aggregate(path: Path | None = None, since: str | None = None) -> dict[str, Any]:
    """기간 전체의 소스별 퍼널 합계와 도달률.

    reach = delivered / collected (§3.6이 정의한 그 비율).
    starved = 수집은 됐는데 한 번도 발송되지 않은 소스. 이게 원래 안 보이던 것이다.
    """
    totals: dict[str, dict[str, int]] = {}
    days: set[str] = set()

    for row in load(path, since):
        days.add(str(row.get("date")))
        for key, funnel in row["sources"].items():
            bucket = totals.setdefault(key, {"collected": 0, "candidates": 0, "delivered": 0})
            for stage in bucket:
                try:
                    bucket[stage] += int(funnel.get(stage, 0))
                except (TypeError, ValueError):
                    continue

    sources = {
        key: {
            **bucket,
            "reach": round(bucket["delivered"] / bucket["collected"], 4)
            if bucket["collected"]
            else 0.0,
        }
        for key, bucket in sorted(totals.items())
    }
    starved = sorted(
        k for k, v in sources.items() if v["collected"] > 0 and v["delivered"] == 0
    )

    return {
        "days": len(days),
        "source_count": len(sources),
        "starved_sources": starved,
        "starved_count": len(starved),
        "sources": sources,
    }
