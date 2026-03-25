# DS Digest — 수집 소스 가이드

Claude Code에게 이 파일을 전달하세요. 소스 목록, 티어 구조, 중복 방지 로직을 포함합니다.

---

## 현재 .env에 설정된 YouTube 채널

```
YOUTUBE_CHANNELS=UCZTDcq38hQ7PG66SGQO27-w,UC-mOekGSesms0agFntnQang,UCdQF7F6hwjSpulj_fwB9iDQ,UCNrehnUq7Il-J7HQxrzp7CA
```

| Channel ID | 추정 채널명 | 확인 방법 |
|---|---|---|
| UCZTDcq38hQ7PG66SGQO27-w | 확인 필요 — SLASH(토스) 또는 다른 채널일 수 있음 | YouTube에서 `youtube.com/channel/UCZTDcq38hQ7PG66SGQO27-w` 접속해서 확인 |
| UC-mOekGSesms0agFntnQang | 우아한테크 (배민) | 이전에 검증됨 |
| UCdQF7F6hwjSpulj_fwB9iDQ | 확인 필요 — if(kakao) 또는 다른 채널일 수 있음 | YouTube에서 `youtube.com/channel/UCdQF7F6hwjSpulj_fwB9iDQ` 접속해서 확인 |
| UCNrehnUq7Il-J7HQxrzp7CA | NAVER D2 | 이전에 검증됨 |

> **TODO**: 본인이 4개 채널 ID를 각각 YouTube에서 열어서 실제 채널명을 확인하고, 아래 Tier 1 목록과 맞는지 대조해주세요.

---

## Tier별 소스 목록

### Tier 1 — Core (MVP, 매일 수집)

DS 현업에 직접 도움. 바로 .env에 넣어서 사용.

**YouTube 채널:**

| 채널명 | Channel ID | DS 관련 콘텐츠 | 업로드 주기 |
|---|---|---|---|
| SLASH - 토스 | `UC-ooeEMOToyByAC1WOQE8ew` | A/B testing, 추천, ML 서빙, 데이터 엔지니어링 | 비정기 (컨퍼런스 시즌 집중) |
| 우아한테크 (배민) | `UC-mOekGSesms0agFntnQang` | 데이터 파이프라인, 추천 시스템, 대용량 처리 | 월 2-3회 |
| if(kakao) | `UCwKk-EEF0gmsHJ5z3CVnRzA` | 검색/추천, NLP, 대규모 ML 인프라 | 비정기 (컨퍼런스 시즌) |
| NAVER D2 | `UCNrehnUq7Il-J7HQxrzp7CA` | 검색 랭킹, 하이퍼클로바, ML 시스템 | 월 1-2회 |

**RSS 피드:**

| 소스명 | URL | DS 관련 콘텐츠 |
|---|---|---|
| 토스 기술 블로그 | `https://toss.tech/rss.xml` | A/B testing 사례, 데이터 기반 의사결정 |
| 당근 테크 블로그 | `https://medium.com/feed/daangn` | 추천, 검색, ML 서빙, 인과추론 |
| 우아한형제들 블로그 | `https://techblog.woowahan.com/feed/` | 데이터 플랫폼, 실험 플랫폼 |
| 카카오 기술 블로그 | `https://tech.kakao.com/blog/feed/` | 추천, 검색 랭킹, ML 인프라 |
| The Batch (Andrew Ng) | `https://www.deeplearning.ai/the-batch/feed/` | AI/ML 주간 트렌드, 실무 적용 관점 |
| Data Elixir | `https://dataelixir.com/issues.rss` | DS 주간 큐레이션, 도구/라이브러리 |

### Tier 2 — Watch (안정화 후 추가)

해외 빅테크 사례 + ML 연구 트렌드. Tier 1이 잘 돌아간 뒤 .env에 추가.

**YouTube 채널:**

| 채널명 | Channel ID | DS 관련 콘텐츠 | 업로드 주기 |
|---|---|---|---|
| Two Minute Papers | `UCbfYPyITQ-7l4upoX8nvctg` | 최신 ML/AI 논문 시각적 요약 | 주 2-3회 |
| Yannic Kilcher | `UCZHmQk67mSJgfCCTn7xBfew` | 논문 딥다이브, ML 연구 트렌드 | 주 1-2회 |
| 3Blue1Brown | `UCYO_jab_esuFRV4b17AJtAw` | 수학/통계 직관 (선형대수, 확률, 신경망) | 월 1-2회 |
| ML Street Talk | `UCMLtBahI5DMrt0NPvDSoIRQ` | ML 연구자 인터뷰, 최신 논문 토론 | 주 1회 |

**RSS 피드:**

| 소스명 | URL | DS 관련 콘텐츠 | 비고 |
|---|---|---|---|
| Netflix Tech Blog | `https://netflixtechblog.com/feed` | A/B testing at scale, 추천, 인과추론 | |
| Uber Engineering | `https://www.uber.com/blog/engineering/rss/` | ML 플랫폼, 수요 예측, 실험 플랫폼 | |
| Airbnb Engineering | `https://medium.com/feed/airbnb-engineering` | 실험 플랫폼, 검색 랭킹, 가격 최적화 | |
| Spotify Engineering | `https://engineering.atspotify.com/feed/` | 추천, 개인화, ML 시스템 | |
| arXiv stat.ML | `https://rss.arxiv.org/rss/stat.ML` | 최신 ML/통계 논문 | 양 많음 — threshold 8+ 권장 |
| arXiv cs.AI | `https://rss.arxiv.org/rss/cs.AI` | 최신 AI 논문 | 양 매우 많음 — 나중에 추가 |

### Tier 3 — Nice to have (품질 편차 큼)

| 소스명 | URL | 비고 |
|---|---|---|
| Towards Data Science | `https://towardsdatascience.com/feed` | Medium 기반, 양 많고 품질 편차. threshold 높게 |
| ML Mastery | `https://machinelearningmastery.com/feed/` | ML 실습 튜토리얼, 코드 예제 |

---

## 추가 추천 채널 (검토 후 Tier 1 또는 2에 편입)

본인 관심사(DS, A/B testing, causal inference, MLOps)에 맞는 추가 후보:

**YouTube:**

| 채널명 | 핸들 | 이유 |
|---|---|---|
| 쿠팡 테크 | 확인 필요 (@coupangengineering) | 추천/검색/물류 ML 발표 |
| 라인 테크 | 확인 필요 | 대규모 추천, NLP |
| SK텔레콤 | 확인 필요 | AI/ML 연구 발표 |
| StatQuest (Josh Starmer) | `@statquest` | 통계/ML 개념 설명 (영어, 매우 직관적) |
| ritvikmath | `@riaborvikmath` | 실무 DS 개념 (인과추론, 베이지안 등) |

**RSS:**

| 소스명 | URL | 이유 |
|---|---|---|
| 쿠팡 기술 블로그 | `https://medium.com/feed/coupang-engineering` | 추천, 검색, 물류 최적화 |
| 뱅크샐러드 기술 블로그 | `https://blog.banksalad.com/feed.xml` | 금융 DS, 개인화 |
| Google AI Blog | `https://blog.research.google/feeds/posts/default` | ML 연구 최신 동향 |
| Meta AI Blog | `https://ai.meta.com/blog/rss/` | 오픈소스 ML (LLaMA 등) |
| Chip Huyen's Blog | `https://huyenchip.com/feed.xml` | MLOps, ML 시스템 설계 |

> **채널 ID 확인법**: YouTube에서 채널 페이지 접속 → 주소창에 `/channel/UC...` 형태로 나오면 그게 ID. `@handle` 형태면 페이지 소스 보기에서 `channel_id`를 검색하거나, https://commentpicker.com/youtube-channel-id.php 에 URL을 넣으면 됨.

---

## 중복 방지 로직 (dedup)

**현재 구현 상태 확인 필요:**

파이프라인이 매일 돌면서 같은 콘텐츠를 반복 분석/발송하면 안 됨.
Claude Code에 아래를 확인/구현하도록 전달:

```
1. 수집 단계 직후, 분석 전에 dedup 실행
2. Supabase의 seen_urls 테이블에서 이미 발송한 URL인지 확인 (is_seen)
3. 이미 본 URL은 스킵
4. 분석 + 발송 성공한 아이템만 mark_seen으로 기록
5. Supabase 연결 실패 시 → fail-open (중복 허용, 미발송 방지)

확인 사항:
- daily_digest.py에 _deduplicate() 함수가 있는지
- 발송 성공 시에만 mark_seen()을 호출하는지 (발송 실패한 건 다음 날 재시도 가능하도록)
- seen_urls 테이블에 first_seen_at 타임스탬프가 있는지 (나중에 "최근 N일 아카이브" 조회용)
```

이미 구현돼 있다면 (`app/db.py`에 is_seen/mark_seen + `daily_digest.py`에 _deduplicate) 이 부분은 확인만 하면 됨.

---

## .env 예시 (Tier 1 전체)

```properties
# YouTube (Tier 1)
YOUTUBE_CHANNELS=UC-ooeEMOToyByAC1WOQE8ew,UC-mOekGSesms0agFntnQang,UCwKk-EEF0gmsHJ5z3CVnRzA,UCNrehnUq7Il-J7HQxrzp7CA

# RSS (Tier 1)
RSS_FEEDS=https://toss.tech/rss.xml,https://medium.com/feed/daangn,https://techblog.woowahan.com/feed/,https://tech.kakao.com/blog/feed/,https://www.deeplearning.ai/the-batch/feed/,https://dataelixir.com/issues.rss
```

## .env 예시 (Tier 1+2 확장)

```properties
# YouTube (Tier 1+2)
YOUTUBE_CHANNELS=UC-ooeEMOToyByAC1WOQE8ew,UC-mOekGSesms0agFntnQang,UCwKk-EEF0gmsHJ5z3CVnRzA,UCNrehnUq7Il-J7HQxrzp7CA,UCbfYPyITQ-7l4upoX8nvctg,UCZHmQk67mSJgfCCTn7xBfew,UCYO_jab_esuFRV4b17AJtAw,UCMLtBahI5DMrt0NPvDSoIRQ

# RSS (Tier 1+2)
RSS_FEEDS=https://toss.tech/rss.xml,https://medium.com/feed/daangn,https://techblog.woowahan.com/feed/,https://tech.kakao.com/blog/feed/,https://www.deeplearning.ai/the-batch/feed/,https://dataelixir.com/issues.rss,https://netflixtechblog.com/feed,https://www.uber.com/blog/engineering/rss/,https://medium.com/feed/airbnb-engineering,https://engineering.atspotify.com/feed/
```
