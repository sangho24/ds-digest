# DS Digest — 오늘 완료할 작업

## 목표
내일 아침 7시 30분(KST)에 Telegram + 이메일로 다이제스트가 자동 발송되어야 함.

---

## 작업 1: GitHub Actions cron 시간 수정

현재 daily_digest.yml의 cron이 `'0 23 * * *'` (= KST 08:00)로 되어 있을 텐데,
KST 07:30에 도착하게 하려면 파이프라인 실행 시간(2~5분)을 감안해서:

```yaml
on:
  schedule:
    - cron: '25 22 * * *'   # UTC 22:25 = KST 07:25 → 발송 07:27~30 도착
  workflow_dispatch:
```

## 작업 2: GitHub Actions에 필요한 Secrets 목록 확인

현재 .env에서 사용 중인 환경변수를 전부 확인하고,
GitHub repo → Settings → Secrets and variables → Actions에 등록해야 할 목록을 출력해줘.

최소한 다음이 필요:
- GEMINI_API_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- RESEND_API_KEY
- EMAIL_FROM
- EMAIL_TO
- SUPABASE_URL
- SUPABASE_KEY
- 기타 사용 중인 설정 (YOUTUBE_CHANNELS, RSS_FEEDS, RELEVANCE_THRESHOLD 등)

daily_digest.yml의 env 섹션에 이 시크릿들이 전부 매핑되어 있는지 확인하고, 빠진 게 있으면 추가해줘.

## 작업 3: workflow dispatch로 수동 테스트

GitHub에 push 한 뒤 Actions 탭에서 "Run workflow" 버튼으로 수동 실행해서 성공하는지 확인할 거야.
실패할 수 있는 포인트:
- requirements.txt 설치 실패 (supabase 버전 이슈 등)
- 환경변수 누락
- YouTube transcript API가 GitHub Actions IP에서 차단

각 실패 케이스에 대한 에러 메시지와 대응을 daily_digest.yml에 주석으로 넣어줘.

## 작업 4: Telegram 피드백 E2E 확인

현재 Telegram 인라인 버튼(👍/👎/📝)을 눌렀을 때:
1. callback_data가 어디로 가는지 확인
2. Supabase feedback 테이블에 실제로 row가 생기는지 확인
3. 다음 파이프라인 실행 시 해당 피드백이 필터링 프롬프트에 반영되는지 확인

만약 Telegram callback 처리가 polling 방식이라면, GitHub Actions에서는 파이프라인 실행 시점에만 돌아가니까 피드백은 별도 서버가 필요해.

현실적 해결책:
- 방법 A: FastAPI 서버를 Railway/Render에 배포하고, Telegram webhook을 설정
- 방법 B (MVP): 피드백 polling을 daily_digest 실행 시작 시 한 번 돌리기 — 파이프라인 시작 전에 "어제 들어온 callback을 처리하고 프로필에 반영" → 그 다음 수집/분석/발송

방법 B로 구현해줘. daily_digest.py 시작부에:
```
1. getUpdates로 미처리 callback 수집
2. 각 callback을 feedback 테이블에 저장
3. 프로필 업데이트
4. 이후 일반 파이프라인 실행
```

이러면 별도 서버 없이도 하루 1회 피드백이 반영됨.

## 작업 5: 현재 코드 전체 점검

push 전에 확인:
- .gitignore에 .env, data/, __pycache__/ 포함되어 있는지
- requirements.txt에 모든 의존성이 있는지
- .env.example이 최신 상태인지 (GEMINI_API_KEY 등 반영)

## 완료 기준

1. GitHub repo에 push 완료
2. Actions 탭에서 수동 workflow_dispatch 실행 → 성공 (녹색 체크)
3. Telegram + 이메일 수신 확인
4. Telegram에서 👍 버튼 클릭 → 다음 실행 시 피드백 반영되는 구조 확인
5. cron이 UTC 22:25로 설정되어 내일 KST 07:25에 자동 실행 예약
