"""소스 퍼널 지표 — 발송 아이템만으로는 볼 수 없는 것을 본다.

`source_reach`는 소스 목록을 **발송된 아이템**에서 만든다. 그래서 한 번도
발송되지 않은 소스는 목록에 없고 "장기 미등장"으로 잡히지도 않는다.
실측: arXiv가 40일간 발송 0건인데 지표는 "소스 18개 / 미등장 1개"로 정상 보고했다.

이 모듈은 파이프라인이 남긴 수집 기록(app/source_stats.py)을 읽어 분모를 채운다.
기록이 없으면(퍼널 도입 전 구간) 빈 결과를 돌려주고, 임계값 규칙은 통과한다 —
없는 데이터로 경보를 울리지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def source_funnel(path: Path | None = None, since: str | None = None) -> dict[str, Any]:
    """소스별 (발송 / 수집) 도달률과 굶는 소스 목록."""
    try:
        from app.source_stats import aggregate
    except ImportError:  # app 패키지 없이 단독 실행되는 경우
        return {"days": 0, "source_count": 0, "starved_sources": [], "starved_count": 0, "sources": {}}
    return aggregate(path, since)
