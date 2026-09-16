# DS Digest — 수집 소스 가이드

Claude Code에게 이 파일을 전달하세요. 소스 목록, 티어 구조, 중복 방지 로직을 포함합니다.

---

## 현재 GitHub secrets 에 설정된 소스 (2026-09-15 동기화)

운영 설정의 정본은 GitHub secrets `YOUTUBE_CHANNELS`, `RSS_FEEDS` 다. 아래는 그 값의 사본이다(Tier 1+2).

```properties
YOUTUBE_CHANNELS=UCN4ZJl6nQpIBo-eRJZom4xA,UC-mOekGSesms0agFntnQang,UCdQF7F6hwjSpulj_fwB9iDQ,UCNrehnUq7Il-J7HQxrzp7CA,UCbfYPyITQ-7l4upoX8nvctg,UCYO_jab_esuFRV4b17AJtAw,UCMLtBahI5DMrt0NPvDSoIRQ
RSS_FEEDS=https://toss.tech/rss.xml,https://medium.com/feed/daangn,https://tech.kakao.com/blog/feed/,https://d2.naver.com/d2.atom,https://netflixtechblog.com/feed,https://medium.com/feed/airbnb-engineering,https://engineering.atspotify.com/feed/
```

### 2026-09-15 정리 내역

| 소스 | 이전 값 | 조치 | 근거 |
|---|---|---|---|
| SLASH - 토스 (YouTube) | `UC-ooeEMOToyByAC1WOQE8ew` | 교체: 토스 챌린저스 `UCN4ZJl6nQpIBo-eRJZom4xA` | 채널 삭제. 토스는 SLASH 대신 TMC(토스 메이커스 컨퍼런스)를 열고 발표 다시보기를 토스 챌린저스 채널에 올린다 |
| if(kakao) (YouTube) | `UCwKk-EEF0gmsHJ5z3CVnRzA` | 교체: kakao tech `UCdQF7F6hwjSpulj_fwB9iDQ` | 채널 삭제. if(kakao)26 공식 FAQ: 세션별 VOD 는 @kakaotech 채널에 공개 |
| Yannic Kilcher (YouTube) | `UCZHmQk67mSJgfCCTn7xBfew` | 제거 | 마지막 업로드 2026-03-06, 190일 넘게 휴면 |
| The Batch (RSS) | `https://www.deeplearning.ai/the-batch/feed/` | 제거 | 피드 404, 사이트에 대체 RSS 없음(뉴스레터 자체는 발행 중) |
| Data Elixir (RSS) | `https://dataelixir.com/issues.rss` | 제거 | 피드 운영 중단 공지, `/feed/` 최신 글 2020-11 |
| Uber Engineering (RSS) | `https://www.uber.com/blog/engineering/rss/` | 제거 | 피드 406/404, 블로그는 발행 중이나 RSS 경로 없음 |

---

## Tier별 소스 목록

### Tier 1 — Core (MVP, 매일 수집)

DS 현업에 직접 도움. 바로 .env에 넣어서 사용.

**YouTube 채널:**

| 채널명 | Channel ID | DS 관련 콘텐츠 | 업로드 주기 |
|---|---|---|---|
| 토스 챌린저스 | `UCN4ZJl6nQpIBo-eRJZom4xA` | TMC(구 SLASH) 발표 다시보기, 테크톡톡(ML 엔지니어링) | 비정기 (컨퍼런스 후 집중, 채용 영상 섞임) |
| 우아한테크 (배민) | `UC-mOekGSesms0agFntnQang` | 데이터 파이프라인, 추천 시스템, 대용량 처리 | 월 2-3회 |
| kakao tech | `UCdQF7F6hwjSpulj_fwB9iDQ` | if(kakao) 세션 VOD: 검색/추천, NLP, 대규모 ML 인프라 | 비정기 (if(kakao) 이후 집중) |
| NAVER D2 | `UCNrehnUq7Il-J7HQxrzp7CA` | 검색 랭킹, 하이퍼클로바, ML 시스템 | 월 1-2회 |

**RSS 피드:**

| 소스명 | URL | DS 관련 콘텐츠 |
|---|---|---|
| 토스 기술 블로그 | `https://toss.tech/rss.xml` | A/B testing 사례, 데이터 기반 의사결정 |
| 당근 테크 블로그 | `https://medium.com/feed/daangn` | 추천, 검색, ML 서빙, 인과추론 |
| 카카오 기술 블로그 | `https://tech.kakao.com/blog/feed/` | 추천, 검색 랭킹, ML 인프라 |
| NAVER D2 | `https://d2.naver.com/d2.atom` | 검색, ML 시스템, 대규모 서비스 |

### Tier 2 — Watch (안정화 후 추가)

해외 빅테크 사례 + ML 연구 트렌드. Tier 1이 잘 돌아간 뒤 .env에 추가.

**YouTube 채널:**

| 채널명 | Channel ID | DS 관련 콘텐츠 | 업로드 주기 |
|---|---|---|---|
| Two Minute Papers | `UCbfYPyITQ-7l4upoX8nvctg` | 최신 ML/AI 논문 시각적 요약 | 주 2-3회 |
| 3Blue1Brown | `UCYO_jab_esuFRV4b17AJtAw` | 수학/통계 직관 (선형대수, 확률, 신경망) | 월 1-2회 |
| ML Street Talk | `UCMLtBahI5DMrt0NPvDSoIRQ` | ML 연구자 인터뷰, 최신 논문 토론 | 주 1회 |

**RSS 피드:**

| 소스명 | URL | DS 관련 콘텐츠 | 비고 |
|---|---|---|---|
| Netflix Tech Blog | `https://netflixtechblog.com/feed` | A/B testing at scale, 추천, 인과추론 | |
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
YOUTUBE_CHANNELS=UCN4ZJl6nQpIBo-eRJZom4xA,UC-mOekGSesms0agFntnQang,UCdQF7F6hwjSpulj_fwB9iDQ,UCNrehnUq7Il-J7HQxrzp7CA

# RSS (Tier 1)
RSS_FEEDS=https://toss.tech/rss.xml,https://medium.com/feed/daangn,https://tech.kakao.com/blog/feed/,https://d2.naver.com/d2.atom
```

## .env 예시 (Tier 1+2 확장)

```properties
# YouTube (Tier 1+2)
YOUTUBE_CHANNELS=UCN4ZJl6nQpIBo-eRJZom4xA,UC-mOekGSesms0agFntnQang,UCdQF7F6hwjSpulj_fwB9iDQ,UCNrehnUq7Il-J7HQxrzp7CA,UCbfYPyITQ-7l4upoX8nvctg,UCYO_jab_esuFRV4b17AJtAw,UCMLtBahI5DMrt0NPvDSoIRQ

# RSS (Tier 1+2)
RSS_FEEDS=https://toss.tech/rss.xml,https://medium.com/feed/daangn,https://tech.kakao.com/blog/feed/,https://d2.naver.com/d2.atom,https://netflixtechblog.com/feed,https://medium.com/feed/airbnb-engineering,https://engineering.atspotify.com/feed/
```
