"""기존 `data/records/digest_*.json` 정본을 공개 JSON 계약으로 소급 발행한다.

계약을 오늘부터만 내면 소비자가 과거를 못 읽는다. 정본은 이미 리포에 쌓여 있으므로
한 번 변환해두면 아카이브 전체가 JSON으로 열린다.

주의: 기존 레코드의 `date`는 UTC 기준으로 찍혀 있다(KST 고정은 이번 변경부터).
소급분은 원본 날짜를 그대로 보존한다 — 임의 보정하면 HTML 아카이브와 어긋난다.

사용: python -m scripts.backfill_contract
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.contract import build_contract, rebuild_index
from app.models import DigestRecord

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    records_dir = ROOT / "data" / "records"
    docs_dir = ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(records_dir.glob("digest_????-??-??.json"))
    if not files:
        print("소급할 레코드 없음", file=sys.stderr)
        return 1

    written, skipped = 0, 0
    for path in files:
        date_str = path.stem.removeprefix("digest_")
        try:
            record = DigestRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — 개별 실패가 전체를 막지 않게
            print(f"skip {date_str}: {e}", file=sys.stderr)
            skipped += 1
            continue

        payload = build_contract(record.items, date_str, record.generated_at)
        (docs_dir / f"{date_str}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written += 1

    index = rebuild_index(docs_dir)
    latest_date = json.loads(index.read_text(encoding="utf-8"))["latest"]
    if latest_date:
        # latest.json은 가장 최신 날짜의 사본
        (docs_dir / "latest.json").write_text(
            (docs_dir / f"{latest_date}.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    print(f"발행 {written}건 · 건너뜀 {skipped}건 · latest={latest_date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
