"""
피드백 루프 E2E 테스트

Supabase 없이 로컬 JSON fallback으로 전체 루프를 검증한다.
  사용자 피드백 → 프로필 업데이트 → 분석 프롬프트 반영

실행: pytest tests/test_feedback_loop.py -v
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from app.models import FeedbackPayload, UserProfile
from app.feedback import process_feedback, load_profile, save_profile
from app.analyzer import ANALYSIS_PROMPT


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    """각 테스트마다 독립된 프로필 파일 경로 + Supabase 비활성화."""
    profile_path = tmp_path / "profile.json"

    # feedback.py의 PROFILE_PATH를 임시 경로로 교체
    import app.feedback as fb_module
    monkeypatch.setattr(fb_module, "PROFILE_PATH", profile_path)

    # Supabase 미설정 상태 강제 (로컬 JSON fallback 사용)
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_KEY", "")

    # lru_cache된 get_settings 무효화
    from app.config import get_settings
    get_settings.cache_clear()

    yield profile_path

    get_settings.cache_clear()


# ──────────────────────────────────────────────
# 기본 피드백 처리
# ──────────────────────────────────────────────

def test_like_updates_profile():
    payload = FeedbackPayload(item_url="https://example.com/a", action="like")
    profile = process_feedback(payload)

    assert "https://example.com/a" in profile.liked_item_ids
    assert "https://example.com/a" not in profile.disliked_item_ids


def test_dislike_updates_profile():
    payload = FeedbackPayload(item_url="https://example.com/b", action="dislike")
    profile = process_feedback(payload)

    assert "https://example.com/b" in profile.disliked_item_ids
    assert "https://example.com/b" not in profile.liked_item_ids


def test_keyword_request_updates_profile():
    payload = FeedbackPayload(
        item_url="https://example.com/c",
        action="keyword_request",
        keyword="causal inference",
    )
    profile = process_feedback(payload)

    assert "causal inference" in profile.keyword_requests


def test_keyword_is_lowercased():
    payload = FeedbackPayload(
        item_url="https://example.com/d",
        action="keyword_request",
        keyword="  MLOps  ",
    )
    profile = process_feedback(payload)

    assert "mlops" in profile.keyword_requests
    assert "MLOps" not in profile.keyword_requests


# ──────────────────────────────────────────────
# 중복 방지
# ──────────────────────────────────────────────

def test_like_not_duplicated():
    payload = FeedbackPayload(item_url="https://example.com/e", action="like")
    process_feedback(payload)
    process_feedback(payload)  # 두 번 호출

    profile = load_profile()
    assert profile.liked_item_ids.count("https://example.com/e") == 1


def test_keyword_not_duplicated():
    payload = FeedbackPayload(
        item_url="https://example.com/f",
        action="keyword_request",
        keyword="feature store",
    )
    process_feedback(payload)
    process_feedback(payload)

    profile = load_profile()
    assert profile.keyword_requests.count("feature store") == 1


# ──────────────────────────────────────────────
# 프로필 영속성
# ──────────────────────────────────────────────

def test_profile_persists_across_load(isolated_profile):
    """저장된 프로필이 다음 load_profile() 호출에서 복원되는지 확인."""
    profile = UserProfile(
        user_id="default",
        preferred_topics=["MLOps", "A/B testing"],
        keyword_requests=["feature store"],
        liked_item_ids=["https://example.com/x"],
    )
    save_profile(profile)

    loaded = load_profile()
    assert loaded.preferred_topics == ["MLOps", "A/B testing"]
    assert loaded.keyword_requests == ["feature store"]
    assert "https://example.com/x" in loaded.liked_item_ids


def test_profile_file_is_valid_json(isolated_profile):
    """프로필 파일이 유효한 JSON인지 확인."""
    process_feedback(FeedbackPayload(item_url="https://example.com/g", action="like"))

    data = json.loads(isolated_profile.read_text(encoding="utf-8"))
    assert "liked_item_ids" in data
    assert isinstance(data["liked_item_ids"], list)


# ──────────────────────────────────────────────
# 프롬프트 반영 (핵심: 피드백이 분석에 실제로 영향을 주는가)
# ──────────────────────────────────────────────

def test_keyword_appears_in_analysis_prompt():
    """keyword_request가 다음 날 분석 프롬프트에 반영되는지 확인."""
    process_feedback(FeedbackPayload(
        item_url="https://example.com/h",
        action="keyword_request",
        keyword="llm evaluation",
    ))

    profile = load_profile()
    assert "llm evaluation" in profile.keyword_requests

    # 프롬프트 포맷팅 시 keyword_requests가 포함되는지 검증
    prompt = ANALYSIS_PROMPT.format(
        topics=", ".join(profile.preferred_topics) or "data science",
        keywords=", ".join(profile.keyword_requests[-5:]),
        concept_vocabulary="(아직 없음)",
        standing_note="없음",
        title="Test Article",
        source_name="Test",
        source_type="rss",
        content="some content",
        timestamp_instruction="   - timestamp는 null.",
    )
    assert "llm evaluation" in prompt


def test_multiple_feedbacks_accumulate():
    """여러 피드백이 누적되어 프로필에 모두 반영되는지 확인."""
    process_feedback(FeedbackPayload(item_url="https://a.com/1", action="like"))
    process_feedback(FeedbackPayload(item_url="https://a.com/2", action="dislike"))
    process_feedback(FeedbackPayload(
        item_url="https://a.com/3", action="keyword_request", keyword="ray"
    ))
    process_feedback(FeedbackPayload(
        item_url="https://a.com/4", action="keyword_request", keyword="dbt"
    ))

    profile = load_profile()
    assert "https://a.com/1" in profile.liked_item_ids
    assert "https://a.com/2" in profile.disliked_item_ids
    assert "ray" in profile.keyword_requests
    assert "dbt" in profile.keyword_requests


# ──────────────────────────────────────────────
# Telegram callback_data 64바이트 한도 (발송 유실 회귀)
# ──────────────────────────────────────────────
#
# 버튼은 `like|{item_url}`을 실어 보냈는데 Telegram의 callback_data 한도는
# 64바이트다. 실측 153건 중 39건(25.5%)이 이를 넘겼다. 넘기면 Telegram이
# BUTTON_DATA_INVALID를 반환하고 _send_message가 False가 되는데, 키보드는
# 아이템 메시지에 붙어 있으므로 **그 아이템 메시지 자체가 발송되지 않았다**.
# 긴 URL을 가진 아이템은 사용자에게 도달조차 못 했고, 피드백도 받을 수 없었다.

from app.contract import item_id as _item_id  # noqa: E402
from app.deliverers.telegram import _item_keyboard  # noqa: E402

TELEGRAM_CALLBACK_DATA_LIMIT = 64


def _callback_values(keyboard: dict) -> list[str]:
    return [button["callback_data"] for button in keyboard["inline_keyboard"][0]]


def test_callback_data_within_telegram_limit_for_long_url():
    """실측에서 한도를 넘겼던 길이의 URL로도 64바이트를 지켜야 한다."""
    long_url = "https://artificialanalysis.ai/models/" + "segment/" * 30

    for value in _callback_values(_item_keyboard(long_url)):
        assert len(value.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_LIMIT


def test_callback_data_carries_contract_item_id():
    """버튼이 가리키는 아이템과 공개 계약 JSON의 id가 같은 좌표계여야 한다."""
    url = "https://youtu.be/VIDEO"

    like, dislike = _callback_values(_item_keyboard(url))

    assert like == f"like|{_item_id(url)}"
    assert dislike == f"dislike|{_item_id(url)}"


def test_polling_resolves_item_id_back_to_url(monkeypatch, tmp_path):
    """콜백으로 온 id는 프로필에 URL로 쌓여야 나중에 정본과 대조된다."""
    import asyncio

    from app.deliverers import polling

    url = "https://youtu.be/VIDEO"
    captured: list = []
    monkeypatch.setattr(polling, "process_feedback", lambda p: captured.append(p))
    monkeypatch.setattr(polling, "resolve_feedback_target", lambda token: url)

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(polling, "_answer_callback", _noop)

    summary = {"likes": 0, "dislikes": 0, "keywords": []}
    update = {
        "update_id": 1,
        "callback_query": {"id": "cq1", "data": f"like|{_item_id(url)}"},
    }
    asyncio.run(polling._handle_update(None, "token", update, summary))

    assert summary["likes"] == 1
    assert captured[0].item_url == url
