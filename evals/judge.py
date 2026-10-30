"""LLM 심판(LLM-as-a-judge): 다이제스트 산출물이 본문에 근거하는지 외부 모델에게 묻는다.

왜 필요한가:
    evals/run.py 의 지표는 전부 통계(개수·분포·중복)라 "쓴 내용이 본문에 실제로
    있는가"를 읽는 눈이 없다. 이 모듈은 정본 `data/records/digest_*.json` 의
    아이템을 OpenAI 호환 chat completions 심판에게 보내 예/아니오 판정과 본문 인용을
    받고, 인용이 정말 본문에 있는지는 코드가 검사한다. 결과는 append-only 캐시에
    쌓여 추세 지표가 된다.

범위:
    보고 전용이다. run.py 의 게이트·thresholds 는 건드리지 않는다.

실행 예:
    .venv/bin/python -m evals.judge run --judge deepseek --dry-run
    .venv/bin/python -m evals.judge run --judge qwen --since 2026-10-01 --limit 20
    .venv/bin/python -m evals.judge report --window 14 --json
    .venv/bin/python -m evals.judge disagreements --out data/evals/disagreements.json
    .venv/bin/python -m evals.judge labeling --out data/evals/labeling.html
    .venv/bin/python -m evals.judge import-labels labels.json

사람 표시 흐름:
    disagreements → labeling(HTML 한 장, 브라우저에서 표시) → "결과 JSON 복사" →
    import-labels(data/evals/labels.jsonl 에 append) → report 에 심판별 사람 일치율.

환경변수:
    CLI 진입 시 프로젝트 루트의 .env 를 읽되, 이미 설정된 변수는 덮지 않는다.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, TextIO

from dotenv import load_dotenv
import httpx

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "data" / "records"
DEFAULT_CACHE_PATH = ROOT / "data" / "evals" / "judgments.jsonl"
DEFAULT_LABELS_PATH = ROOT / "data" / "evals" / "labels.jsonl"

# 사람 표시 값. 페이지의 라디오 값이자 labels.jsonl 의 human 필드다.
HUMAN_CHOICES = ("supported", "unsupported", "unsure")
HUMAN_LABELS_KO = {"supported": "근거 있음", "unsupported": "근거 없음", "unsure": "판단 보류"}


def load_project_env(env_path: Path | str | None = None) -> bool:
    """프로젝트 루트 .env 를 읽어 **아직 없는** 환경변수만 채운다(override=False).

    app/config.py 가 pydantic_settings 로 읽는 것과 같은 파일이다. CLI 진입(main)
    시점에만 부른다. import 시점에 부르면 테스트가 개발자의 .env 키를 물려받는다.
    키 값은 어디에도 출력하지 않는다. 파일이 없으면 False.
    """
    path = Path(env_path) if env_path is not None else ROOT / ".env"
    if not path.is_file():
        return False
    return bool(load_dotenv(path, override=False))

JUDGE_MAX_CHARS = int(os.environ.get("JUDGE_MAX_CHARS", "16000"))
MAX_WORKERS = 4
REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.0
JUDGEABLE_EVIDENCE_LEVEL = "full"

QUESTION_KINDS = ("summary", "key_points", "production_ideas", "quiz")

# 심판 제공자. model 은 환경변수(model_env)로 덮어쓸 수 있다. extra_body 는 요청 본문에
# 그대로 합쳐지는 제공자 전용 파라미터다.
#
# DeepSeek Flash 는 thinking 이 기본으로 켜진다(effort high). 2026-10-30 시험 10건에서 출력
# 36,004 토큰 중 30,601 이 reasoning 토큰이라 출력 비용($0.60/M)이 입력($0.15/M)을 넘었다.
# 심판은 본문 인용을 코드가 검증하므로 긴 사고 과정의 이득이 작고, 소급 395건을 잔액 안에서
# 끝내려면 꺼야 한다. 켜서 비교하고 싶으면 JUDGE_DEEPSEEK_THINKING=1 로 돌린다. 캐시 키에는
# 안 들어가므로 같은 아이템을 두 모드로 받으려면 행을 지우고 다시 돌려야 한다. usage 의
# reasoning_tokens 로 어느 모드였는지 사후에 구분할 수 있다.
JUDGES: dict[str, dict[str, Any]] = {
    "deepseek": {
        "id": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "model_env": "JUDGE_DEEPSEEK_MODEL",
        "api_key_env": "DEEPSEEK_API_KEY",
        "extra_body": {"thinking": {"type": "disabled"}},
        "extra_body_off_env": "JUDGE_DEEPSEEK_THINKING",
    },
    "qwen": {
        "id": "qwen",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-flash",
        "model_env": "JUDGE_QWEN_MODEL",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
}

# ──────────────────────────────────────────────
# 프롬프트. 바꾸면 prompt_hash 가 바뀌어 캐시가 전부 미스가 된다(의도된 동작).
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 기술 콘텐츠 요약물의 근거를 검사하는 심판이다. 주어진 본문(BODY)만을 사실의 출처로 삼는다.

규칙:
1. 판정은 true/false 로만 한다. 점수나 등급을 매기지 않는다.
2. 모든 판정에 근거가 되는 본문 구절을 quote 로 붙인다. 인용은 본문에서 글자 그대로 복사하라. 요약하거나 고쳐 쓰거나 번역하지 않는다. 본문에 해당 구절이 없으면 quote 는 null 로 둔다.
3. 본문 바깥의 지식으로 판단하지 않는다. 본문에 없으면 근거 없음이다.
4. 응답은 JSON 객체 하나만 낸다. 설명 문장이나 코드펜스를 붙이지 않는다.
"""

USER_PROMPT_TEMPLATE = """[BODY]
<<BODY>>

[SUMMARY]
<<SUMMARY>>

[KEY_POINTS]
<<KEY_POINTS>>

[PRODUCTION_IDEAS]
<<PRODUCTION_IDEAS>>

[QUIZ]
<<QUIZ>>

질문:
- summary: SUMMARY 에 BODY 에 없는 주장이 있는가. 없으면 faithful=true 와 SUMMARY 를 뒷받침하는 BODY 구절을 quote 로 적는다. 있으면 faithful=false 와 그 주장을 unsupported_claim 에 적는다.
- key_points: 각 포인트가 BODY 에서 뒷받침되는가(supported). 뒷받침하는 BODY 구절을 quote 로 적는다.
- production_ideas: 각 아이디어가 BODY 의 어떤 기법·구조·조건을 출발점으로 삼는가. 그 출발점을 BODY 에서 찾아 quote 로 복사한다. 지목하지 못하면 grounded=false, quote=null.
- quiz: 각 퀴즈를 BODY 만 읽고 답할 수 있는가(answerable). 제시된 정답이 BODY 와 맞는가(answer_correct). 근거 BODY 구절을 quote 로 적는다.

응답 형식(JSON 객체 하나, 값은 예시일 뿐이다):
{"summary": {"faithful": false, "unsupported_claim": "본문에 없는 주장", "quote": null},
 "key_points": [{"index": 0, "supported": true, "quote": "본문에서 복사한 구절"},
                {"index": 1, "supported": false, "quote": null}],
 "production_ideas": [{"index": 0, "grounded": false, "quote": null},
                      {"index": 1, "grounded": true, "quote": "본문에서 복사한 구절"}],
 "quiz": [{"index": 0, "answerable": true, "answer_correct": false, "quote": "본문에서 복사한 구절"}]}
index 는 위 목록의 번호와 같게, 항목 수는 목록의 개수와 같게 낸다. 목록이 비어 있으면 빈 배열을 낸다.
"""


def prompt_hash() -> str:
    """시스템·사용자 프롬프트를 합친 sha256 앞 12자."""
    digest = hashlib.sha256(
        (SYSTEM_PROMPT + "\n" + USER_PROMPT_TEMPLATE).encode("utf-8")
    ).hexdigest()
    return digest[:12]


PROMPT_HASH = prompt_hash()


# ──────────────────────────────────────────────
# 텍스트 유틸
# ──────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*(.*?)```", re.DOTALL)


def normalize_ws(text: Any) -> str:
    """연속 공백을 하나로 줄이고 양끝을 자른다. 인용 대조는 전부 이 형태로 한다."""
    if text is None:
        return ""
    return _WS_RE.sub(" ", str(text)).strip()


def verify_quote(quote: Any, body: str) -> bool:
    """심판이 낸 인용이 본문에 부분 문자열로 존재하는가. 공백 차이만 허용한다."""
    normalized = normalize_ws(quote)
    if not normalized:
        return False
    return normalized in normalize_ws(body)


def analysis_hash(analysis: dict[str, Any]) -> str:
    """analysis dict 의 정렬된 JSON sha256 앞 12자. 키 순서에는 둔감, 값 변화에는 민감."""
    canonical = json.dumps(
        analysis, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


_JSON_DECODER = json.JSONDecoder()


def _loads_object(candidate: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_json_response(text: str) -> dict[str, Any]:
    """모델 응답에서 JSON 객체를 꺼낸다.

    순서가 중요하다. 인용문 안에 ``` 가 들어간 순수 JSON 을 펜스 정규식이 먼저 먹으면
    깨지므로 (a) 원문 그대로, (b) 코드펜스를 벗겨서, (c) 원문의 각 '{' 위치에서
    raw_decode 로 객체 하나를 읽어 보는 순서로 시도한다. (c) 는 앞 설명문에 중괄호가
    섞여 있어도("결과 {참고}: {...}") 실제 객체를 찾는다.
    """
    candidate = (text or "").strip()
    parsed = _loads_object(candidate)
    if parsed is not None:
        return parsed

    fence = _FENCE_RE.search(candidate)
    if fence:
        parsed = _loads_object(fence.group(1).strip())
        if parsed is not None:
            return parsed

    position = candidate.find("{")
    while position >= 0:
        try:
            value, _ = _JSON_DECODER.raw_decode(candidate, position)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
        position = candidate.find("{", position + 1)

    raise ValueError("심판 응답에서 JSON 객체를 찾지 못했다")


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ──────────────────────────────────────────────
# 심판 대상 만들기
# ──────────────────────────────────────────────


def build_body_text(raw: dict[str, Any], max_chars: int | None = None) -> tuple[str, bool]:
    """body + transcript 를 이어 붙이고 상한을 넘으면 앞부분만 쓴다. (본문, truncated)

    max_chars 가 None 이면 호출 시점의 JUDGE_MAX_CHARS 를 쓴다(테스트에서 패치 가능).
    """
    if max_chars is None:
        max_chars = JUDGE_MAX_CHARS
    parts = [str(raw.get("body") or "").strip(), str(raw.get("transcript") or "").strip()]
    text = "\n\n".join(part for part in parts if part)
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def build_target(
    record_date: str, entry: dict[str, Any], max_chars: int | None = None
) -> dict[str, Any]:
    """정본 아이템 하나를 심판 대상 dict 로 바꾼다. 건너뛸 사유가 있으면 skip_reason 에 둔다."""
    raw = entry.get("raw") or {}
    analysis = entry.get("analysis") or {}
    body, truncated = build_body_text(raw, max_chars=max_chars)
    evidence_level = analysis.get("evidence_level")

    skip_reason: str | None = None
    if not raw.get("url"):
        skip_reason = "no_url"
    elif evidence_level != JUDGEABLE_EVIDENCE_LEVEL:
        skip_reason = f"evidence_level={evidence_level}"
    elif not body:
        skip_reason = "empty_body"

    return {
        "date": record_date,
        "url": raw.get("url"),
        "title": raw.get("title"),
        "evidence_level": evidence_level,
        "analysis": analysis,
        "analysis_hash": analysis_hash(analysis),
        "body": body,
        "truncated": truncated,
        "skip_reason": skip_reason,
    }


def iter_record_files(
    records_dir: Path, since: date | None = None, until: date | None = None
) -> list[tuple[date, Path]]:
    files: list[tuple[date, Path]] = []
    if not records_dir.exists():
        return files
    for path in sorted(records_dir.glob("digest_*.json")):
        record_date = _parse_date(path.stem.replace("digest_", ""))
        if record_date is None:
            continue
        if since and record_date < since:
            continue
        if until and record_date > until:
            continue
        files.append((record_date, path))
    return files


def collect_targets(
    records_dir: Path | None = None,
    since: date | None = None,
    until: date | None = None,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """기간 안의 정본 파일을 읽어 심판 대상 목록을 만든다."""
    targets: list[dict[str, Any]] = []
    for record_date, path in iter_record_files(records_dir or RECORDS_DIR, since, until):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in record.get("items") or []:
            targets.append(build_target(record_date.isoformat(), entry, max_chars=max_chars))
    return targets


# ──────────────────────────────────────────────
# 프롬프트 조립과 판정 정규화
# ──────────────────────────────────────────────


def _format_quiz(quiz: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(quiz):
        item = item or {}
        options = item.get("options") or []
        answer_index = item.get("answer_index")
        answer = None
        if isinstance(answer_index, int) and 0 <= answer_index < len(options):
            answer = options[answer_index]
        option_text = " / ".join(f"({i}) {opt}" for i, opt in enumerate(options))
        lines.append(
            f"{index}. 질문: {item.get('question')}\n"
            f"   선지: {option_text}\n"
            f"   제시된 정답: ({answer_index}) {answer}"
        )
    return "\n".join(lines) if lines else "(없음)"


def _format_list(values: list[Any]) -> str:
    if not values:
        return "(없음)"
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values))


def key_point_texts(analysis: dict[str, Any]) -> list[str]:
    return [str((kp or {}).get("point") or "") for kp in analysis.get("key_points") or []]


_PLACEHOLDER_RE = re.compile(r"<<(BODY|SUMMARY|KEY_POINTS|PRODUCTION_IDEAS|QUIZ)>>")


def build_user_prompt(target: dict[str, Any]) -> str:
    """템플릿의 플레이스홀더를 한 번에 치환한다.

    순차 replace 를 쓰면 본문 안에 "<<SUMMARY>>" 같은 글자가 있을 때 다음 단계가 그것까지
    바꿔 버린다. 정규식 한 패스로 템플릿에 있던 자리만 채운다.
    """
    analysis = target["analysis"]
    values = {
        "BODY": target["body"],
        "SUMMARY": str(analysis.get("one_line_summary") or "(없음)"),
        "KEY_POINTS": _format_list(key_point_texts(analysis)),
        "PRODUCTION_IDEAS": _format_list(list(analysis.get("production_ideas") or [])),
        "QUIZ": _format_quiz(list(analysis.get("quiz") or [])),
    }
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], USER_PROMPT_TEMPLATE)


def as_bool(value: Any) -> bool:
    """심판이 낸 판정 값을 bool 로. bool 은 그대로, 문자열은 true/yes 만 참, 그 외는 전부 거짓."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes")
    return False


def _align(entries: Any, length: int) -> list[dict[str, Any]]:
    """심판이 낸 배열을 대상 목록 길이에 맞춘다. index 가 있으면 그것으로, 없으면 위치로."""
    aligned: list[dict[str, Any]] = [{} for _ in range(length)]
    if not isinstance(entries, list):
        return aligned
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = position
        if 0 <= index < length:
            aligned[index] = entry
    return aligned


def _quote_of(entry: dict[str, Any]) -> str | None:
    quote = entry.get("quote")
    if quote is None:
        return None
    quote = str(quote).strip()
    return quote or None


def build_verdicts(parsed: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """심판 응답을 캐시 행의 verdicts 로 정규화한다.

    각 판정에 심판의 주장(bool)과 quote_verified, 그리고 둘을 합친 verified 를 둔다.
    집계는 verified 만 센다.
    """
    body = target["body"]
    analysis = target["analysis"]

    summary_raw = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else {}
    summary_quote = _quote_of(summary_raw)
    summary_verified = verify_quote(summary_quote, body)
    faithful = as_bool(summary_raw.get("faithful"))
    unsupported = summary_raw.get("unsupported_claim")
    summary = {
        "text": analysis.get("one_line_summary"),
        "faithful": faithful,
        "unsupported_claim": str(unsupported) if unsupported else None,
        "quote": summary_quote,
        "quote_verified": summary_verified,
        "verified": faithful and summary_verified,
    }

    points = key_point_texts(analysis)
    key_points = []
    for index, entry in enumerate(_align(parsed.get("key_points"), len(points))):
        quote = _quote_of(entry)
        quote_verified = verify_quote(quote, body)
        supported = as_bool(entry.get("supported"))
        key_points.append(
            {
                "index": index,
                "text": points[index],
                "supported": supported,
                "quote": quote,
                "quote_verified": quote_verified,
                "verified": supported and quote_verified,
            }
        )

    ideas = list(analysis.get("production_ideas") or [])
    production_ideas = []
    for index, entry in enumerate(_align(parsed.get("production_ideas"), len(ideas))):
        quote = _quote_of(entry)
        quote_verified = verify_quote(quote, body)
        grounded = as_bool(entry.get("grounded"))
        production_ideas.append(
            {
                "index": index,
                "text": ideas[index],
                "grounded": grounded,
                "quote": quote,
                "quote_verified": quote_verified,
                "verified": grounded and quote_verified,
            }
        )

    quiz_items = list(analysis.get("quiz") or [])
    quiz = []
    for index, entry in enumerate(_align(parsed.get("quiz"), len(quiz_items))):
        quote = _quote_of(entry)
        quote_verified = verify_quote(quote, body)
        answerable = as_bool(entry.get("answerable"))
        answer_correct = as_bool(entry.get("answer_correct"))
        quiz.append(
            {
                "index": index,
                "text": (quiz_items[index] or {}).get("question"),
                "answerable": answerable,
                "answer_correct": answer_correct,
                "quote": quote,
                "quote_verified": quote_verified,
                "verified_answerable": answerable and quote_verified,
                "verified_answer_correct": answer_correct and quote_verified,
            }
        )

    return {
        "summary": summary,
        "key_points": key_points,
        "production_ideas": production_ideas,
        "quiz": quiz,
    }


# ──────────────────────────────────────────────
# 전송 (httpx) 과 재시도
# ──────────────────────────────────────────────

Transport = Callable[[dict[str, Any]], dict[str, Any]]


class JudgeError(Exception):
    """재시도로 회복할 수 없는 심판 호출 실패. 캐시에 error 로 남긴다."""


class JudgeAuthError(JudgeError):
    """401/403. 키 문제라 다음 아이템도 전부 실패하므로 run 전체를 중단한다."""


AUTH_STATUSES = (401, 403)
# 제공자가 response_format 을 거부할 때 내는 상태. 이때만 그 필드를 빼고 1회 재시도한다.
RESPONSE_FORMAT_REJECT_STATUSES = (400, 422)


def httpx_transport(request: dict[str, Any]) -> dict[str, Any]:
    """기본 전송. request: {url, headers, json, timeout} → {status_code, json, text}."""
    response = httpx.post(
        request["url"],
        headers=request["headers"],
        json=request["json"],
        timeout=request.get("timeout", REQUEST_TIMEOUT),
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return {"status_code": response.status_code, "json": payload, "text": response.text}


def resolve_model(judge: dict[str, str]) -> str:
    return os.environ.get(judge["model_env"]) or judge["model"]


def call_judge(
    judge: dict[str, str],
    user_prompt: str,
    *,
    api_key: str,
    transport: Transport,
    sleep: Callable[[float], None] = time.sleep,
    model: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """아이템 하나를 심판에게 보낸다. (파싱된 JSON, usage) 를 돌려준다.

    429/5xx/전송 예외는 지수 백오프로 MAX_RETRIES 번 재시도한다. 400/422 는
    response_format 을 뺀 1회 재시도만 하고(제공자가 json_object 를 거부하는 경우),
    401/403 은 JudgeAuthError, 나머지 4xx 는 재시도 없이 JudgeError 를 낸다.
    """
    model = model or resolve_model(judge)
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    # 제공자 전용 파라미터. extra_body_off_env 가 "1" 이면 붙이지 않는다(기본 동작으로 되돌림).
    off_env = judge.get("extra_body_off_env")
    if judge.get("extra_body") and not (off_env and os.environ.get(off_env) == "1"):
        body.update(judge["extra_body"])
    request = {
        "url": judge["base_url"].rstrip("/") + "/chat/completions",
        "headers": {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        "json": body,
        "timeout": REQUEST_TIMEOUT,
    }

    attempt = 0
    dropped_response_format = False
    while True:
        try:
            response = transport(request)
        except (httpx.HTTPError, OSError) as exc:
            if attempt >= MAX_RETRIES:
                raise JudgeError(f"transport: {exc}") from exc
            sleep(BACKOFF_BASE_SECONDS * (2**attempt))
            attempt += 1
            continue

        status = int(response.get("status_code", 0))
        if status == 429 or status >= 500:
            if attempt >= MAX_RETRIES:
                raise JudgeError(f"HTTP {status}: {_short(response.get('text'))}")
            sleep(BACKOFF_BASE_SECONDS * (2**attempt))
            attempt += 1
            continue
        if status in AUTH_STATUSES:
            raise JudgeAuthError(f"HTTP {status}: {_short(response.get('text'))}")
        if 400 <= status < 500:
            if (
                status in RESPONSE_FORMAT_REJECT_STATUSES
                and "response_format" in request["json"]
                and not dropped_response_format
            ):
                request["json"] = {k: v for k, v in body.items() if k != "response_format"}
                dropped_response_format = True
                continue
            raise JudgeError(f"HTTP {status}: {_short(response.get('text'))}")
        if status < 200 or status >= 300:
            raise JudgeError(f"HTTP {status}: {_short(response.get('text'))}")

        payload = response.get("json")
        if not isinstance(payload, dict):
            raise JudgeError("응답 본문이 JSON 이 아니다")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise JudgeError(f"choices 구조가 다르다: {exc}") from exc
        try:
            parsed = parse_json_response(str(content))
        except (ValueError, json.JSONDecodeError) as exc:
            raise JudgeError(f"심판 JSON 파싱 실패: {exc}") from exc
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        return parsed, usage


def _short(text: Any, limit: int = 200) -> str:
    return normalize_ws(text)[:limit]


# ──────────────────────────────────────────────
# 캐시
# ──────────────────────────────────────────────

CacheKey = tuple[str, str, str, str]


def resolve_cache_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("JUDGE_CACHE_PATH")
    return Path(env) if env else DEFAULT_CACHE_PATH


def cache_key(row: dict[str, Any]) -> CacheKey:
    return (
        str(row.get("judge_id")),
        str(row.get("url")),
        str(row.get("analysis_hash")),
        str(row.get("prompt_hash")),
    )


def load_cache(path: Path | str | None = None) -> list[dict[str, Any]]:
    cache_path = resolve_cache_path(path)
    if not cache_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with cache_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def append_cache(row: dict[str, Any], path: Path | str | None = None) -> None:
    """행 하나를 덧붙인다. 파일 꼬리가 개행으로 끝나지 않으면 먼저 개행을 넣어 행이 붙지 않게 한다."""
    cache_path = resolve_cache_path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if cache_path.exists() and cache_path.stat().st_size > 0:
        with cache_path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) != b"\n"
    with cache_path.open("a", encoding="utf-8") as handle:
        if needs_newline:
            handle.write("\n")
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def current_item_keys(records_dir: Path | None = None) -> set[tuple[str, str]]:
    """정본에 현재 있는 (url, analysis_hash) 쌍. 같은 url 이 다른 analysis 로 두 번 있을 수 있어 쌍이 키다."""
    return {
        (str(target["url"]), target["analysis_hash"])
        for target in collect_targets(records_dir)
        if target.get("url")
    }


def filter_current_rows(
    rows: list[dict[str, Any]],
    *,
    prompt_hash: str | None = PROMPT_HASH,
    current_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """보고에 쓸 행만 남긴다.

    prompt_hash 가 주어지면 그 프롬프트로 판정한 행만, current_keys 가 주어지면 정본에
    아직 있는 (url, analysis_hash) 행만 남긴다. 대체된 옛 행이 이중 집계되는 것을 막는다.
    """
    kept: list[dict[str, Any]] = []
    dropped = {"other_prompt": 0, "stale_analysis": 0, "kept": 0}
    for row in rows:
        if prompt_hash is not None and row.get("prompt_hash") != prompt_hash:
            dropped["other_prompt"] += 1
            continue
        if current_keys is not None and (str(row.get("url")), str(row.get("analysis_hash"))) not in current_keys:
            dropped["stale_analysis"] += 1
            continue
        kept.append(row)
    dropped["kept"] = len(kept)
    return kept, dropped


def latest_rows(rows: list[dict[str, Any]]) -> dict[CacheKey, dict[str, Any]]:
    """같은 키의 행이 여러 개면 마지막(최신) 행만 남긴다. 성공 행이 error 행을 덮는다."""
    latest: dict[CacheKey, dict[str, Any]] = {}
    for row in rows:
        latest[cache_key(row)] = row
    return latest


def is_fresh(row: dict[str, Any] | None) -> bool:
    """다시 호출하지 않아도 되는 행인가. error 가 있으면 재시도 대상이다."""
    return row is not None and not row.get("error")


def make_row(
    judge_id: str,
    model: str | None,
    target: dict[str, Any],
    *,
    skipped: bool = False,
    reason: str | None = None,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    verdicts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "judge_id": judge_id,
        "model": model,
        "prompt_hash": PROMPT_HASH,
        "date": target["date"],
        "url": target["url"],
        "title": target.get("title"),
        "analysis_hash": target["analysis_hash"],
        "judged_at": _now_iso(),
        "skipped": skipped,
        "reason": reason,
        "error": error,
        "truncated": bool(target.get("truncated")),
        "usage": usage,
        "verdicts": verdicts,
    }


# ──────────────────────────────────────────────
# run
# ──────────────────────────────────────────────


def _estimate_tokens(target: dict[str, Any]) -> int:
    """본문 글자수 / 2 근사. 한국어·영어 혼합에서 대략 맞는다."""
    return len(target["body"]) // 2


def run_judge(
    judge_id: str,
    *,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    transport: Transport | None = None,
    cache_path: Path | str | None = None,
    records_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_workers: int = MAX_WORKERS,
    api_key: str | None = None,
    out: TextIO | None = None,
) -> dict[str, Any]:
    """대상 아이템을 골라 캐시에 없는 것만 심판에게 보낸다. 요약 dict 를 돌려준다."""
    out = out or sys.stdout
    if judge_id not in JUDGES:
        raise ValueError(f"알 수 없는 심판: {judge_id} (가능: {', '.join(JUDGES)})")
    judge = JUDGES[judge_id]
    model = resolve_model(judge)

    targets = collect_targets(records_dir, since, until)
    cached = latest_rows(load_cache(cache_path))

    pending_skip: list[dict[str, Any]] = []
    pending_call: list[dict[str, Any]] = []
    cache_hits = 0
    for target in targets:
        key = (judge_id, str(target["url"]), target["analysis_hash"], PROMPT_HASH)
        if is_fresh(cached.get(key)):
            cache_hits += 1
            continue
        if target["skip_reason"]:
            pending_skip.append(target)
        else:
            pending_call.append(target)

    if limit is not None:
        pending_call = pending_call[: max(limit, 0)]

    full_count = sum(1 for t in targets if t["evidence_level"] == JUDGEABLE_EVIDENCE_LEVEL)
    estimated_tokens = sum(_estimate_tokens(t) for t in pending_call)
    summary: dict[str, Any] = {
        "judge_id": judge_id,
        "model": model,
        "prompt_hash": PROMPT_HASH,
        "targets": len(targets),
        "full": full_count,
        "full_ratio": (full_count / len(targets)) if targets else None,
        "cache_hits": cache_hits,
        "to_call": len(pending_call),
        "to_skip": len(pending_skip),
        "estimated_tokens": estimated_tokens,
        "dry_run": dry_run,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "usage_total": 0,
        "aborted": None,
    }

    full_ratio_text = f"{summary['full_ratio']:.1%}" if summary["full_ratio"] is not None else "n/a"
    print(
        f"[judge:{judge_id}] model={model} prompt={PROMPT_HASH} 대상 {len(targets)}건"
        f" (full {full_count}건, {full_ratio_text}) 캐시 적중 {cache_hits}건"
        f" 호출 예정 {len(pending_call)}건 건너뜀 예정 {len(pending_skip)}건"
        f" 예상 입력 토큰 약 {estimated_tokens:,}",
        file=out,
    )
    if dry_run:
        return summary

    if transport is None:
        key_value = api_key or os.environ.get(judge["api_key_env"])
        if not key_value:
            raise SystemExit(f"{judge['api_key_env']} 가 없다. 심판 {judge_id} 를 호출할 수 없다.")
        transport = httpx_transport
    else:
        key_value = api_key or os.environ.get(judge["api_key_env"]) or ""

    lock = threading.Lock()
    abort = threading.Event()  # 401/403 이 한 번 나오면 남은 호출을 전부 멈춘다

    for target in pending_skip:
        append_cache(
            make_row(judge_id, model, target, skipped=True, reason=target["skip_reason"]),
            cache_path,
        )
        summary["skipped"] += 1
        print(f"  skip  {target['date']} {target['url']} ({target['skip_reason']})", file=out)

    def _work(target: dict[str, Any]) -> dict[str, Any] | None:
        if abort.is_set():
            return None  # 기록하지 않는다. 다음 run 에서 다시 대상이 된다
        user_prompt = build_user_prompt(target)
        try:
            parsed, usage = call_judge(
                judge, user_prompt, api_key=key_value, transport=transport, sleep=sleep, model=model
            )
        except JudgeAuthError as exc:
            abort.set()
            with lock:
                summary["aborted"] = summary["aborted"] or str(exc)
            return make_row(judge_id, model, target, error=str(exc))
        except JudgeError as exc:
            return make_row(judge_id, model, target, error=str(exc))
        verdicts = build_verdicts(parsed, target)
        return make_row(judge_id, model, target, usage=usage, verdicts=verdicts)

    workers = max(1, min(max_workers, len(pending_call) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_work, target): target for target in pending_call}
        for future in as_completed(futures):
            target = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 - 어떤 예외든 error 행으로 남겨 재시도 대상으로 둔다
                row = make_row(judge_id, model, target, error=f"{type(exc).__name__}: {exc}")
            if row is None:
                continue
            with lock:
                append_cache(row, cache_path)
                if row.get("error"):
                    summary["failed"] += 1
                    print(f"  error {target['date']} {target['url']} {row['error']}", file=out)
                else:
                    summary["succeeded"] += 1
                    total = int((row.get("usage") or {}).get("total_tokens") or 0)
                    summary["usage_total"] += total
                    print(
                        f"  ok    {target['date']} {target['url']} tokens={total}"
                        f"{' truncated' if row.get('truncated') else ''}",
                        file=out,
                    )

    if summary["aborted"]:
        print(f"[judge:{judge_id}] 인증 오류로 중단: {summary['aborted']} (키를 확인하라)", file=out)
    print(
        f"[judge:{judge_id}] 성공 {summary['succeeded']} 실패 {summary['failed']}"
        f" 건너뜀 {summary['skipped']} 사용 토큰 {summary['usage_total']:,}",
        file=out,
    )
    return summary


# ──────────────────────────────────────────────
# report
# ──────────────────────────────────────────────


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _iso_week(value: date) -> str:
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """최신 행 목록(한 심판)에서 지표를 낸다. 근거 있음은 verified 만 센다.

    truncated 아이템은 잘린 본문을 기준으로 검증되어 비율을 깎을 수 있다. 그래서 전체
    비율과 별도로 truncated 를 뺀 비율을 `excl_truncated` 에 함께 낸다.
    """
    judged = [r for r in rows if not r.get("skipped") and not r.get("error") and r.get("verdicts")]
    skipped = sum(1 for r in rows if r.get("skipped"))
    errors = sum(1 for r in rows if r.get("error"))
    truncated = sum(1 for r in judged if r.get("truncated"))

    metrics = _verdict_rates(judged)
    metrics.update(
        {
            "judged_count": len(judged),
            "skipped_count": skipped,
            "error_count": errors,
            "truncated_count": truncated,
            "excl_truncated": {
                "judged_count": len(judged) - truncated,
                **_verdict_rates([r for r in judged if not r.get("truncated")]),
            },
        }
    )
    return metrics


def _verdict_rates(judged: list[dict[str, Any]]) -> dict[str, Any]:
    """성공 행 목록에서 비율 6종과 분모를 낸다."""
    summary_total = summary_ok = 0
    kp_total = kp_ok = 0
    idea_total = idea_ok = 0
    quiz_total = quiz_answerable = quiz_correct = 0
    quotes_total = quotes_ok = 0

    for row in judged:
        verdicts = row["verdicts"]
        summary = verdicts.get("summary") or {}
        if summary:
            summary_total += 1
            summary_ok += 1 if summary.get("verified") else 0
            if summary.get("quote"):
                quotes_total += 1
                quotes_ok += 1 if summary.get("quote_verified") else 0
        for entry in verdicts.get("key_points") or []:
            kp_total += 1
            kp_ok += 1 if entry.get("verified") else 0
            if entry.get("quote"):
                quotes_total += 1
                quotes_ok += 1 if entry.get("quote_verified") else 0
        for entry in verdicts.get("production_ideas") or []:
            idea_total += 1
            idea_ok += 1 if entry.get("verified") else 0
            if entry.get("quote"):
                quotes_total += 1
                quotes_ok += 1 if entry.get("quote_verified") else 0
        for entry in verdicts.get("quiz") or []:
            quiz_total += 1
            quiz_answerable += 1 if entry.get("verified_answerable") else 0
            quiz_correct += 1 if entry.get("verified_answer_correct") else 0
            if entry.get("quote"):
                quotes_total += 1
                quotes_ok += 1 if entry.get("quote_verified") else 0

    return {
        "summary_faithful_rate": _rate(summary_ok, summary_total),
        "key_point_support_rate": _rate(kp_ok, kp_total),
        "grounded_idea_rate": _rate(idea_ok, idea_total),
        "quiz_answerable_rate": _rate(quiz_answerable, quiz_total),
        "quiz_answer_correct_rate": _rate(quiz_correct, quiz_total),
        "quote_verified_rate": _rate(quotes_ok, quotes_total),
        "counts": {
            "summary": summary_total,
            "key_points": kp_total,
            "production_ideas": idea_total,
            "quiz": quiz_total,
            "quotes": quotes_total,
        },
    }


def _claims(verdicts: dict[str, Any]) -> dict[str, list[tuple[int, bool]]]:
    """agreement 비교용: 질문 종류별 (index, 심판 주장 bool) 목록."""
    summary = verdicts.get("summary") or {}
    return {
        "summary": [(0, bool(summary.get("faithful")))] if summary else [],
        "key_points": [(e["index"], bool(e.get("supported"))) for e in verdicts.get("key_points") or []],
        "production_ideas": [
            (e["index"], bool(e.get("grounded"))) for e in verdicts.get("production_ideas") or []
        ],
        "quiz_answerable": [(e["index"], bool(e.get("answerable"))) for e in verdicts.get("quiz") or []],
        "quiz_answer_correct": [
            (e["index"], bool(e.get("answer_correct"))) for e in verdicts.get("quiz") or []
        ],
    }


AGREEMENT_KINDS = ("summary", "key_points", "production_ideas", "quiz_answerable", "quiz_answer_correct")


ItemKey = tuple[str, str, str]  # (url, analysis_hash, prompt_hash)


def _judged_index(rows: list[dict[str, Any]]) -> dict[str, dict[ItemKey, dict[str, Any]]]:
    """judge_id → {(url, analysis_hash, prompt_hash): 성공 행}. 최신 행 기준.

    prompt_hash 를 키에 넣어 다른 프롬프트로 판정한 두 심판을 비교하지 않는다.
    """
    index: dict[str, dict[ItemKey, dict[str, Any]]] = {}
    for row in latest_rows(rows).values():
        if row.get("skipped") or row.get("error") or not row.get("verdicts"):
            continue
        key = (str(row["url"]), str(row["analysis_hash"]), str(row.get("prompt_hash")))
        index.setdefault(str(row["judge_id"]), {})[key] = row
    return index


def compute_agreement(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """두 심판이 모두 판정한 아이템에서 질문 단위 일치율. 심판의 주장(claimed)을 비교한다."""
    index = _judged_index(rows)
    judge_ids = sorted(index)
    if len(judge_ids) < 2:
        return None
    first, second = judge_ids[0], judge_ids[1]
    common = sorted(set(index[first]) & set(index[second]))
    if not common:
        return None  # 같은 프롬프트·같은 analysis 로 둘 다 판정한 아이템이 없다
    compared = {kind: 0 for kind in AGREEMENT_KINDS}
    agreed = {kind: 0 for kind in AGREEMENT_KINDS}
    for key in common:
        left = _claims(index[first][key]["verdicts"])
        right = _claims(index[second][key]["verdicts"])
        for kind in AGREEMENT_KINDS:
            right_map = dict(right[kind])
            for position, value in left[kind]:
                if position not in right_map:
                    continue
                compared[kind] += 1
                agreed[kind] += 1 if right_map[position] == value else 0
    return {
        "pair": [first, second],
        "items_both_judged": len(common),
        "by_question": {
            kind: {"compared": compared[kind], "agreed": agreed[kind], "rate": _rate(agreed[kind], compared[kind])}
            for kind in AGREEMENT_KINDS
        },
        "overall_rate": _rate(sum(agreed.values()), sum(compared.values())),
    }


def question_id(url: Any, analysis_hash: Any, prompt_hash: Any, question: Any, index: Any) -> str:
    """사람 표시 항목의 결정적 id. 같은 (url, analysis, prompt, 질문, 번호)면 언제 만들어도 같다."""
    raw = "|".join(str(part) for part in (url, analysis_hash, prompt_hash, question, index))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _claimed_positive(question: str, entry: dict[str, Any]) -> bool:
    """심판 주장(claimed)을 사람 표시의 '근거 있음' 과 같은 축으로 접는다.

    퀴즈는 answerable 과 answer_correct 가 모두 참일 때만 긍정이다. 사람은 질문 하나에
    한 번 표시하므로 두 필드를 따로 대조하지 않는다.
    """
    if question == "summary":
        return bool(entry.get("faithful"))
    if question == "key_points":
        return bool(entry.get("supported"))
    if question == "production_ideas":
        return bool(entry.get("grounded"))
    if question == "quiz":
        return bool(entry.get("answerable")) and bool(entry.get("answer_correct"))
    return False


def _row_question_entries(row: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """심판 행의 판정 항목마다 (question_id, 질문 종류, 항목). id 는 find_disagreements 와 같은 규칙."""
    verdicts = row.get("verdicts") or {}
    url, analysis_hash, prompt_hash = row.get("url"), row.get("analysis_hash"), row.get("prompt_hash")
    out: list[tuple[str, str, dict[str, Any]]] = []
    for kind in QUESTION_KINDS:
        if kind == "summary":
            entries = [verdicts.get("summary")] if verdicts.get("summary") else []
        else:
            entries = list(verdicts.get(kind) or [])
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            index = 0 if kind == "summary" else entry.get("index", position)
            out.append((question_id(url, analysis_hash, prompt_hash, kind, index), kind, entry))
    return out


def compute_human_agreement(
    rows: list[dict[str, Any]], labels: dict[str, dict[str, Any]] | None
) -> dict[str, Any] | None:
    """심판별 사람 일치율. human 이 보류(unsure)인 라벨은 분모에서 뺀다.

    라벨은 **id 로만** 매칭한다. id 가 (url, analysis_hash, prompt_hash, 질문, 번호)의
    결정적 해시라 심판 행 쪽에서 같은 규칙으로 계산해 대조하면 되고, 라벨에 실린
    url 등 키 필드는 사람이 읽기 위한 보조 정보일 뿐이다. 심판 행이 없는 라벨은 그
    심판에서 세지 않는다.
    """
    if not labels:
        return None
    index = _judged_index(rows)
    by_judge = {judge_id: {"compared": 0, "agreed": 0} for judge_id in sorted(index)}
    unsure = sum(1 for label in labels.values() if label.get("human") == "unsure")
    active = {label_id: label for label_id, label in labels.items() if label.get("human") != "unsure"}
    for judge_id, items in index.items():
        for row in items.values():
            for label_id, kind, entry in _row_question_entries(row):
                label = active.get(label_id)
                if label is None:
                    continue
                by_judge[judge_id]["compared"] += 1
                if _claimed_positive(kind, entry) == (label.get("human") == "supported"):
                    by_judge[judge_id]["agreed"] += 1
    return {
        "labels": len(labels),
        "unsure": unsure,
        "by_judge": {
            judge_id: {**counts, "rate": _rate(counts["agreed"], counts["compared"])}
            for judge_id, counts in by_judge.items()
        },
    }


def build_report(
    rows: list[dict[str, Any]],
    *,
    window_days: int = 14,
    until: date | None = None,
    prompt_hash: str | None = PROMPT_HASH,
    current_keys: set[tuple[str, str]] | None = None,
    labels: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """심판별 주(ISO 월요일 시작) 단위와 최근 창 지표, 두 심판의 일치율, 사람 일치율.

    최근 창의 기준일(until)은 오늘이 아니라 판정 데이터의 마지막 날짜다(evals/run.py 의
    `_recent_since` 와 같은 규칙). 파이프라인이 멈춰도 창이 통째로 비지 않게 한다.
    prompt_hash 가 None 이면 모든 프롬프트의 행을 본다(`--all-prompts`).
    labels 가 있으면(import-labels 로 반입한 사람 표시) 심판별 사람 일치율을 함께 낸다.
    """
    rows, filtered = filter_current_rows(rows, prompt_hash=prompt_hash, current_keys=current_keys)
    latest = list(latest_rows(rows).values())
    dates = [d for d in (_parse_date(row.get("date")) for row in latest) if d is not None]
    if until is None:
        until = max(dates) if dates else date.today()
    today = until
    window_start = until - timedelta(days=max(window_days, 1) - 1)

    by_judge: dict[str, list[dict[str, Any]]] = {}
    for row in latest:
        by_judge.setdefault(str(row.get("judge_id")), []).append(row)

    judges: dict[str, Any] = {}
    for judge_id in sorted(by_judge):
        judge_rows = by_judge[judge_id]
        weeks: dict[str, list[dict[str, Any]]] = {}
        window_rows: list[dict[str, Any]] = []
        for row in judge_rows:
            row_date = _parse_date(row.get("date"))
            if row_date is None:
                continue
            weeks.setdefault(_iso_week(row_date), []).append(row)
            if window_start <= row_date <= today:
                window_rows.append(row)
        judges[judge_id] = {
            "overall": compute_metrics(judge_rows),
            "window": compute_metrics(window_rows),
            "weeks": {week: compute_metrics(weeks[week]) for week in sorted(weeks)},
        }

    window_rows_all = [
        row
        for row in latest
        if (d := _parse_date(row.get("date"))) is not None and window_start <= d <= today
    ]
    return {
        "generated_at": _now_iso(),
        "prompt_hash": prompt_hash,
        "filtered": filtered,
        "window": {"days": window_days, "since": window_start.isoformat(), "until": today.isoformat()},
        "judges": judges,
        "agreement": {
            "overall": compute_agreement(latest),
            "window": compute_agreement(window_rows_all),
        },
        "human": compute_human_agreement(latest, labels),
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def format_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    window = report["window"]
    filtered = report.get("filtered") or {}
    lines.append(
        f"LLM 심판 보고 (prompt {report['prompt_hash'] or '전체'}, 창 {window['since']}~{window['until']},"
        f" 행 {filtered.get('kept', 0)}건 사용, 다른 프롬프트 {filtered.get('other_prompt', 0)}건·"
        f"정본에서 사라진 analysis {filtered.get('stale_analysis', 0)}건 제외)"
    )
    metric_keys = (
        ("summary_faithful_rate", "요약 충실"),
        ("key_point_support_rate", "포인트 근거"),
        ("grounded_idea_rate", "아이디어 근거"),
        ("quiz_answerable_rate", "퀴즈 답가능"),
        ("quiz_answer_correct_rate", "퀴즈 정답일치"),
        ("quote_verified_rate", "인용 검증"),
    )
    if not report["judges"]:
        lines.append("  판정 행이 없다.")
    for judge_id, block in report["judges"].items():
        lines.append(f"[{judge_id}]")
        for label, metrics in (("최근 창", block["window"]), ("전체", block["overall"])):
            lines.append(
                f"  {label}: 판정 {metrics['judged_count']} 건너뜀 {metrics['skipped_count']}"
                f" 오류 {metrics['error_count']} | "
                + " ".join(f"{name} {_fmt(metrics[key])}" for key, name in metric_keys)
            )
        for week, metrics in block["weeks"].items():
            lines.append(
                f"  {week}: 판정 {metrics['judged_count']} | "
                + " ".join(f"{name} {_fmt(metrics[key])}" for key, name in metric_keys)
            )
    agreement = report["agreement"]["overall"]
    if agreement:
        lines.append(
            f"[일치율 {agreement['pair'][0]} vs {agreement['pair'][1]}] 공통 {agreement['items_both_judged']}건"
            f" 전체 {_fmt(agreement['overall_rate'])} | "
            + " ".join(f"{kind} {_fmt(v['rate'])}" for kind, v in agreement["by_question"].items())
        )
    else:
        lines.append("[일치율] 두 심판이 모두 판정한 아이템이 없다.")
    human = report.get("human")
    if human:
        parts = [
            f"{judge_id} {_fmt(block['rate'])} ({block['agreed']}/{block['compared']})"
            for judge_id, block in human["by_judge"].items()
        ]
        lines.append(
            f"[사람 일치율] 표시 {human['labels']}건 (보류 {human['unsure']}건은 분모 제외) | "
            + (" ".join(parts) if parts else "대조할 심판 행이 없다")
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────
# disagreements
# ──────────────────────────────────────────────

EXCERPT_CONTEXT = 300
EXCERPT_HEAD = 600


def body_excerpt(body: str, quotes: list[str | None]) -> str | None:
    """인용 주변 300자. 본문에서 찾는 인용이 없으면 앞 600자."""
    if not body:
        return None
    normalized = normalize_ws(body)
    for quote in quotes:
        candidate = normalize_ws(quote)
        if not candidate:
            continue
        position = normalized.find(candidate)
        if position >= 0:
            start = max(0, position - EXCERPT_CONTEXT)
            end = min(len(normalized), position + len(candidate) + EXCERPT_CONTEXT)
            return normalized[start:end]
    return normalized[:EXCERPT_HEAD]


_QUESTION_FIELDS: dict[str, tuple[str, ...]] = {
    "summary": ("faithful",),
    "key_points": ("supported",),
    "production_ideas": ("grounded",),
    "quiz": ("answerable", "answer_correct"),
}


def _verdict_view(entry: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    view = {field: bool(entry.get(field)) for field in fields}
    view["quote"] = entry.get("quote")
    view["quote_verified"] = bool(entry.get("quote_verified"))
    if "unsupported_claim" in entry:
        view["unsupported_claim"] = entry.get("unsupported_claim")
    return view


def find_disagreements(
    rows: list[dict[str, Any]],
    targets: list[dict[str, Any]] | None = None,
    *,
    prompt_hash: str | None = PROMPT_HASH,
    current_keys: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """두 심판이 어느 질문이든 엇갈린 아이템을 사람 표시용으로 낸다.

    targets 가 주어지고 current_keys 가 없으면 targets 에서 정본 키를 만들어 옛 analysis 행을 거른다.
    """
    if current_keys is None and targets is not None:
        current_keys = {(str(t["url"]), t["analysis_hash"]) for t in targets if t.get("url")}
    rows, _ = filter_current_rows(rows, prompt_hash=prompt_hash, current_keys=current_keys)
    index = _judged_index(rows)
    judge_ids = sorted(index)
    if len(judge_ids) < 2:
        return []
    first, second = judge_ids[0], judge_ids[1]
    bodies: dict[tuple[str, str], dict[str, Any]] = {}
    for target in targets or []:
        bodies[(str(target["url"]), target["analysis_hash"])] = target

    items: list[dict[str, Any]] = []
    for key in sorted(set(index[first]) & set(index[second])):
        left_row, right_row = index[first][key], index[second][key]
        left, right = left_row["verdicts"], right_row["verdicts"]
        target = bodies.get(key[:2])
        body = target["body"] if target else ""
        questions: list[dict[str, Any]] = []

        for kind, fields in _QUESTION_FIELDS.items():
            if kind == "summary":
                pairs = [(0, left.get("summary") or {}, right.get("summary") or {})]
            else:
                right_map = {e["index"]: e for e in right.get(kind) or []}
                pairs = [
                    (e["index"], e, right_map[e["index"]])
                    for e in left.get(kind) or []
                    if e["index"] in right_map
                ]
            for position, left_entry, right_entry in pairs:
                if not left_entry or not right_entry:
                    continue
                if all(bool(left_entry.get(f)) == bool(right_entry.get(f)) for f in fields):
                    continue
                quotes = [left_entry.get("quote"), right_entry.get("quote")]
                questions.append(
                    {
                        "id": question_id(key[0], key[1], key[2], kind, position),
                        "question": kind,
                        "index": position,
                        "text": left_entry.get("text"),
                        "verdicts": {
                            first: _verdict_view(left_entry, fields),
                            second: _verdict_view(right_entry, fields),
                        },
                        "excerpt": body_excerpt(body, quotes),
                        "human": None,
                    }
                )
        if questions:
            items.append(
                {
                    "date": left_row.get("date"),
                    "url": left_row.get("url"),
                    "title": left_row.get("title") or (target or {}).get("title"),
                    "analysis_hash": key[1],
                    "prompt_hash": key[2],
                    "judges": [first, second],
                    "questions": questions,
                }
            )
    return items


# ──────────────────────────────────────────────
# 사람 표시: labels.jsonl
# ──────────────────────────────────────────────

# 라벨에 함께 실리는 보조 필드. 매칭은 id 로만 하고, 이 필드들은 사람이 읽기 위한 것이다.
LABEL_KEY_FIELDS = ("url", "analysis_hash", "prompt_hash", "question", "index")


def resolve_labels_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("JUDGE_LABELS_PATH")
    return Path(env) if env else DEFAULT_LABELS_PATH


def normalize_label(raw: Any) -> dict[str, Any] | None:
    """페이지가 낸 라벨 하나를 저장 형태로 다듬는다. id 나 human 이 없거나 값이 틀리면 None."""
    if not isinstance(raw, dict):
        return None
    label_id = str(raw.get("id") or "").strip()
    human = str(raw.get("human") or "").strip()
    if not label_id or human not in HUMAN_CHOICES:
        return None
    record: dict[str, Any] = {"id": label_id, "human": human, "note": str(raw.get("note") or "").strip()}
    for field in LABEL_KEY_FIELDS:
        record[field] = raw.get(field)
    return record


def load_labels(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """labels.jsonl 을 읽어 id → 마지막 레코드. 같은 id 가 여러 번이면 뒤의 것이 이긴다."""
    labels_path = resolve_labels_path(path)
    latest: dict[str, dict[str, Any]] = {}
    if not labels_path.exists():
        return latest
    with labels_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = normalize_label(json.loads(line))
            except json.JSONDecodeError:
                continue
            if record is not None:
                latest[record["id"]] = record
    return latest


def import_labels(source: Path | str, labels_path: Path | str | None = None) -> dict[str, int]:
    """페이지에서 복사한 JSON(목록 또는 {"labels": [...]})을 labels.jsonl 에 append 한다."""
    payload = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("labels")
    if not isinstance(payload, list):
        raise ValueError("라벨 JSON 은 목록이거나 {\"labels\": [...]} 여야 한다")
    target = resolve_labels_path(labels_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    imported = rejected = 0
    labeled_at = _now_iso()
    with target.open("a", encoding="utf-8") as handle:
        for raw in payload:
            record = normalize_label(raw)
            if record is None:
                rejected += 1
                continue
            record["labeled_at"] = labeled_at
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            imported += 1
    return {"imported": imported, "rejected": rejected}


# ──────────────────────────────────────────────
# 사람 표시: HTML 페이지
# ──────────────────────────────────────────────

QUESTION_LABELS_KO = {
    "summary": "요약",
    "key_points": "핵심 포인트",
    "production_ideas": "적용 아이디어",
    "quiz": "퀴즈",
}
_FIELD_LABELS_KO = {
    "faithful": "충실",
    "supported": "근거",
    "grounded": "출발점",
    "answerable": "답가능",
    "answer_correct": "정답일치",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _verdict_html(judge_id: str, question: str, view: dict[str, Any]) -> str:
    fields = _QUESTION_FIELDS.get(question, ())
    flags = " ".join(
        f'<span class="flag {"yes" if view.get(f) else "no"}">{_FIELD_LABELS_KO.get(f, f)}'
        f' {"예" if view.get(f) else "아니오"}</span>'
        for f in fields
    )
    quote = view.get("quote")
    if quote:
        verified = "본문에 있음" if view.get("quote_verified") else "본문에 없음"
        quote_html = f'<blockquote>{_esc(quote)}</blockquote><small class="qv">인용 검증: {verified}</small>'
    else:
        quote_html = '<small class="qv">인용 없음</small>'
    claim = view.get("unsupported_claim")
    claim_html = f'<p class="claim">근거 없다고 본 주장: {_esc(claim)}</p>' if claim else ""
    return f'<div class="verdict"><strong>{_esc(judge_id)}</strong> {flags}{claim_html}{quote_html}</div>'


def _question_html(item: dict[str, Any], question: dict[str, Any]) -> str:
    kind = str(question.get("question"))
    index = question.get("index")
    label_id = _label_id_of(item, question)
    verdicts = question.get("verdicts") or {}
    verdict_blocks = "".join(_verdict_html(j, kind, v or {}) for j, v in verdicts.items())
    radios = "".join(
        f'<label><input type="radio" name="h-{_esc(label_id)}" value="{choice}"> {HUMAN_LABELS_KO[choice]}</label>'
        for choice in HUMAN_CHOICES
    )
    excerpt = question.get("excerpt")
    excerpt_html = (
        f"<details><summary>본문 발췌</summary><p class=\"excerpt\">{_esc(excerpt)}</p></details>"
        if excerpt
        else '<p class="excerpt none">본문 발췌 없음</p>'
    )
    title = item.get("title") or item.get("url") or "(제목 없음)"
    return (
        f'<section class="q" data-id="{_esc(label_id)}" data-url="{_esc(item.get("url"))}"'
        f' data-analysis-hash="{_esc(item.get("analysis_hash"))}" data-prompt-hash="{_esc(item.get("prompt_hash"))}"'
        f' data-question="{_esc(kind)}" data-index="{_esc(index)}">'
        f'<div class="meta">{_esc(item.get("date"))} · <a href="{_esc(item.get("url"))}" target="_blank" rel="noopener">'
        f"{_esc(title)}</a> · {QUESTION_LABELS_KO.get(kind, _esc(kind))} #{_esc(index)}</div>"
        f'<p class="target">{_esc(question.get("text"))}</p>'
        f'<div class="verdicts">{verdict_blocks}</div>'
        f"{excerpt_html}"
        f'<div class="human">{radios} <input type="text" class="note" placeholder="한 줄 메모" maxlength="200"></div>'
        "</section>"
    )


_LABELING_CSS = """
:root{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#ddd;--yes:#1a7f37;--no:#b42318;--box:#f6f7f9}
@media(prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e8e8e8;--muted:#9a9a9a;--line:#333;--yes:#4ade80;--no:#f87171;--box:#1c2026}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
main{max-width:860px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.lead{color:var(--muted);margin:0 0 16px}
.q{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 14px}
.meta{color:var(--muted);font-size:13px;word-break:break-all}
.meta a{color:inherit}
.target{font-weight:600;margin:6px 0 10px}
.verdict{background:var(--box);border-radius:6px;padding:8px 10px;margin:0 0 6px}
.flag{display:inline-block;margin-left:6px;font-size:13px}
.flag.yes{color:var(--yes)}.flag.no{color:var(--no)}
.claim{margin:6px 0 0;font-size:14px}
blockquote{margin:6px 0 2px;padding-left:10px;border-left:3px solid var(--line);white-space:pre-wrap;word-break:break-word}
.qv{color:var(--muted)}
details{margin:6px 0}
.excerpt{white-space:pre-wrap;word-break:break-word;font-size:14px;color:var(--muted)}
.excerpt.none{font-style:italic}
.human{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:10px;padding-top:10px;border-top:1px dashed var(--line)}
.human label{white-space:nowrap}
.note{flex:1 1 200px;min-width:0;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.foot{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--box);color:var(--fg);cursor:pointer;font:inherit}
#status{color:var(--muted);font-size:14px}
#fallback{width:100%;min-height:140px;font:13px/1.4 ui-monospace,Menlo,monospace;display:none;margin-top:8px}
.empty{padding:40px 0;text-align:center;color:var(--muted)}
"""

_LABELING_JS = """
(function(){
  var sections=document.querySelectorAll('.q');
  var status=document.getElementById('status');
  var fallback=document.getElementById('fallback');
  function collect(){
    var out=[];
    sections.forEach(function(s){
      var r=s.querySelector('input[type=radio]:checked');
      if(!r) return;
      var note=s.querySelector('.note');
      out.push({id:s.dataset.id,url:s.dataset.url,analysis_hash:s.dataset.analysisHash,
        prompt_hash:s.dataset.promptHash,question:s.dataset.question,index:Number(s.dataset.index),
        human:r.value,note:note?note.value.trim():''});
    });
    return out;
  }
  function refresh(){
    if(status) status.textContent='표시 '+collect().length+' / '+sections.length+'건';
  }
  document.addEventListener('change',refresh);
  refresh();
  var btn=document.getElementById('copy');
  if(btn) btn.addEventListener('click',function(){
    var text=JSON.stringify(collect(),null,2);
    function showFallback(){
      fallback.style.display='block';fallback.value=text;fallback.focus();fallback.select();
      status.textContent='클립보드 복사가 막혀 아래 상자에 넣었다. 전체 선택해 복사하라.';
    }
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(function(){
        status.textContent='결과 JSON '+collect().length+'건을 클립보드에 복사했다. import-labels 로 반입하라.';
      },showFallback);
    }else{showFallback();}
  });
})();
"""


def _label_id_of(item: dict[str, Any], question: dict[str, Any]) -> str:
    return str(
        question.get("id")
        or question_id(
            item.get("url"), item.get("analysis_hash"), item.get("prompt_hash"), question.get("question"), question.get("index")
        )
    )


def dedupe_label_questions(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """같은 id 의 질문이 두 번 오면 뒤의 것을 버린다. (정리된 목록, 버린 수)

    id 가 라디오 name 이라 중복되면 두 항목이 한 라디오 그룹으로 묶여 표시가 섞인다.
    """
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        kept: list[dict[str, Any]] = []
        for question in item.get("questions") or []:
            label_id = _label_id_of(item, question)
            if label_id in seen:
                skipped += 1
                continue
            seen.add(label_id)
            kept.append(question)
        if kept:
            cleaned.append({**item, "questions": kept})
    return cleaned, skipped


def build_labeling_html(items: list[dict[str, Any]]) -> str:
    """disagreements 목록으로 사람 표시용 단일 HTML 을 만든다. 외부 리소스를 쓰지 않는다.

    중복 id 는 여기서도 걸러 라디오 name 충돌을 막는다. 건너뛴 수를 알리려면 호출 전에
    dedupe_label_questions 를 직접 부른다(main 이 그렇게 한다).
    """
    items, _ = dedupe_label_questions(items)
    question_count = sum(len(item.get("questions") or []) for item in items)
    if question_count == 0:
        body = '<p class="empty">엇갈린 항목이 없다. 두 심판이 모두 판정한 아이템에서 판정이 갈린 질문이 없다.</p>'
        foot = ""
    else:
        sections = "".join(
            _question_html(item, question) for item in items for question in item.get("questions") or []
        )
        body = sections
        foot = (
            '<div class="foot"><button id="copy" type="button">결과 JSON 복사</button>'
            '<span id="status"></span><textarea id="fallback" readonly></textarea></div>'
        )
    return (
        "<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>LLM 심판 사람 표시</title>"
        f"<style>{_LABELING_CSS}</style></head><body><main>"
        "<h1>LLM 심판 사람 표시</h1>"
        f'<p class="lead">두 심판이 엇갈린 질문 {question_count}건 (아이템 {len(items)}건). '
        "본문 발췌를 보고 산출물에 근거가 있는지 표시한 뒤, 하단에서 결과 JSON 을 복사해"
        " <code>python -m evals.judge import-labels</code> 로 반입한다.</p>"
        f"{body}{foot}"
        f"</main><script>{_LABELING_JS}</script></body></html>\n"
    )


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


def _cli_date(value: str | None) -> date | None:
    if not value:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"날짜 형식이 아니다: {value}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.judge", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="캐시에 없는 아이템을 심판에게 보낸다")
    run_p.add_argument("--judge", required=True, choices=sorted(JUDGES))
    run_p.add_argument("--since", type=_cli_date, default=None)
    run_p.add_argument("--until", type=_cli_date, default=None)
    run_p.add_argument("--limit", type=int, default=None)
    run_p.add_argument("--dry-run", action="store_true")

    report_p = sub.add_parser("report", help="심판별 추세 지표와 일치율")
    report_p.add_argument("--window", type=int, default=14)
    report_p.add_argument("--json", action="store_true")
    report_p.add_argument(
        "--all-prompts", action="store_true", help="현재 PROMPT_HASH 행만이 아니라 모든 프롬프트의 행을 본다"
    )

    dis_p = sub.add_parser("disagreements", help="두 심판이 엇갈린 아이템을 내보낸다")
    dis_p.add_argument("--out", type=Path, default=None)
    dis_p.add_argument("--all-prompts", action="store_true")

    lab_p = sub.add_parser("labeling", help="엇갈린 아이템을 사람이 표시할 HTML 한 장으로 만든다")
    lab_p.add_argument("--out", type=Path, required=True)
    lab_p.add_argument(
        "--in", dest="source", type=Path, default=None, help="disagreements JSON. 없으면 캐시에서 바로 계산한다"
    )
    lab_p.add_argument("--all-prompts", action="store_true")

    imp_p = sub.add_parser("import-labels", help="표시 페이지에서 복사한 JSON 을 labels.jsonl 에 append 한다")
    imp_p.add_argument("source", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    args = build_parser().parse_args(argv)
    if args.command == "run":
        summary = run_judge(
            args.judge, since=args.since, until=args.until, limit=args.limit, dry_run=args.dry_run
        )
        return 0 if summary["failed"] == 0 else 1
    if args.command == "report":
        report = build_report(
            load_cache(),
            window_days=args.window,
            prompt_hash=None if args.all_prompts else PROMPT_HASH,
            current_keys=current_item_keys(),
            labels=load_labels(),
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_report(report))
        return 0
    if args.command == "disagreements":
        items = find_disagreements(
            load_cache(), collect_targets(), prompt_hash=None if args.all_prompts else PROMPT_HASH
        )
        text = json.dumps(items, ensure_ascii=False, indent=2)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text + "\n", encoding="utf-8")
            print(f"엇갈린 아이템 {len(items)}건 → {args.out}")
        else:
            print(text)
        return 0
    if args.command == "labeling":
        if args.source is not None:
            items = json.loads(args.source.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                print("disagreements JSON 은 목록이어야 한다", file=sys.stderr)
                return 2
        else:
            items = find_disagreements(
                load_cache(), collect_targets(), prompt_hash=None if args.all_prompts else PROMPT_HASH
            )
        items, skipped = dedupe_label_questions(items)
        page = build_labeling_html(items)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
        question_count = sum(len(item.get("questions") or []) for item in items)
        print(f"표시 페이지 생성: 질문 {question_count}건 (아이템 {len(items)}건) → {args.out}")
        if skipped:
            print(f"  같은 id 의 질문 {skipped}건은 중복이라 건너뛰었다")
        return 0
    if args.command == "import-labels":
        try:
            result = import_labels(args.source)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"라벨 반입 실패: {exc}", file=sys.stderr)
            return 2
        print(
            f"라벨 {result['imported']}건 반입 (거부 {result['rejected']}건) → {resolve_labels_path()}"
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
