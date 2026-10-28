"""소스 퍼널 지표 — 발송 아이템만으로는 볼 수 없는 것을 본다.

`source_reach`는 소스 목록을 **발송된 아이템**에서 만든다. 그래서 한 번도
발송되지 않은 소스는 목록에 없고 "장기 미등장"으로 잡히지도 않는다.
실측: arXiv가 40일간 발송 0건인데 지표는 "소스 18개 / 미등장 1개"로 정상 보고했다.

이 모듈은 파이프라인이 남긴 수집 기록(app/source_stats.py)을 읽어 분모를 채운다.
기록이 없으면(퍼널 도입 전 구간) 빈 결과를 돌려주고, 임계값 규칙은 통과한다 —
없는 데이터로 경보를 울리지 않는다.

같은 기록에서 "현재 설정된 소스 키"(`current_keys`)도 꺼내 준다. `source_reach` 가 퇴역
소스를 장기 미등장으로 세지 않게 하는 데 쓴다(§50). 기록이 없거나 import 가 실패하면 None.
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


def current_keys() -> set[str] | None:
    """현재 설정된 소스 키 집합. 기록이 없거나 app 패키지를 못 불러오면 None.

    None 과 빈 set 모두 source_reach 에서는 "판정을 좁히지 않음"으로 읽히지만, 호출자가
    "기록이 없어서"와 "기록은 있는데 키가 없어서"를 구분할 수 있게 전자는 None 으로 돌려준다.
    """
    try:
        from app.source_stats import current_source_keys, load
    except ImportError:  # app 패키지 없이 단독 실행되는 경우
        return None
    rows = load()
    if not rows:
        return None
    return current_source_keys(rows)
