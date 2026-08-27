"""구조화 정본(`data/records/digest_*.json`) → 계측용 평탄 아이템 변환.

왜 필요한가:
    `evals/run.py`는 `evals/data/archive_items.json`이라는 정적 스냅샷을 읽는데,
    `evals/data/`는 .gitignore 대상이고 그 파일을 만드는 스크립트도 리포에 없었다.
    그래서 Weekly Evals 워크플로가 만들어진 이래 **5번 실행해 5번 전부 exit 2로
    실패**했다(2026-07-27 ~ 08-24). 품질 게이트가 한 번도 작동한 적이 없다.

    반면 `data/records/`는 커밋된다(.gitignore가 `!data/records/`로 예외 처리).
    즉 CI에도 이미 원천 데이터가 있다 — 형식만 안 맞았을 뿐이다. 이 모듈이 그
    형식 차이를 메워서 스냅샷 없이도 계측이 돌게 한다.

두 입력의 관계:
    정적 스냅샷은 baseline이 계측된 과거 구간(2026-03-30~07-16)을 담고 있고,
    records는 그 이후 구간을 담는다. 겹치지 않으므로 스냅샷이 있으면 그쪽을
    우선하고(baseline과 같은 창), 없을 때만 records로 대체한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "data" / "records"


def flatten_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    """DigestRecord 형태의 dict 하나를 계측용 아이템 리스트로 편다.

    metrics.py가 읽는 키만 뽑는다. 정본 스키마가 커져도 여기만 따라가면 된다.
    """
    date = record.get("date")
    items: list[dict[str, Any]] = []

    for entry in record.get("items") or []:
        raw = entry.get("raw") or {}
        analysis = entry.get("analysis") or {}
        key_points = analysis.get("key_points") or []

        items.append(
            {
                "date": date,
                "url": raw.get("url"),
                "source_key": raw.get("source_key"),
                "source_name": raw.get("source_name"),
                "tags": analysis.get("tags") or [],
                "relevance": analysis.get("relevance_score"),
                # evidence_proxy는 "타임스탬프가 하나라도 붙었나"만 본다.
                # key_points의 timestamp는 자막이 없으면 null이 정상이다.
                "has_timestamp": any(
                    (kp or {}).get("timestamp") for kp in key_points
                ),
                "production_ideas": analysis.get("production_ideas") or [],
                "quiz_count": len(analysis.get("quiz") or []),
                "one_line_summary": analysis.get("one_line_summary"),
            }
        )

    return items


def build_items(records_dir: Path | None = None) -> list[dict[str, Any]]:
    """`data/records/digest_YYYY-MM-DD.json` 전부를 날짜순으로 펴서 돌려준다.

    깨진 파일 하나가 계측 전체를 막으면 안 되므로 개별 파일 오류는 건너뛴다
    (계측은 통계라 몇 건 빠져도 의미가 유지된다).
    """
    directory = records_dir or RECORDS_DIR
    if not directory.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("digest_????-??-??.json")):
        try:
            with path.open("r", encoding="utf-8") as file:
                record = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            items.extend(flatten_record(record))

    return items


def date_span(items: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates = sorted({str(item.get("date")) for item in items if item.get("date")})
    return (dates[0], dates[-1]) if dates else (None, None)
