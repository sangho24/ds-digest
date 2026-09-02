"""수식 표기를 읽히는 형태로 정리한다.

왜 필요한가:
    발송 채널(Discord·이메일)은 **LaTeX를 렌더링하지 않는다.** MathJax도 KaTeX도
    없다. 그래서 모델이 낸 `Φ_{μ,d}^{(α)}(t)`나 `∫_0^{4v(t)}(log(1/μ(B_d(t,r))))^{1/α}dr`
    같은 표기가 그대로 화면에 찍힌다 — 실측 2026-09-02 퀴즈가 이랬고, 선지 넷이
    중괄호 위치만 다른 적분식이라 사실상 읽을 수 없었다.

    "LaTeX 문법에 맞게 쓰자"는 방향은 오히려 더 나쁘다. 렌더링이 없으니
    `\\int_0^{4v(t)}`가 화면에 그대로 나오고, 지금보다 더 지저분해진다.

    근본 해결은 생성 단계다(analyzer 프롬프트의 표기 규칙). 이 모듈은 모델이
    규칙을 어겼을 때를 위한 **마지막 방어선**이라, 확실히 좋아지는 변환만 한다.
    애매하면 건드리지 않는다 — 잘못 바꾸면 뜻이 달라진다.
"""

from __future__ import annotations

import re

SUP = str.maketrans("0123456789+-=()aeioruvxyn", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵉⁱᵒʳᵘᵛˣʸⁿ")
SUB = str.maketrans("0123456789+-=()aeioruvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₒᵣᵤᵥₓ")

# 자주 나오는 LaTeX 명령만 다룬다. 목록에 없으면 백슬래시만 떼고 이름을 남긴다 —
# 뜻을 지어내는 것보다 낫다.
COMMANDS = {
    "int": "∫", "sum": "Σ", "prod": "∏", "sqrt": "√", "infty": "∞",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "psi": "ψ", "omega": "ω",
    "Delta": "Δ", "Gamma": "Γ", "Lambda": "Λ", "Sigma": "Σ", "Omega": "Ω",
    "leq": "≤", "geq": "≥", "neq": "≠", "approx": "≈", "sim": "∼",
    "times": "×", "cdot": "·", "pm": "±", "to": "→", "rightarrow": "→",
    "in": "∈", "subset": "⊂", "forall": "∀", "exists": "∃", "partial": "∂",
    "log": "log", "exp": "exp", "min": "min", "max": "max",
}

_SCRIPT = re.compile(r"([_^])\{([^{}]{1,12})\}")
# 중괄호 없는 한 자리 숫자 첨자(x_1, 10^6). 숫자만 다룬다 — `B_d`의 d까지
# 건드리면 식별자를 망가뜨린다.
# 뒤에 식이 이어지면 건드리지 않는다. `^1/α`를 `¹/α`로 바꾸면 지수 표시가
# 사라져 뜻이 달라진다 — 읽기 좋게 만들려다 틀린 식을 만드는 쪽이 훨씬 나쁘다.
_BARE_DIGIT = re.compile(r"([_^])([0-9])(?![0-9A-Za-z{(/\\^_.])")
_FRAC = re.compile(r"\\frac\s*\{([^{}]{1,40})\}\s*\{([^{}]{1,40})\}")
_CMD = re.compile(r"\\([A-Za-z]+)")
_INLINE_MATH = re.compile(r"\$([^$]{1,200})\$")


SUB_SRC = set("0123456789+-=()aeioruvx")
SUP_SRC = set("0123456789+-=()aeioruvxyn")


def _script(kind: str, body: str) -> str:
    """`_{ab}` → 유니코드 첨자. 한 글자라도 매핑이 없으면 중괄호만 없앤다.

    "이미 유니코드니까 됐다"고 보면 안 된다. `^{(α)}`의 α는 위첨자가 없어서
    그대로 남는데, 괄호만 위첨자로 바뀌면 `⁽α⁾`처럼 높이가 어긋난 것이 나온다.
    전부 바꿀 수 있을 때만 바꾼다.
    """
    table, source = (SUB, SUB_SRC) if kind == "_" else (SUP, SUP_SRC)
    if all(c in source or c == " " for c in body):
        return body.translate(table)
    # 매핑이 안 되는 글자가 섞이면 유니코드로 못 만든다. 그래도 중괄호는
    # 빼는 게 낫다 — `Φ_{μ,d}` 보다 `Φ_μ,d` 가 읽힌다.
    return f"{kind}{body}"


def to_readable(text: str) -> str:
    """LaTeX 흔적을 렌더링 없이도 읽히는 표기로 바꾼다."""
    if not text or not any(ch in text for ch in ("\\", "{", "$", "^")):
        return text

    out = _INLINE_MATH.sub(r"\1", text)
    out = _FRAC.sub(r"(\1)/(\2)", out)
    out = _CMD.sub(lambda m: COMMANDS.get(m.group(1), m.group(1)), out)
    # 중첩 첨자가 있을 수 있어 몇 번 돌린다.
    for _ in range(3):
        new = _SCRIPT.sub(lambda m: _script(m.group(1), m.group(2)), out)
        if new == out:
            break
        out = new
    return _BARE_DIGIT.sub(lambda m: _script(m.group(1), m.group(2)), out)
