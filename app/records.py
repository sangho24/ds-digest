"""다이제스트 산출의 구조화 정본을 로컬 JSON으로 저장한다.

로컬 JSON이 정본(source of truth)이고, Supabase는 best-effort 미러다(app/db.py).
발송용 HTML은 이 레코드로부터 파생된다. 기존에는 HTML만 저장되고 분석 결과가
발송 후 폐기됐는데, 이 모듈이 구조화 레코드를 리포에 영속화한다.
"""
from datetime import datetime
from pathlib import Path

import structlog

from app.models import DigestItem, DigestRecord

logger = structlog.get_logger()


def save_digest_records(
    digest_items: list[DigestItem],
    date_str: str,
    base_dir: Path | None = None,
) -> Path:
    """digest_items를 DigestRecord로 감싸 data/records/digest_{date}.json에 저장한다.

    - base_dir: 저장 루트 override(테스트용). 기본은 리포 루트(app/의 부모).
    - generated_at은 다른 코드(daily_digest 등)와 맞춰 datetime.now().isoformat() 사용.
    - 로컬 정본이므로 실패를 과하게 삼키지 않는다: 로깅 후 되던진다
      (기존 HTML 저장이 예외를 흘려보내는 수준과 일치).

    반환: 저장된 JSON 파일 경로.
    """
    # app/records.py 기준 리포 루트: app/ 의 부모 디렉토리
    root = base_dir if base_dir is not None else Path(__file__).parent.parent
    records_dir = root / "data" / "records"
    records_dir.mkdir(parents=True, exist_ok=True)

    record = DigestRecord(
        date=date_str,
        generated_at=datetime.now().isoformat(),
        items=digest_items,
    )
    path = records_dir / f"digest_{date_str}.json"
    try:
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    except Exception as e:
        # 정본 저장 실패는 조용히 넘기지 않는다 — 로깅 후 되던져 상위가 인지하게 한다.
        logger.error("records_save_failed", path=str(path), error=str(e))
        raise
    return path
