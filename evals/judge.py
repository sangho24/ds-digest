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
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, TextIO

import httpx

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "data" / "records"
DEFAULT_CACHE_PATH = ROOT / "data" / "evals" / "judgments.jsonl"

JUDGE_MAX_CHARS = int(os.environ.get("JUDGE_MAX_CHARS", "16000"))
MAX_WORKERS = 4
REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.0
JUDGEABLE_EVIDENCE_LEVEL = "full"

QUESTION_KINDS = ("summary", "key_points", "production_ideas", "quiz")

# 심판 제공자. model 은 환경변수(model_env)로 덮어쓸 수 있다.
JUDGES: dict[str, dict[str, str]] = {
    "deepseek": {
        "id": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "model_env": "JUDGE_DEEPSEEK_MODEL",
        "api_key_env": "DEEPSEEK_API_KEY",
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


def build_report(
    rows: list[dict[str, Any]],
    *,
    window_days: int = 14,
    until: date | None = None,
    prompt_hash: str | None = PROMPT_HASH,
    current_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """심판별 주(ISO 월요일 시작) 단위와 최근 창 지표, 두 심판의 일치율.

    최근 창의 기준일(until)은 오늘이 아니라 판정 데이터의 마지막 날짜다(evals/run.py 의
    `_recent_since` 와 같은 규칙). 파이프라인이 멈춰도 창이 통째로 비지 않게 한다.
    prompt_hash 가 None 이면 모든 프롬프트의 행을 본다(`--all-prompts`).
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
    return parser


def main(argv: list[str] | None = None) -> int:
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
