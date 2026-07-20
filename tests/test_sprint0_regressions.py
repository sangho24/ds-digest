"""
Sprint 0 회귀 테스트

98일/247아이템 전수 분석에서 드러난 결함들이 다시 들어오지 않도록 고정한다.
각 테스트는 "무엇이 어떻게 잘못됐었는지"를 docstring에 남긴다.

실행: pytest tests/test_sprint0_regressions.py -v
"""
import pytest

from app.models import FeedbackPayload, QuizItem
from app.config import Settings


# ──────────────────────────────────────────────
# 1. /keyword 처리 시 ValidationError 회귀
# ──────────────────────────────────────────────

def test_keyword_request_payload_without_item_url():
    """
    `/keyword X` 는 특정 아이템에 귀속되지 않으므로 item_url이 없다.

    회귀 내용: item_url이 필수 필드라 polling.py가 이를 생략하고 생성하면
    ValidationError가 발생했고, poll_once()의 광역 except에 삼켜져
    - 키워드가 저장되지 않고
    - 같은 배치의 뒤따르는 like/dislike 처리까지 중단되며
    - acknowledge(offset 갱신)도 건너뛰었다.
    """
    payload = FeedbackPayload(action="keyword_request", keyword="ray")

    assert payload.item_url == ""
    assert payload.keyword == "ray"
    assert payload.user_id == "default"


def test_like_payload_still_requires_meaningful_url():
    """item_url 기본값 도입이 like/dislike 경로를 망가뜨리지 않는지."""
    payload = FeedbackPayload(item_url="https://example.com/a", action="like")
    assert payload.item_url == "https://example.com/a"


# ──────────────────────────────────────────────
# 2. 퀴즈 선지 이중 라벨 회귀
# ──────────────────────────────────────────────

def _quiz(options: list[str]) -> QuizItem:
    return QuizItem(question="Q", options=options, answer_index=0, explanation="E")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("A. 데이터 품질 개선", "데이터 품질 개선"),
        ("C) 조직 내 ML 역량 향상", "조직 내 ML 역량 향상"),
        ("(B) Kubernetes 오토스케일링", "Kubernetes 오토스케일링"),
        ("1. 이중 쓰기 구현", "이중 쓰기 구현"),
        ("가. 카나리 배포", "카나리 배포"),
    ],
)
def test_option_label_is_stripped(raw, expected):
    """
    렌더러가 "A) "를 직접 붙이는데 LLM 출력에도 "A. "가 들어 있어
    "A) A. 데이터 품질 개선" 같은 이중 라벨이 247건 전부에서 발생했다.
    프롬프트 지시로는 막히지 않아 파싱 시점에 제거한다.
    """
    quiz = _quiz([raw, "다른 선지", "또 다른 선지"])
    assert quiz.options[0] == expected


@pytest.mark.parametrize(
    "raw",
    [
        "네. 맞습니다",              # 한글 열거기호가 아닌 일반 어절
        "데이터 파이프라인 구축",      # 라벨 없음
        "A/B 테스트를 수행한다",       # 단일 문자로 시작하나 라벨 아님
        "3.5 버전으로 업그레이드",     # 숫자 뒤 공백 없음
        "gRPC. 스트리밍 지원",        # 여러 글자 토큰
    ],
)
def test_non_label_prefix_is_preserved(raw):
    """정상 선지의 앞부분이 잘려나가지 않아야 한다."""
    quiz = _quiz([raw, "다른 선지", "또 다른 선지"])
    assert quiz.options[0] == raw


def test_all_options_are_stripped_not_just_first():
    quiz = _quiz(["A. 하나", "B) 둘", "(C) 셋", "D. 넷"])
    assert quiz.options == ["하나", "둘", "셋", "넷"]


# ──────────────────────────────────────────────
# 3. seen_urls 30일 만료로 인한 재유입 회귀
# ──────────────────────────────────────────────

def test_seen_url_ttl_is_long_enough_to_prevent_recycling():
    """
    seen_url_ttl_days가 30이었을 때, 31일째 되는 날 만료된 URL이
    재수집되어 전체 아이템의 19.8%가 중복 발송되었다.
    (재등장 간격 49건이 전부 30~37일, 30일 미만 0건)

    소스 풀이 작을수록 심해지므로 최소 180일 이상을 유지한다.
    """
    assert Settings().seen_url_ttl_days >= 180
