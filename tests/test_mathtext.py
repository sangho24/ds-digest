"""수식 표기 정리 — Discord·이메일은 LaTeX를 렌더링하지 않는다.

실측 2026-09-02 퀴즈가 이렇게 나갔다:
    Φ_{μ,d}^{(α)}(t)의 정의는 무엇인가?
    ∫_0^{4v(t)}(log(1/μ(B_d(t,r))))^{1/α}dr
사실상 읽을 수 없었다. 근본 해결은 생성 프롬프트지만, 모델이 규칙을 어길 때를
위한 마지막 방어선이 필요하다. 그래서 **확실히 좋아지는 변환만** 한다.
"""

from app.mathtext import to_readable


def test_converts_braced_scripts():
    assert to_readable("x_{1} 과 y^{n}") == "x₁ 과 yⁿ"


def test_strips_inline_math_and_commands():
    # 중괄호 지수가 먼저 바뀌면서 `_0` 뒤에 식이 남지 않게 되므로 첨자까지 간다.
    assert to_readable(r"$\int_0^{10} x^{2} dx$") == "∫₀¹⁰ x² dx"
    assert to_readable(r"\frac{a}{b} \leq \alpha") == "(a)/(b) ≤ α"


def test_keeps_braces_off_when_not_convertible():
    """전부 첨자로 못 바꾸면 중괄호만 없앤다 — 높이가 어긋나면 더 나쁘다."""
    assert to_readable("Φ_{μ,d}^{(α)}(t)") == "Φ_μ,d^(α)(t)"


def test_does_not_break_expressions():
    """지수 뒤에 식이 이어지면 손대지 않는다.

    `^1/α`를 `¹/α`로 바꾸면 지수 표시가 사라져 뜻이 달라진다.
    읽기 좋게 만들려다 틀린 식을 만드는 쪽이 훨씬 나쁘다.
    """
    out = to_readable("∫_0^{4v(t)}(log(1/μ(B_d(t,r))))^{1/α}dr")
    assert "^1/α" in out and "¹/α" not in out


def test_leaves_ordinary_text_alone():
    for text in ["수식 없는 평범한 문장입니다", "B_d(t,r)", "path/to/file_1.py", ""]:
        assert to_readable(text) == text


def test_bare_digit_superscript():
    assert to_readable("10^6 배") == "10⁶ 배"


def test_discord_formatting_applies_normalization():
    """발송 경로에서 실제로 걸리는지 — 모듈만 고치고 안 붙이면 의미가 없다."""
    import app.deliverers.discord as d
    assert d._truncate(r"x^{2} 의 \alpha", 100) == "x² 의 α"


def test_keeps_prices_and_regex_intact():
    """가격 표기와 정규식은 수식이 아니다.

    실측 정본 44일치에서 `$99`, `$500 million and $2 billion` 같은 달러 표기가
    6일치 65곳에 나왔다. `$...$`를 무조건 벗기면 `500 million and 2 billion`이
    되고, `\\d+`의 백슬래시를 떼면 정규식 설명이 틀린 문장이 된다.
    """
    assert to_readable("$500 million and $2 billion") == "$500 million and $2 billion"
    assert to_readable("GPT-5 costs $1.25/M input and $10/M output") == (
        "GPT-5 costs $1.25/M input and $10/M output"
    )
    assert to_readable(r"regex \d+ and \w") == r"regex \d+ and \w"
    # 안쪽이 LaTeX처럼 생겼을 때만 수식으로 본다.
    assert to_readable(r"$x^{2}$ 와 $\alpha$") == "x² 와 α"
