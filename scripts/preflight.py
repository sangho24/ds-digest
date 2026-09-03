"""발송 전 예비 점검 — 배선이 깨졌는지 실제 코드로 확인한다.

    python scripts/preflight.py     (종료 코드 0=통과, 1=실패)


API 키가 없으므로 httpx.AsyncClient.post만 가짜로 바꾸고, 프롬프트 조립·페이로드
구성·응답 파싱·점수 혼합·근거 천장 등 **파이프라인 코드는 전부 실제로** 태운다.
모델의 판단력은 검증할 수 없지만, 배선이 깨졌는지는 여기서 다 걸린다.

유닛 테스트와의 차이: 유닛 테스트는 대개 _call_llm_with_fallback을 대역으로
두므로 _call_groq의 페이로드 조립(response_format·strict 스키마)과 프롬프트
.format()이 실행되지 않는다. 여기서는 httpx 레벨에서만 끊으므로 그 구간이
전부 실제로 돈다.

특히 잡으려는 것:
  - 프롬프트 .format() 키 불일치 (한 건이라도 나면 전 아이템 분석 실패 → 빈 다이제스트)
  - 새로 넣은 standing_note / concept_vocabulary 키가 실제 경로에서 채워지는가
  - 상대 평가가 strict 스키마로 나가고 혼합·천장이 적용되는가
  - 타임스탬프가 전사 → 분석 → 레코드까지 살아서 도착하는가
  - 난이도 지시가 depth 가중·상대 평가 프롬프트까지 실제로 닿는가
  - 논문 배경·위치(positioning)가 프롬프트에 요청되고 결과까지 파싱되는가
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import app.analyzer as analyzer
import app.transcript_gemini as tg
from app.config import Settings
from app.directives import Directive
from app.models import RawContent, SourceType, UserProfile

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── 가짜 전송 계층 ────────────────────────────────────────────────────────
sent = []          # 보낸 요청 (url, payload)

TRANSCRIPT_WITH_TS = (
    "[00:00] 안녕하세요, 오늘은 피처 스토어 운영에 대해 이야기하겠습니다.\n"
    "[00:47] 먼저 온라인/오프라인 저장소를 분리해야 하는 이유부터 보겠습니다.\n"
    "[02:15] 실제로 저희 팀은 Redis를 온라인 저장소로 쓰고 있고, 지연이 8ms입니다.\n"
) * 12  # 근거 수준 full이 되도록 충분히 길게


def _groq_body(content: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}


def _gemini_body(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}, ]}


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.headers, self.text = code, body, {}, json.dumps(body)

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=None)


ANALYSIS_REPLY = {
    "one_line_summary": "온라인 저장소를 Redis로 분리해 피처 조회 지연을 8ms로 낮춘 사례",
    "domain": ["data-eng"],
    "content_type": "case-study",
    "half_life": "durable",
    "tags": ["Redis", "Feature Store"],
    "concepts": ["피처 스토어"],
    "key_points": [
        {"point": "온라인/오프라인 저장소 분리", "timestamp": "00:47"},
        {"point": "Redis 온라인 저장소 지연 8ms", "timestamp": "02:15"},
    ],
    "positioning": "기존 피처 스토어는 배치 조회를 전제했는데, 이 사례는 온라인 조회를 분리한다.",
    "actionability": 7,
    "depth": 6,
    "skip_reason": None,
    "production_ideas": ["Redis 기반 온라인 피처 저장소 PoC"],
    "quiz": [{"question": "온라인 저장소 지연은?", "options": ["8ms", "80ms", "800ms"],
              "answer_index": 0, "explanation": "발표에서 8ms로 언급"}],
}


async def fake_post(self, url, headers=None, json=None, params=None, **kw):
    payload = json or {}
    sent.append((url, payload))

    if "generativelanguage" in url:                       # Gemini
        prompt = payload.get("contents", [{}])[0].get("parts", [{}])[0].get("text", "")
        if "받아쓰세요" in prompt:                          # 전사
            return Resp(200, _gemini_body(TRANSCRIPT_WITH_TS))
        return Resp(200, _gemini_body(__import__("json").dumps(ANALYSIS_REPLY)))

    # Groq — 프롬프트를 보고 어떤 단계인지 판별한다.
    prompt = payload["messages"][0]["content"]
    if "top_indices" in prompt:
        return Resp(200, _groq_body({"top_indices": [0, 1]}))
    if "서로 비교해서" in prompt:                            # 상대 평가
        n = prompt.count("[")  # 후보 수 근사
        return Resp(200, _groq_body({"ratings": [
            {"index": 0, "rating": 9}, {"index": 1, "rating": 5}, {"index": 2, "rating": 2},
        ]}))
    if "설정 해석기" in prompt:                              # 자연어 지시
        return Resp(200, _groq_body({"boost": ["피처 스토어"], "suppress": [],
                                     "drop_sources": [], "standing_note": "실무 사례 우선",
                                     "difficulty": "harder"}))
    return Resp(200, _groq_body(ANALYSIS_REPLY))            # 분석


def make_items():
    return [
        RawContent(source_type=SourceType.YOUTUBE, source_name="테코톡", source_key="yt_a",
                   title="피처 스토어 운영기", url="https://youtu.be/AAA",
                   transcript=TRANSCRIPT_WITH_TS),
        RawContent(source_type=SourceType.RSS, source_name="HackerNews", source_key="hackernews",
                   title="Feature stores in production", url="https://news.example/1",
                   body="본문 " * 400),
        RawContent(source_type=SourceType.RSS, source_name="arXiv", source_key="arxiv",
                   title="제목만 있는 논문", url="https://arxiv.org/abs/1"),
    ]


async def main():
    settings = Settings(groq_api_key="k", gemini_api_key="g", dry_run=False,
                        max_items_per_digest=5, relevance_floor=4, _env_file=None)
    analyzer.get_settings = lambda: settings
    tg.get_settings = lambda: settings
    async def _no_sleep(*_a, **_k):
        return None
    analyzer.asyncio.sleep = _no_sleep
    # 개념 어휘·퀴즈 라벨은 실제 파일을 건드리지 않게 비워둔다.
    analyzer.load_vocabulary = lambda *a, **k: {"version": 1, "concepts": {}}
    analyzer.save_vocabulary = lambda *a, **k: None
    analyzer.weak_concepts = lambda *a, **k: set()
    httpx.AsyncClient.post = fake_post

    # 자연어 지시 원문을 임시 파일에 심는다 (실제 data/를 건드리지 않는다).
    import app.directives as dmod
    dmod.DIRECTIVES_PATH = Path(tempfile.gettempdir()) / "preflight_directives.jsonl"
    dmod.DIRECTIVES_PATH.unlink(missing_ok=True)
    dmod.capture("요즘 논문보다 실무 사례가 더 필요해")

    items = make_items()
    profile = UserProfile()

    print("\n[1] 자연어 지시 해석")
    directive = await analyzer.resolve_directives(items)
    check("지시 프롬프트가 포맷된다", any("설정 해석기" in p["messages"][0]["content"]
                                    for _, p in sent if "messages" in p))
    check("standing_note 파싱", directive.standing_note == "실무 사례 우선", directive.describe())
    # 자유 텍스트는 정렬 키가 될 수 없다. 난이도 지시가 구조화 축으로 떨어져야
    # 선정에 닿는다(실측 2026-09-03: 해석은 맞았는데 선정은 지시와 무관했다).
    check("난이도 지시가 depth_bias로 떨어짐", directive.depth_bias == 1,
          f"depth_bias={directive.depth_bias}")
    check("적용 중인 지시에 난이도가 보인다", "🎚" in directive.describe(), directive.describe())

    print("\n[2] Stage 1 메타 랭킹 (standing_note 주입)")
    sent.clear()
    await analyzer.resolve_youtube_transcripts(items[:1], profile, 1, directive)
    print("     (YouTube 1건이라 랭킹 생략 — 프롬프트 포맷은 아래에서 직접 검증)")
    rank_prompt = analyzer._METADATA_RANKING_PROMPT.format(
        budget=2, keywords="없음", liked_tags="없음", disliked_tags="없음",
        standing_note=directive.standing_note, listing="[0] ...")
    check("랭킹 프롬프트 .format 성공", "실무 사례 우선" in rank_prompt)

    print("\n[3] 분석 — 프롬프트 조립 + 타임스탬프 관통")
    sent.clear()
    digest = await analyzer.filter_and_analyze(items, profile, directive)
    analysis_prompts = [p["messages"][0]["content"] for _, p in sent
                        if "messages" in p and "분석 기준" in p["messages"][0]["content"]]
    check("분석 프롬프트 .format 성공 (KeyError 없음)", len(analysis_prompts) == 3,
          f"{len(analysis_prompts)}건")
    check("concept_vocabulary 주입됨", all("기존 개념" in p for p in analysis_prompts))
    check("standing_note 주입됨", all("실무 사례 우선" in p for p in analysis_prompts))
    check("전사의 [MM:SS]가 프롬프트에 실림", any("[00:47]" in p for p in analysis_prompts))
    check("positioning이 프롬프트에서 요청됨", all("positioning" in p for p in analysis_prompts))
    positioning = [d.analysis.positioning for d in digest if d.analysis.positioning]
    check("positioning이 분석 결과까지 도달", bool(positioning),
          f"{(positioning[0][:40] + '…') if positioning else '없음'}")

    ts = [kp.timestamp for d in digest for kp in d.analysis.key_points if kp.timestamp]
    check("타임스탬프가 분석 결과까지 도달", len(ts) > 0, f"{ts[:3]}")

    print("\n[4] 상대 평가")
    rating_calls = [p for _, p in sent if "messages" in p
                    and "서로 비교해서" in p["messages"][0]["content"]]
    check("상대 평가가 호출됨", len(rating_calls) == 1)
    if rating_calls:
        # 점수를 벌리는 단계에 지시가 없으면 글쓰기만 바뀌고 순위는 그대로다.
        check("상대 평가 프롬프트에 지시가 실림",
              "실무 사례 우선" in rating_calls[0]["messages"][0]["content"])
    if rating_calls:
        rf = rating_calls[0].get("response_format", {})
        check("strict json_schema로 나감",
              rf.get("type") == "json_schema" and rf["json_schema"]["strict"] is True)

    scores = sorted((d.analysis.relevance_score for d in digest), reverse=True)
    check("점수가 벌어졌다 (최고-최저 >= 3)", len(scores) >= 2 and scores[0] - scores[-1] >= 3,
          f"{scores}")

    print("\n[5] 근거 게이트가 상대 평가에 뚫리지 않는가")
    thin = [d for d in digest if d.raw.url.startswith("https://arxiv")]
    for d in thin:
        cap = analyzer.evidence_ceiling(d.analysis.evidence_level)
        check(f"제목만 아이템이 천장({cap}) 이하", d.analysis.relevance_score <= cap,
              f"근거={d.analysis.evidence_level.value} 점수={d.analysis.relevance_score}")

    print("\n[6] 난이도 지시가 점수 축을 옮기는가")
    check("depth 가중이 지시로 이동", analyzer.depth_weight_for(directive) == analyzer.DEPTH_WEIGHT_HARDER,
          f"{analyzer.DEPTH_WEIGHT} → {analyzer.depth_weight_for(directive)}")
    # 얕지만 실행 가능한 글 vs 깊지만 바로 못 쓰는 논문의 순서가 뒤집혀야 한다.
    shallow = analyzer.derive_relevance_score(8, 3, analyzer.depth_weight_for(directive))
    deep = analyzer.derive_relevance_score(3, 8, analyzer.depth_weight_for(directive))
    check("'더 어렵게'에서 깊은 쪽이 앞선다", deep > shallow, f"깊음 {deep} > 얕음 {shallow}")

    print("\n[7] 개념 추출")
    concepts = sorted({c for d in digest for c in d.analysis.concepts})
    check("개념이 추출·해소됨", bool(concepts), f"{concepts}")

    print("\n" + "=" * 60)
    print(f"통과 {len(PASS)} / 실패 {len(FAIL)}")
    if FAIL:
        print("실패 항목:", FAIL)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
