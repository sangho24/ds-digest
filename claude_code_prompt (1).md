# DS Digest — Claude Code 작업 지시서

아래 사양에 맞는 Python 프로젝트를 처음부터 만들어줘.

## 프로젝트 개요

매일 아침 Data Science 관련 YouTube, 블로그, 뉴스레터에서 콘텐츠를 자동 수집 → AI로 필터링/분석 → 이메일 뉴스레터로 발송하는 파이프라인이야. 핵심은 사용자 피드백 루프가 있어서, 👍/👎 + 키워드 요청이 다음 날 큐레이션에 반영되는 구조야.

## 파이프라인 흐름

```
Collect (YouTube RSS + Blog RSS)
  → Dedupe (Supabase에서 이미 본 URL 제거)
  → Batch Filter (전체 아이템을 Claude API 1회 호출로 점수화 — API 절약)
  → Analyze (통과한 아이템별 상세 분석 — 요약/타임스탬프/적용아이디어/퀴즈)
  → Render (Jinja2 HTML 이메일 템플릿)
  → Send (Resend)
  → Archive (Supabase에 발송 이력 저장)

피드백 루프:
  이메일 내 👍/👎/키워드요청 버튼 클릭
  → FastAPI GET 엔드포인트
  → Supabase feedback 테이블에 저장
  → 다음 날 파이프라인에서 프로필 읽어서 필터링/분석 프롬프트에 반영
```

## 디렉토리 구조

```
ds-digest/
├── .env.example
├── .gitignore
├── requirements.txt
├── supabase_schema.sql          ← DB 테이블 초기화 SQL
├── .github/workflows/
│   └── daily_digest.yml         ← GitHub Actions cron (UTC 23:00 = KST 08:00)
└── app/
    ├── config.py                ← pydantic-settings 기반 환경변수 관리
    ├── main.py                  ← FastAPI (피드백 수신 엔드포인트)
    ├── sources.py               ← 수집 소스 설정 (아래 목록 참고)
    ├── collectors/
    │   ├── base.py              ← ContentItem 데이터클래스
    │   ├── youtube.py           ← 채널 RSS + youtube-transcript-api
    │   └── rss.py               ← RSS/Atom 피드 수집 (feedparser)
    ├── filters/
    │   └── claude_filter.py     ← 전체 아이템 배치 점수화 (Claude API 1회)
    ├── analyzers/
    │   └── claude_analyzer.py   ← 아이템별 상세 분석 (요약/퀴즈/아이디어)
    ├── renderers/
    │   └── html.py              ← Jinja2 이메일 렌더링
    ├── templates/
    │   └── digest.html          ← 뉴스레터 HTML 템플릿
    ├── deliverers/
    │   └── resend_client.py     ← Resend 이메일 발송
    ├── db/
    │   └── client.py            ← Supabase 연결 (중복방지 + 피드백 저장)
    └── jobs/
        └── daily_digest.py      ← 파이프라인 오케스트레이터
```

## 수집 소스 목록

### Tier 1 (MVP — 바로 사용)

YouTube 채널:
- 토스 SLASH: UC-ooeEMOToyByAC1WOQE8ew
- 우아한테크 (배민): UC-mOekGSesms0agFntnQang
- if(kakao): UCwKk-EEF0gmsHJ5z3CVnRzA
- NAVER D2: UCNrehnUq7Il-J7HQxrzp7CA

RSS 피드:
- 토스 기술 블로그: https://toss.tech/rss.xml
- 당근 테크 블로그: https://medium.com/feed/daangn
- 우아한형제들 블로그: https://techblog.woowahan.com/feed/
- 카카오 기술 블로그: https://tech.kakao.com/blog/feed/
- The Batch (Andrew Ng): https://www.deeplearning.ai/the-batch/feed/
- Data Elixir: https://dataelixir.com/issues.rss

### Tier 2 (나중에 .env에서 추가)

YouTube: Two Minute Papers, Yannic Kilcher, 3Blue1Brown, ML Street Talk
RSS: Netflix Tech Blog, Uber Engineering, Airbnb Engineering, Spotify Engineering, arXiv stat.ML, arXiv cs.AI

sources.py에 Tier 1/2/3 전체를 딕셔너리로 정리하고, `python app/sources.py` 실행하면 .env에 붙여넣을 값을 출력하는 헬퍼 함수를 만들어줘.

## 기술 스택

- Python 3.12+
- FastAPI + uvicorn (피드백 webhook)
- anthropic SDK (Claude Sonnet for filtering & analysis)
- youtube-transcript-api (자막 추출, 실패 시 영상 description fallback)
- feedparser (RSS 수집)
- httpx (async HTTP)
- supabase-py (DB)
- resend (이메일 발송)
- jinja2 (뉴스레터 템플릿)
- pydantic + pydantic-settings (설정 & 모델)
- structlog (로깅)
- GitHub Actions (cron 스케줄링)

## 핵심 설계 요구사항

### 1. 필터링 — 배치 1회 호출
수집된 아이템 전체(제목 + 출처 목록)를 Claude에게 한 번에 보내서 각각 1-10 점수를 매기게 해. 아이템 수 × API 호출이 아니라, 1회 호출로 전부 처리하는 거야. 무료/저가 사용에서 rate limit을 아끼기 위해.

프롬프트에 사용자 프로필(선호 토픽, 최근 좋아요/싫어요 히스토리, 키워드 요청)을 포함시켜서 개인화된 점수를 매기도록 해.

### 2. 분석 — 아이템별 호출
필터 통과한 아이템(threshold 이상)에 대해서만 개별 Claude 호출. 각 호출에서 뽑을 것:
- 한 줄 요약 (한국어, 30자 이내)
- 핵심 포인트 최대 3개 + 타임스탬프 (영상인 경우 MM:SS)
- 현업 프로덕션 적용 아이디어 1-2개
- 내용 확인 퀴즈 2문항 (4지선다, 정답 인덱스, 해설) — 반드시 해당 콘텐츠 고유의 내용으로 출제

rate limit 대응: 호출 간 6초 간격 (RPM 10 기준). 실패 시 3회 재시도 (지수 백오프).
JSON 파싱: ```json 블록 자동 제거 처리.

### 3. Supabase 스키마

테이블 3개:
- `seen_items`: url (PK), title, source, first_seen_at → 중복 방지
- `feedback`: id, user_id, item_url, action (like/dislike/keyword_request), keyword, created_at
- `user_profiles`: user_id (PK), preferred_topics (jsonb), keyword_requests (jsonb), updated_at

supabase_schema.sql로 CREATE TABLE 문을 제공해줘.

중복 방지 정책: is_seen() 호출 시 Supabase 연결 실패하면 False 반환 (fail-open). 미발송보다 가끔 중복이 나은 trade-off.

### 4. 이메일 뉴스레터 템플릿

이메일 클라이언트 호환을 고려해줘:
- JavaScript 미동작 → 퀴즈 정답을 display:none으로 숨기지 말고 항상 표시
- 피드백 버튼은 GET 링크로: `{feedback_base_url}?action=like&item_url={url_encoded}`
- "키워드 요청하기" 버튼 → FastAPI가 간단한 입력 폼 HTML을 반환
- 깔끔한 이메일 디자인 (인라인 CSS, max-width 600px)

각 아이템에 표시할 것:
- 소스 뱃지 (YOUTUBE / RSS) + 관련도 점수
- 제목 (링크)
- 한 줄 요약
- 핵심 포인트 (타임스탬프 포함)
- 현업 적용 아이디어 (💡 아이콘)
- 퀴즈 (질문 + 선택지 + 정답/해설)
- 👍 도움됐어 / 👎 별로야 버튼

### 5. 피드백 엔드포인트 (FastAPI)

```
GET /api/feedback?action=like&item_url=...&user_id=default
GET /api/feedback?action=dislike&item_url=...&user_id=default  
GET /api/feedback?action=keyword_request&keyword=...&user_id=default
```

- like/dislike → Supabase feedback 테이블에 저장 + "감사합니다" HTML 반환
- keyword_request에 keyword가 비어있으면 → 입력 폼 HTML 반환
- keyword_request에 keyword가 있으면 → 저장 + "등록됐습니다" HTML 반환
- POST /api/trigger → daily_digest 수동 트리거 (개발용)

### 6. YouTube 트랜스크립트 fallback

youtube-transcript-api는 비공식이라 차단될 수 있어:
- 한국어 자막 우선 → 영어 자막 → 자동생성 자막 순서로 시도
- 전부 실패 시 영상 description 텍스트로 fallback
- 파이프라인은 절대 중단하지 말고 가능한 데이터로 계속 진행

### 7. GitHub Actions

```yaml
on:
  schedule:
    - cron: '0 23 * * *'  # UTC 23:00 = KST 08:00
  workflow_dispatch:        # 수동 트리거
```

모든 환경변수는 GitHub Secrets에서 주입.

### 8. .env.example

```
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-20250514
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJ...
RESEND_API_KEY=re_...
EMAIL_FROM=onboarding@resend.dev  # 무료 플랜은 이것만 가능. 커스텀 도메인은 DNS 인증 필요
EMAIL_TO=you@example.com
YOUTUBE_CHANNELS=UC-ooeEMOToyByAC1WOQE8ew,UC-mOekGSesms0agFntnQang,...
RSS_FEEDS=https://toss.tech/rss.xml,https://medium.com/feed/daangn,...
MAX_ITEMS_PER_DIGEST=5
RELEVANCE_THRESHOLD=7
LOG_LEVEL=INFO
```

## 알려진 이슈 & 대응 (코드에 반영해줘)

1. **YouTube Transcript 차단** → description fallback 구현. 로그에 warning 남기기.
2. **Claude JSON 파싱 실패** → ```json 블록 제거 + 3회 재시도 (지수 백오프)
3. **퀴즈 동일 내용 방지** → 분석 프롬프트에 "이 콘텐츠 고유의 내용으로 출제하라" 명시
4. **이메일 JS 미동작** → 퀴즈 정답 항상 표시 (display:none 쓰지 않기)
5. **Rate limit** → 분석 호출 간 6초 간격, 필터는 배치 1회
6. **Supabase 연결 실패** → fail-open (중복 허용, 미발송 방지)
7. **Resend 무료 발신** → onboarding@resend.dev 기본값, .env 주석에 안내
8. **GitHub Actions UTC** → cron '0 23 * * *' = KST 08:00

## 시작 방법

프로젝트 전체를 생성한 뒤, README.md에 아래 순서를 적어줘:
1. cp .env.example .env → API 키 채우기
2. Supabase에서 supabase_schema.sql 실행
3. pip install -r requirements.txt
4. python -m app.jobs.daily_digest (수동 테스트)
5. uvicorn app.main:app --reload (피드백 서버)
6. GitHub Actions Secrets 등록 → 자동 발송 시작
