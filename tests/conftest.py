"""테스트 공통 설정."""

from __future__ import annotations

import pytest

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
