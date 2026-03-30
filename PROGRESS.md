# DS Digest Pipeline — 개발 진행 기록

## 현재 아키텍처

```
GitHub Actions (07:30 KST)
        │
        ▼
  collect_all()
  ├── fetch_youtube_recent()     YouTube RSS → transcript / description fallback
  ├── fetch_rss_recent()         RSS 피드 (48h 필터)
  ├── fetch_arxiv_recent()       ArXiv cs.LG, stat.ML 논문 (48h 필터)
  └── fetch_hackernews_recent()  HackerNews 키워드 스토리 (24h, 점수≥50)
        │
        ▼
  _deduplicate()               Supabase seen_urls bulk 조회 + URL 정규화
        │
        ▼
  filter_and_analyze()         Gemini / Groq → relevance 점수 필터 (≥7)
        │
        ▼
  deliver()
  ├── send_telegram_digest()   텍스트 메시지 + 인라인 피드백 버튼 + tg-spoiler 퀴즈
  └── send_digest()            Resend 이메일 (HTML 템플릿)
        │
        ▼
  _mark_sent()                 발송 URL → Supabase seen_urls 기록
```

**스택**: Python 3.12 / FastAPI / Supabase / Gemini 2.5 Flash (Groq fallback) / Telegram Bot / Resend

---

## 완료된 작업

### 1. 중복 제거 강화 (`db.py`, `daily_digest.py`)
- **문제**: `is_seen()`이 Supabase 장애 시 False 반환(fail-open) + 아이템마다 N번 개별 쿼리 → 일시적 오류 한 번으로 중복 발송
- **수정**:
  - `fetch_seen_urls(urls)` 추가 — 한 번의 `IN` 쿼리로 전체 URL 일괄 조회, 실패 지점 N→1
  - `_normalize_url()` 추가 — 트레일링 슬래시·대소문자 차이로 인한 URL 미스매치 방지
  - mark_seen도 정규화된 URL로 저장

### 2. 관련도 점수 분산 (`analyzer.py`)
- **문제**: 프롬프트에 점수 정의가 없어 LLM이 보수적으로 7-8에 클러스터링
- **수정**: 1-10 각 점수의 구체적 기준 정의 + 자기 검토 지시("이 콘텐츠가 정말 9점인가?") 추가

### 3. Telegram 퀴즈 토글 (`deliverers/telegram.py`)
- **문제**: Telegram은 HTML `<details>` 미지원 → 정답이 항상 노출
- **수정**: `<tg-spoiler>` 태그로 변경 — 탭해야 정답 확인 가능한 Telegram 네이티브 스포일러

### 4. YouTube 타임라인 싱크 (`analyzer.py`)
- **문제**: LLM이 transcript 실제 타임스탬프 대신 임의로 생성(hallucination)
- **수정**: 프롬프트에 "자막에 실제로 등장한 [MM:SS]만 사용, 없으면 null" 명시

### 5. Transcript 없을 때 타임라인 처리 (`analyzer.py`)
- **문제**: transcript API 실패 시에도 프롬프트가 YouTube면 timestamp를 요구 → 100% hallucination
- **수정**: `timestamp_instruction`을 동적 생성
  - transcript 있음 → "실제 자막 시간만 사용"
  - transcript 없음 → "모든 timestamp는 null" (명시적 금지)
  - YouTube 아님 → "null"

### 6. 적용 아이디어 구체화 (`analyzer.py`)
- **문제**: "파이프라인 개선", "모니터링 도입" 같은 generic 아이디어 반복 생성
- **수정**: 3가지 조건 추가
  - 콘텐츠에서 언급된 구체적 기법/도구/알고리즘을 직접 인용할 것
  - 일반적 문장 금지
  - 구체적 기술명·지표·시나리오 포함 필수

### 7. GitHub Actions YouTube 차단 우회 (`collectors.py`, `daily_digest.yml`)
- **문제**: GitHub Actions의 datacenter IP가 YouTube에서 봇으로 차단됨
- **수정**: `YOUTUBE_COOKIES` 환경변수 지원 추가
  - Netscape 형식 쿠키 파일을 base64 인코딩 → GitHub Secret에 저장
  - `requests.Session` + `MozillaCookieJar`로 쿠키 주입 후 `http_client`로 전달
  - 쿠키 미설정 시 익명 요청으로 graceful fallback
  - **결과**: 쿠키로도 IP 차단은 우회 불가 (cloud IP 자체 차단) → description fallback으로 대응

### 8. YouTube description fallback (`collectors.py`)
- **문제**: transcript API 차단 시 YouTube 아이템의 `body`가 None → LLM이 제목만 보고 분석
- **수정**: `_extract_yt_description(entry)` 추가
  - RSS 피드 `entry.summary`에서 영상 설명글 추출, HTML 태그 제거, 3000자 truncate
  - 챕터 마커(`00:00 Intro`) 포함 시 타임라인 정보도 LLM에 전달
  - `has_body=True/False` 로깅 추가

### 9. ArXiv 논문 수집 (`collectors.py`, `config.py`)
- **추가**: `fetch_arxiv_recent(categories, hours=48)`
  - ArXiv Atom API로 `cs.LG`, `stat.ML` 카테고리 최신 논문 수집
  - abstract 전문을 body로 사용 → 분석 품질 높음
  - `ARXIV_CATEGORIES` 환경변수로 커스터마이징 가능

### 10. HackerNews 스토리 수집 (`collectors.py`, `config.py`)
- **추가**: `fetch_hackernews_recent(keywords, hours=24, min_score=50)`
  - Algolia HN API로 키워드 기반 스토리 수집
  - 점수 50+ 필터로 커뮤니티 검증된 콘텐츠만 포함
  - URL 기준 중복 제거, `HACKERNEWS_KEYWORDS` / `HACKERNEWS_MIN_SCORE` 설정 가능

### 11. Supabase 연동 및 중복 제거 실 검증
- Supabase 프로젝트 신규 생성 + `supabase_schema.sql` 실행
- `SUPABASE_URL`, `SUPABASE_KEY` GitHub Secrets 등록
- 실 동작 확인: `dedup_bulk_check already_seen=0 total=5` → 정상 수집 및 발송 확인

---

## 다음 스텝 아이디에이션

### 🔴 버그/안정성 (즉시 가치)

**A. 피드백 루프 실제 작동 검증**
현재 Telegram 👍/👎 버튼이 `user_profile`에 반영되는 경로가 polling 기반(하루 1회)이라 실제로 반영되고 있는지 불명확. liked/disliked 아이템이 다음날 분석에 영향을 주는지 end-to-end 테스트 필요.

**B. seen_urls 만료 정책 부재**
현재 seen_urls는 영구 보존. 같은 영상이 6개월 뒤 다시 올라와도 차단됨. `seen_at` 기준 N일 이후 만료 처리 필요 (Supabase scheduled function 또는 pipeline 시작 시 cleanup).

**C. 발송 실패 시 재시도 없음**
Telegram/Email 발송 실패 시 그냥 넘어감. 최소한 발송 실패 시 로그 알림(Telegram 자체로 에러 메시지)이라도 있어야 운영 가능.

---

### 🟡 품질 개선 (UX 임팩트)

**D. 관련도 점수 보정: 콘텐츠 간 상대 비교**
현재 각 아이템을 개별로 채점 → 절대 기준이 흔들림. 수집된 후보군 전체를 한 번에 넘겨서 "이 중에서 순위를 매겨라" 방식으로 변경하면 점수 분산이 강제됨. 단, 프롬프트 토큰이 대폭 증가하는 트레이드오프 있음.

**E. 요약 품질: one_line_summary가 제목 paraphrasing 수준**
"왜 이게 중요한가"를 담지 못하고 제목을 한국어로 바꾸는 수준. 프롬프트에 "제목을 반복하지 말고 이 콘텐츠가 DS 현업자에게 왜 중요한지 한 줄로" 조건 추가.

**F. 태그 일관성 부재**
같은 개념이 "MLOps", "ml-ops", "ML 운영" 등 다양한 형태로 생성됨. 허용 태그 목록(taxonomy)을 프롬프트에 포함하거나, 생성 후 유사도 기반 normalization.

---

### 🟢 기능 확장 (프로덕션 레벨)

**G. 아카이브 웹 뷰어**
현재 `data/archive/digest_YYYY-MM-DD.html`이 로컬에만 쌓임. Supabase Storage나 GitHub Pages에 자동 업로드하면 언제든 과거 다이제스트 조회 가능. 피드백 링크도 웹 기반으로 전환 가능.

~~**H. 소스 다양화: arXiv / HackerNews**~~ ✅ — `fetch_arxiv_recent`, `fetch_hackernews_recent` 추가 완료

**I. 멀티 유저 지원 구조**
현재 `user_id="default"` 하드코딩. `user_id`를 Telegram chat_id로 바꾸면 Telegram으로 구독 신청한 사람마다 독립적인 프로필·발송이 가능. 스키마 변경은 최소 (seen_urls에 user_id 컬럼 추가).

**J. 비용 추적**
Gemini API 호출 토큰 수를 로깅해두지 않아 실제 사용량 불투명. `response` 객체에서 `usageMetadata` 파싱 → Supabase에 일별 토큰 기록. 무료 tier 초과 예측 가능.

---

### 💡 실험적 아이디어

**K. 주 1회 "테마 다이제스트"**
일별 큐레이션과 별개로, 한 주 동안 수집된 아이템을 클러스터링해서 "이번 주 DS 트렌드 Top 3 테마" 형태로 주말에 발송. Gemini로 클러스터 라벨링 + 대표 아이템 선정.

**L. 퀴즈 정답률 트래킹**
Telegram 스포일러를 탭했을 때 실제로 맞혔는지 확인하는 버튼(맞음/틀림) 추가. 정답률 데이터가 쌓이면 "자주 틀리는 개념" 기반으로 복습 콘텐츠 우선 추천 가능.

**M. 콘텐츠 신선도 가중치**
현재 relevance_score만으로 정렬. 발행일이 오래될수록 패널티를 주는 freshness score 결합 (`final_score = relevance * 0.7 + freshness * 0.3`). 48h RSS 필터와 별개로 "며칠 됐냐"를 점수에 반영.

---

## 우선순위 제안

프로덕션 배포를 목표로 한다면 아래 순서를 추천:

1. ~~**C (발송 실패 알림)**~~ ✅ — `_send_error_alert()` 추가, 수집 0건·필터 전부·채널 실패 3개 지점에서 호출
2. ~~**B (seen_urls 30일 만료)**~~ ✅ — `cleanup_seen_urls(days=30)` 추가, 파이프라인 시작 시 자동 실행
3. ~~**A (피드백 루프 E2E 테스트)**~~ ✅ — `tests/test_feedback_loop.py` 10개 테스트 전부 통과
4. ~~**E (요약 품질 — 제목 반복 금지)**~~ ✅ — `one_line_summary` 프롬프트에 금지 조건 + 예시 추가
5. **G (아카이브 웹 뷰어)** — 외부에 보여줄 수 있는 결과물
6. **D (관련도 점수 — 상대 비교 방식)** — 점수 분산 추가 개선
7. **F (태그 taxonomy)** — 태그 일관성 확보
8. **I (멀티 유저)** — 프로덕션 스케일
