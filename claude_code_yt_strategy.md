# DS Digest — YouTube 수집 전략 변경

## 문제

현재 YouTube 수집이 "48시간 이내에 올라온 영상"만 가져오는데, 한국 테크 채널들은 다음과 같은 특성이 있어:

1. 토스 SLASH, if(kakao) 같은 컨퍼런스 채널은 1년에 1-2번 영상을 몰아서 올림
2. 카카오 테크(UCdQF7F6hwjSpulj_fwB9iDQ)는 더 이상 새 영상이 안 올라오지만 기존 영상 중 볼만한 게 많음
3. 토스(UCZTDcq38hQ7PG66SGQO27-w)는 영상이 다 내려간 상태

48시간 필터로는 이런 채널에서 아무것도 수집이 안 됨.

## 변경 요청

YouTube와 RSS의 수집 전략을 분리해줘:

### RSS (블로그/뉴스레터) — 기존 유지
- 시간 기반 필터: `published_at`이 최근 48시간 이내인 것만 수집
- 블로그 글은 발행일이 중요하니까 이게 맞음

### YouTube — dedup 기반으로 변경
- 시간 필터 제거
- 채널별 최근 영상 N개를 가져옴 (기본 N=10, 설정 가능)
- Supabase `seen_urls` 테이블에서 이미 발송한 URL 제거 (dedup)
- 남은 것 중에서 최대 3개만 분석 대상으로 (하루에 너무 많이 안 오게)
- 분석 + 발송 성공하면 mark_seen

이렇게 하면:
- 처음 실행 시: 채널별 최근 10개 중 unseen 3개씩 분석/발송
- 다음 날: 이미 본 건 스킵, 새로 올라온 게 있으면 그것만 분석
- 오래된 채널이라도 첫 실행 시 한 번은 좋은 콘텐츠를 건질 수 있음
- 영상이 다 내려간 채널은 RSS 파싱 시 빈 결과 → 자연스럽게 스킵

### .env에 추가할 설정

```
# YouTube 수집 설정
YT_FETCH_PER_CHANNEL=10      # 채널당 최근 N개 영상 가져오기
YT_NEW_PER_CHANNEL=3         # 채널당 하루 최대 분석 대상 수
```

### 코드 변경 위치

`app/collectors/youtube.py`에서:
- `hours` 파라미터 기반 시간 필터 제거
- 대신 `feed.entries[:fetch_per_channel]`로 최근 N개만 가져오기

`app/jobs/daily_digest.py`에서:
- YouTube 아이템에 대해 dedup 후 채널별 `new_per_channel`개로 제한
- RSS 아이템은 기존 시간 필터 유지

### 채널 목록 업데이트

현재 .env의 채널 ID도 정리해줘:

```properties
# 기존 (문제 있음)
YOUTUBE_CHANNELS=UCZTDcq38hQ7PG66SGQO27-w,UC-mOekGSesms0agFntnQang,UCdQF7F6hwjSpulj_fwB9iDQ,UCNrehnUq7Il-J7HQxrzp7CA

# 변경 (검증된 채널로 교체 + 추가)
YOUTUBE_CHANNELS=UC-mOekGSesms0agFntnQang,UCNrehnUq7Il-J7HQxrzp7CA,UCwKk-EEF0gmsHJ5z3CVnRzA,UCdQF7F6hwjSpulj_fwB9iDQ,UCbfYPyITQ-7l4upoX8nvctg
```

변경 내역:
- UCZTDcq38hQ7PG66SGQO27-w (토스 — 영상 전부 삭제됨) → 제거
- UC-mOekGSesms0agFntnQang (우아한테크) → 유지
- UCdQF7F6hwjSpulj_fwB9iDQ (카카오 테크 — 신규 없지만 기존 영상 가치 있음) → 유지 (dedup 방식이면 처음에 건질 수 있음)
- UCNrehnUq7Il-J7HQxrzp7CA (NAVER D2) → 유지
- UCwKk-EEF0gmsHJ5z3CVnRzA (if(kakao)) → 추가
- UCbfYPyITQ-7l4upoX8nvctg (Two Minute Papers) → 추가 (주 2-3회 업로드로 꾸준함)
