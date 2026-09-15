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

from app.analyzer import source_family  # 정규화와 같은 계열 규칙을 쓴다

logger = structlog.get_logger()

ROOT = Path(__file__).resolve().parent.parent
STATS_PATH = ROOT / "data" / "source_stats.jsonl"
KST = ZoneInfo("Asia/Seoul")

# 수집 0건을 "수집기가 깨졌다"로 단정하려면 이 정도 기간은 봐야 한다.
# 월간 발행 블로그도 30일이면 최소 한 번은 나온다.
MIN_DAYS_FOR_SILENCE = 30


def _source_key(item: Any) -> str:
    return str(getattr(item, "source_key", "") or getattr(item, "source_name", "") or "(소스 없음)")


def _count(items: Iterable[Any]) -> Counter:
    return Counter(_source_key(i) for i in items)


def build_funnel(
    collected: Iterable[Any],
    candidates: Iterable[Any],
    delivered: Iterable[Any],
    expected: Iterable[str] | None = None,
) -> dict[str, dict[str, int]]:
    """소스별 3단 퍼널을 만든다.

    expected는 **설정상 존재해야 하는** 소스 키 목록이다. 이게 없으면 수집이
    0건인 소스가 퍼널에 아예 나타나지 않아, 또 한 번 투명인간이 된다.

    실제로 그 일이 있었다: arXiv는 URL이 http라 301 리다이렉트에서 본문이 비어
    **40일간 한 건도 수집되지 않았는데**, 퍼널만으로는 그걸 볼 수 없었다.
    수집 0건은 "굶는 소스"보다 나쁜 상태이므로 반드시 보여야 한다.
    """
    c, n, d = _count(collected), _count(candidates), _count(delivered)
    keys = set(c) | set(n) | set(d) | {str(e) for e in (expected or []) if str(e).strip()}
    return {
        key: {"collected": c.get(key, 0), "candidates": n.get(key, 0), "delivered": d.get(key, 0)}
        for key in sorted(keys)
    }


def record(
    collected: Iterable[Any],
    candidates: Iterable[Any],
    delivered: Iterable[Any],
    date: str | None = None,
    path: Path | None = None,
    expected: Iterable[str] | None = None,
) -> dict[str, dict[str, int]]:
    """이번 런의 퍼널을 한 줄로 덧붙인다."""
    funnel = build_funnel(collected, candidates, delivered, expected)
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
    silent = [k for k, v in funnel.items() if not v["collected"]]
    logger.info(
        "source_funnel_recorded",
        sources=len(funnel),
        collected=sum(v["collected"] for v in funnel.values()),
        delivered=sum(v["delivered"] for v in funnel.values()),
        starved_today=starved,
        # 수집이 0건인 소스. 설정돼 있는데 아무것도 안 오면 수집기가 깨진 것이다.
        silent_today=silent,
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


def current_source_keys(rows: list[dict[str, Any]]) -> set[str]:
    """가장 최근 날짜에 기록된 모든 행의 소스 키 합집합 = "현재 설정된 소스".

    각 행의 `sources`에는 그날 설정된 키가 0건까지 전부 들어 있다(record의 expected).
    그래서 가장 최근 기록의 키가 곧 지금 설정이다.

    마지막 **행 하나**가 아니라 마지막 **날짜의 모든 행**을 합치는 이유:
    - 판정 대상이 좁아지는 쪽의 실패가 더 비싸다. 같은 날 수동 재실행이
      다른 설정(예: 로컬 .env에 소스 일부만 둔 채 드라이런 없이 실행)이나
      expected 없이 기록한 행을 남기면, 마지막 행만 볼 때 진짜 설정된 소스가
      판정에서 빠져 FAIL 게이트가 조용히 무뎌진다. 합집합은 그날 한 번이라도
      설정돼 있던 키를 놓치지 않는다.
    - 대가는 소스를 뺀 당일에 옛 설정 행이 같은 날짜에 함께 있으면, 빠진 키가
      그날 하루 더 판정에 남는다는 것뿐이다. 다음 날 행이 쌓이면 사라진다.
    - 날짜는 파일 순서가 아니라 date 문자열의 최댓값으로 고른다.
    """
    if not rows:
        return set()
    latest = max(str(row.get("date", "")) for row in rows)
    keys: set[str] = set()
    for row in rows:
        if str(row.get("date", "")) == latest:
            keys.update(row["sources"])
    return keys


def aggregate(path: Path | None = None, since: str | None = None) -> dict[str, Any]:
    """기간 전체의 소스별 퍼널 합계와 도달률.

    reach = delivered / collected (§3.6이 정의한 그 비율).
    starved = 수집은 됐는데 한 번도 발송되지 않은 소스. 이게 원래 안 보이던 것이다.

    판정(starved·silent·confirmed)은 **현재 설정된 소스**(current_source_keys)만 본다.
    설정에서 뺀 소스가 과거 행에 남아 30일 뒤에도 confirmed_silent로 게이트를
    막으면, 소스를 정리해도 판정이 영영 안 바뀐다(2026-09-15 죽은 소스 6개 정리 후
    35일 시뮬레이션에서 그대로 FAIL). 뺀 소스는 retired_sources로 보여주기만 한다.
    누적 합산과 days(전체 기록 기준)는 그대로다.

    since와의 관계: since는 하한만 거르므로, 걸러진 행이 하나라도 있으면 그 최근
    날짜는 전체 기록의 최근 날짜와 같다. 행이 없으면 현재 키도 없어 판정이 빈다.
    """
    totals: dict[str, dict[str, int]] = {}
    days: set[str] = set()

    rows = load(path, since)
    current = current_source_keys(rows)

    for row in rows:
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
    # 판정 대상: 현재 설정된 소스만. sources(누적 합계)는 기록 확인용으로 전부 남긴다.
    #
    # starved에도 같은 한정을 건다. 빼지 않으면 "수집은 됐지만 한 번도 안 나간 채
    # 설정에서 빠진 소스"가 과거 행 때문에 WARN으로 계속 켜져 있어, silent만
    # 정리되고 starved는 안 정리되는 어긋난 판정이 된다. 뺀 소스에 내릴 처방은 없다.
    active = {k: v for k, v in sources.items() if k in current}
    retired = sorted(k for k in sources if k not in current)
    starved = sorted(
        k for k, v in active.items() if v["collected"] > 0 and v["delivered"] == 0
    )
    # 계열 단위 집계. arXiv를 14개 카테고리로 늘리면서 소스 키가 카테고리별로
    # 갈라졌는데, 하루 정원이 5칸이라 **대부분의 카테고리는 매일 굶는다**.
    # 키 기준으로 세면 starved가 상시 11건 이상이 되어 경보가 늘 켜져 있고,
    # 늘 켜져 있는 경보는 읽히지 않는다. 우리가 실제로 알고 싶은 건
    # "cs.SI가 이번 주에 안 나갔다"가 아니라 "arXiv 계열이 통째로 안 나간다"다.
    #
    # 계열도 현재 키로만 합산한다. arXiv처럼 계열 안 일부 키만 설정에 남으면
    # 남은 키만으로 판정한다. 빠진 카테고리의 과거 수집 건수가 계열 합계를 채워
    # "살아 있음"으로 가리면, 지금 설정된 카테고리가 전부 0건이어도 못 잡는다.
    # 계열은 남은 키가 하나라도 있으면 retired가 아니다.
    families: dict[str, dict[str, int]] = {}
    for key, bucket in active.items():
        fam = families.setdefault(
            source_family(key), {"collected": 0, "candidates": 0, "delivered": 0}
        )
        for stage in fam:
            fam[stage] += bucket[stage]
    starved_families = sorted(
        k for k, v in families.items() if v["collected"] > 0 and v["delivered"] == 0
    )
    # 기간 내내 수집이 0건인 소스 = 수집기가 깨졌거나 설정이 잘못된 것.
    # "굶는 소스"보다 나쁜 상태라 따로 센다.
    silent = sorted(k for k, v in active.items() if v["collected"] == 0)
    silent_families = sorted(k for k, v in families.items() if v["collected"] == 0)
    # 설정에서 빠진 계열: 계열 안에 현재 키가 하나도 남지 않은 것. 보고 전용.
    retired_families = sorted({source_family(k) for k in retired} - set(families))
    # "수집 0건"을 게이트를 막는 FAIL로 쓰려면 **관측 기간이 충분해야 한다.**
    # 실측: 퍼널 2일치로 판정하니 toss·netflix·airbnb·우아한형제들처럼 주간·월간
    # 발행하는 블로그 12곳이 전부 "침묵"으로 잡혀 FAIL이 났다. 이건 수집기가
    # 깨진 게 아니라 그냥 그 주에 글이 안 올라온 것이다.
    #
    # 잡으려던 건 다른 상태다 — arXiv가 리다이렉트 때문에 40일간 0건이었던 것.
    # 그건 기간을 길게 잡으면 확실히 드러나고, 짧게 잡으면 정상 소스와 섞인다.
    # 기간이 모자라면 "문제 없음"이 아니라 **"판단할 근거가 없음"**이므로 비운다.
    confirmed = silent_families if len(days) >= MIN_DAYS_FOR_SILENCE else []

    return {
        "days": len(days),
        "source_count": len(sources),
        "starved_sources": starved,
        "starved_count": len(starved),
        "starved_families": starved_families,
        "starved_family_count": len(starved_families),
        "silent_sources": silent,
        "silent_count": len(silent),
        "silent_families": silent_families,
        "silent_family_count": len(silent_families),
        # 관측 기간이 MIN_DAYS_FOR_SILENCE일 이상일 때만 채워진다.
        "confirmed_silent_families": confirmed,
        "confirmed_silent_count": len(confirmed),
        "enough_days_for_silence": len(days) >= MIN_DAYS_FOR_SILENCE,
        # 가장 최근 기록에 없는 = 설정에서 빠진 소스. 판정(starved·silent·confirmed)
        # 에는 넣지 않고, 정리가 반영됐는지 리포트에서 확인하는 용도다.
        "current_source_count": len(active),
        "retired_sources": retired,
        "retired_families": retired_families,
        "sources": sources,
    }
