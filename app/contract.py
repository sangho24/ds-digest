"""공개 JSON 계약 — 다이제스트 소비자(Edith brief 등)를 위한 안정적 발행 포맷.

왜 별도 모듈인가:
    `data/records/digest_*.json`은 내부 정본이라 모델이 바뀌면 같이 바뀐다.
    소비자가 그걸 직접 읽으면 내부 리팩터링이 곧 외부 파손이 된다.
    HTML 아카이브를 긁는 것도 같은 문제다 — 디자인을 바꾸면 파서가 깨진다.

    이 모듈은 그 사이에 **버전이 박힌 얇은 계약**을 둔다. 내부 모델은 자유롭게
    바꾸되, `CONTRACT_VERSION`이 같으면 소비자가 보는 필드는 안 바뀐다.

발행 위치 (GitHub Pages가 `docs/`를 서빙):
    docs/latest.json        최신 1건 — 소비자의 기본 진입점
    docs/{YYYY-MM-DD}.json  날짜별
    docs/index.json         발행된 날짜 목록 (최신순)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from app.models import DigestItem

logger = structlog.get_logger()

CONTRACT_VERSION = 1
KST = ZoneInfo("Asia/Seoul")

# GitHub Pages 아카이브 루트. 소비자가 HTML 원문으로 되돌아갈 수 있게 링크만 제공한다.
ARCHIVE_BASE = "https://sangho24.github.io/ds-digest"


def today_kst() -> str:
    """다이제스트 날짜(KST 기준 YYYY-MM-DD).

    `datetime.now()`를 그대로 쓰면 GitHub Actions 러너가 UTC라 한국 시각 07:10
    발행분이 전날 날짜로 저장된다(실제로 그렇게 쌓여 있었다). 발행 시각이 KST
    아침이므로 날짜도 KST로 고정한다.
    """
    return datetime.now(KST).strftime("%Y-%m-%d")


def item_id(url: str) -> str:
    """URL 기반 안정 식별자.

    소비자가 아이템을 가리키고(피드백), 날짜를 넘어 중복을 판정할 수 있어야 한다.
    제목은 재분석 때 바뀔 수 있으므로 URL만 쓴다.
    """
    normalized = url.strip().rstrip("/").lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def _item_to_contract(item: DigestItem) -> dict[str, Any]:
    """DigestItem → 계약 dict. 내부 스코어링 내부값(scores/notes/triplets)은 뺀다.

    그것들은 아직 튜닝 중이라 자주 바뀐다. 계약에 넣으면 튜닝할 때마다
    버전을 올려야 한다.
    """
    raw = item.raw
    a = item.analysis
    return {
        "id": item_id(raw.url),
        "title": raw.title,
        "url": raw.url,
        "source": {
            "type": raw.source_type.value
            if hasattr(raw.source_type, "value")
            else str(raw.source_type),
            "name": raw.source_name,
        },
        "published_at": raw.published_at.isoformat() if raw.published_at else None,
        "summary": a.one_line_summary,
        "tags": list(a.tags),
        "relevance": a.relevance_score,
        "key_points": [
            {"point": kp.point, "timestamp": kp.timestamp} for kp in a.key_points
        ],
        "ideas": list(a.production_ideas),
        "quiz": [
            {
                "question": q.question,
                "options": list(q.options),
                "answer_index": q.answer_index,
                "explanation": q.explanation,
            }
            for q in a.quiz
        ],
    }


def build_contract(
    digest_items: list[DigestItem],
    date_str: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """발행용 계약 dict를 만든다."""
    return {
        "contract_version": CONTRACT_VERSION,
        "date": date_str,
        "generated_at": generated_at or datetime.now(KST).isoformat(),
        "count": len(digest_items),
        "archive_url": f"{ARCHIVE_BASE}/{date_str}.html",
        "items": [_item_to_contract(i) for i in digest_items],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def rebuild_index(docs_dir: Path) -> Path:
    """docs/index.json — 발행된 날짜 목록(최신순).

    소비자가 "며칠 결번인지"를 판정하려면 목록이 필요하다. HTML index를 파싱하는
    대신 이걸 읽게 한다.
    """
    dates = sorted(
        (p.stem for p in docs_dir.glob("????-??-??.json")),
        reverse=True,
    )
    path = docs_dir / "index.json"
    _write_json(
        path,
        {
            "contract_version": CONTRACT_VERSION,
            "updated_at": datetime.now(KST).isoformat(),
            "count": len(dates),
            "latest": dates[0] if dates else None,
            "dates": dates,
        },
    )
    return path


def publish(
    digest_items: list[DigestItem],
    date_str: str,
    docs_dir: Path,
    generated_at: str | None = None,
) -> dict[str, Path]:
    """계약 JSON 3종을 docs/에 쓴다.

    HTML 저장 실패와 무관하게 독립적으로 동작해야 하므로 호출부에서 별도 지점에
    둔다. 반환은 쓴 경로들(로깅·테스트용).
    """
    docs_dir.mkdir(parents=True, exist_ok=True)
    payload = build_contract(digest_items, date_str, generated_at)

    dated = docs_dir / f"{date_str}.json"
    _write_json(dated, payload)

    latest = docs_dir / "latest.json"
    _write_json(latest, payload)

    index = rebuild_index(docs_dir)

    logger.info(
        "contract_published",
        date=date_str,
        count=len(digest_items),
        version=CONTRACT_VERSION,
    )
    return {"dated": dated, "latest": latest, "index": index}
