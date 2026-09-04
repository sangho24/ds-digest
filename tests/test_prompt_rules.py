"""근거 기반 출력 프롬프트(_EVIDENCE_OUTPUT_PROMPT)의 생성 규칙 회귀 테스트.

문자열 전체가 아니라 규칙의 핵심 키워드만 단언한다. 문구를 다듬어도 규칙이
살아 있으면 통과하고, 규칙 자체가 사라지거나 무력화되면 깨지도록 하는 것이 목적이다.
"""

import pytest

from app.analyzer import _EVIDENCE_OUTPUT_PROMPT, _GroqPacer
from app.models import EVIDENCE_DEPTH_CAPS, EvidenceLevel

# 규칙을 "지켜도 되고 안 지켜도 되는 것"으로 만드는 표현. 하나라도 절 안에
# 들어오면 위의 금지 규칙이 통째로 무력해지므로 존재 자체를 막는다.
_WEAKENING_EXPRESSIONS = (
    "지키지 않아도",
    "참고사항",
    "선택적",
    "선택 사항",
    "가능하면",
    "권장",
    "되도록",
    "무시해도",
    "필수는 아닙니다",
)


def _render() -> str:
    """실제 사용 경로와 같은 방식으로 프롬프트를 포맷한다."""
    return _EVIDENCE_OUTPUT_PROMPT.format(
        evidence_level=EvidenceLevel.FULL.value,
        depth_cap=EVIDENCE_DEPTH_CAPS[EvidenceLevel.FULL],
    )


def _sections() -> tuple[str, str]:
    """렌더된 프롬프트를 (아이디어 절, 퀴즈 절)로 정확히 잘라 낸다.

    퀴즈 절의 끝을 다음 헤딩("## 표기 규칙")으로 못박는다. 끝까지 잡으면
    표기 규칙·JSON 예시가 퀴즈 절에 섞여 단언이 헐거워진다.
    """
    rendered = _render()
    ideas_start = rendered.index("8. production_ideas")
    quiz_start = rendered.index("9. quiz")
    quiz_end = rendered.index("## 표기 규칙", quiz_start)
    return rendered[ideas_start:quiz_start], rendered[quiz_start:quiz_end]


def test_rendering_does_not_raise():
    """중괄호 이스케이프가 깨지면 여기서 먼저 잡힌다."""
    rendered = _render()
    assert EvidenceLevel.FULL.value in rendered
    assert '"production_ideas"' in rendered
    assert '"quiz"' in rendered


def test_sections_are_bounded():
    """절 경계가 실제로 잘려 서로를 침범하지 않는다."""
    ideas, quiz = _sections()
    assert "9. quiz" not in ideas
    assert "8. production_ideas" not in quiz
    assert "## 표기 규칙" not in quiz
    assert '"answer_index"' not in quiz


def test_prompt_stays_within_token_budget():
    """Groq 무료 8K TPM 상한 때문에 이 프롬프트의 비대화는 곧 발송 지연이다.

    옛 버전이 약 538토큰이었고 증분 상한을 400토큰으로 잡았다(= 938).
    """
    assert _GroqPacer.raw_prompt_tokens(_EVIDENCE_OUTPUT_PROMPT) <= 938


# ── (a) 퀴즈: 수치·고유명사 되묻기 금지 ──────────────────────────


def test_quiz_section_bans_recall_questions():
    _, quiz = _sections()
    assert "수치" in quiz and "고유명사" in quiz
    assert "암기" in quiz
    # 무엇을 답으로 요구하면 안 되는지가 유형으로 열거되어 있어야 한다.
    for token in ("개수", "비율", "지표값", "도구명", "제품명", "버전"):
        assert token in quiz, token


@pytest.mark.parametrize("token", ["몇 개", "98.6%"])
def test_quiz_section_lists_bad_examples(token: str):
    """사실 회상 문항의 대표 유형(개수 암기·수치 대응)이 나쁜 예로 적혀 있다."""
    _, quiz = _sections()
    assert "나쁜 예" in quiz
    assert token in quiz


def test_quiz_section_asks_for_mechanism_questions():
    _, quiz = _sections()
    assert "좋은 예" in quiz
    assert "트레이드오프" in quiz
    assert "왜 그렇게 되는가" in quiz
    assert "실패하는가" in quiz


def test_quiz_exemption_is_symmetric():
    """면제는 수치와 고유명사에 대칭이어야 한다.

    수치만 풀어 주면 모델이 도구명·제품명을 문항에 등장시키는 것 자체를 피해
    만들 수 있는 문항까지 줄어든다.
    """
    _, quiz = _sections()
    sentence = next(line for line in quiz.splitlines() if "괜찮습니다" in line)
    # 면제 문장은 줄바꿈으로 이어질 수 있으므로 해당 불릿 전체를 본다.
    bullet_start = quiz.index(sentence)
    bullet = quiz[bullet_start:]
    assert "수치" in bullet and "고유명사" in bullet
    assert "조건" in bullet
    assert "답으로 되묻는" in bullet


# ── (b) 아이디어: 0~3개, 전이 요구, 재진술 금지 ─────────────────


def test_ideas_section_allows_up_to_three_but_not_as_a_target():
    ideas, _ = _sections()
    assert "0~3개" in ideas
    assert "0~2개" not in ideas
    # 상한이 목표치로 읽히면 얇은 근거에서도 3개가 채워진다.
    assert "상한" in ideas
    assert "목표가 아닙니다" in ideas


def test_ideas_section_requires_transfer_and_bans_restatement():
    ideas, _ = _sections()
    assert "다른 맥락" in ideas
    assert "재진술" in ideas
    assert "다른 단계" in ideas and "다른 도메인" in ideas
    # 무엇을 만들지 + 무엇이 개선되는지가 함께 요구되어야 한다.
    assert "무엇을 만들지" in ideas
    assert "개선되는지" in ideas


def test_ideas_section_anchors_transfer_to_the_source():
    """전이의 출발점을 본문에 묶는 검증 가능한 기준이 있어야 한다.

    "없는 사실을 지어내지 마라"만 남기면 목적지가 본문 밖인 전이에는 바닥이
    없어서, 일반론에 본문 인용만 덧댄 아이디어가 통과한다.
    """
    ideas, _ = _sections()
    assert "출발점" in ideas
    assert "지목할 수 있어야" in ideas
    assert "못 대면" in ideas
    assert "일반론" in ideas


def test_ideas_section_keeps_no_fabrication_rule():
    """창의성의 경계: 전이는 허용, 없는 사실 창작은 금지."""
    ideas, _ = _sections()
    assert "지어내지" in ideas
    assert "창작이 아닙니다" in ideas


# ── (c) 두 필드 모두 "근거 부족 시 빈 리스트" 원칙 유지 ─────────


def test_both_fields_keep_empty_list_fallback():
    ideas, quiz = _sections()
    assert "빈 리스트를 반환하세요" in ideas
    assert "빈 리스트를 반환하세요" in quiz


# ── (d) 규칙 무력화 방지 ────────────────────────────────────────


@pytest.mark.parametrize("phrase", _WEAKENING_EXPRESSIONS)
def test_sections_contain_no_weakening_expressions(phrase: str):
    """금지 규칙을 무르게 만드는 표현이 두 절 어디에도 없어야 한다."""
    ideas, quiz = _sections()
    assert phrase not in ideas, f"아이디어 절: {phrase}"
    assert phrase not in quiz, f"퀴즈 절: {phrase}"
