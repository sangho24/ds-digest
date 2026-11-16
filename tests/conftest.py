"""테스트 공통 설정."""

from __future__ import annotations

import pytest

import app.db as db
import app.interest_model as interest_model


@pytest.fixture(autouse=True)
def _isolate_interest_labels(tmp_path, monkeypatch):
    """실제 data/evals/interest_labels.jsonl 이 테스트의 선정 순서에 새지 않게 한다.

    filter_and_analyze 는 기본 경로의 관심도 표시를 읽어 선정을 조정한다. 표시가 쌓인 개발자
    컴퓨터에서만 선정 테스트 결과가 달라지면 안 되므로, 기본 경로를 없는 파일로 돌려 둔다.
    표시가 필요한 테스트는 이 경로에 직접 쓰거나 path 인자를 넘긴다.
    """
    monkeypatch.setattr(
        interest_model, "DEFAULT_LABELS_PATH", tmp_path / "no_interest_labels.jsonl"
    )


@pytest.fixture(autouse=True)
def _block_real_subscriber_tables(tmp_path, monkeypatch):
    """구독자 테이블(service role)로 가는 실제 Supabase 호출을 막는다.

    개발자 컴퓨터의 .env 에 Supabase 설정이 있으면 Settings 가 그것을 읽는다. 그 상태로
    선정(select_for_user)이나 daily_digest 를 태우는 테스트가 실제 subscribers,
    feedback_events 를 읽거나 sent_items 에 쓰면 안 된다. 클라이언트 생성 자체를 막아 두면
    app/db.py 의 구독자 함수들이 전부 fail-open 경로(None, False)로 빠진다. 구독자 데이터가
    필요한 테스트는 app.db 의 해당 함수를 직접 가짜로 바꾼다.
    """

    def _refuse():
        raise RuntimeError("테스트에서 실제 Supabase(service role) 호출 금지")

    monkeypatch.setattr(db, "get_supabase_service", _refuse)
    # 풀 조인도 실제 data/pool 이 테스트 결과에 새지 않게 없는 경로로 돌린다.
    monkeypatch.setattr(interest_model, "POOL_DIR", tmp_path / "no_pool")
