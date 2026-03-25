# DS Digest 🗞️

**Data Science 현업자를 위한 개인화 큐레이션 뉴스레터**

매일 아침 YouTube, 블로그, 뉴스레터에서 DS 관련 콘텐츠를 수집 → 필터링 → 분석하여
핵심 요약 + 현업 적용 아이디어 + 퀴즈를 이메일로 전달합니다.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│   Collect    │────▶│    Filter    │────▶│   Analyze    │
│ YT/RSS/API   │     │ Claude score │     │ Summary/Quiz │
└─────────────┘     └──────────────┘     └──────┬───────┘
       ▲                                         │
       │            ┌──────────────┐     ┌───────▼──────┐
       └────────────│   Profile    │◀────│   Deliver    │
        preference  │  (Supabase)  │     │  (Resend)    │
        update      └──────────────┘     └──────┬───────┘
                                                │
                                         ┌──────▼───────┐
                                         │   Feedback   │
                                         │  👍/👎/keyword │
                                         └──────────────┘
```

## Quick Start

```bash
# 1. 환경 설정
cp .env.example .env
# .env에 API 키 입력

# 2. 의존성 설치
pip install -r requirements.txt

# 3. 수집 + 분석 (수동 실행)
python -m app.jobs.daily_digest

# 4. API 서버 (피드백 수신용)
uvicorn app.main:app --reload
```

## Tech Stack

| Component   | Tool            | Why                              |
|-------------|-----------------|----------------------------------|
| Framework   | FastAPI         | Webhook + API, async 지원         |
| AI          | Claude API      | Sonnet for filtering & analysis  |
| DB          | Supabase        | Postgres + Auth + REST, 무료 티어  |
| Email       | Resend          | HTML 뉴스레터, 월 3,000통 무료      |
| Transcript  | youtube-transcript-api | 무료, API key 불필요        |
| Scheduler   | GitHub Actions  | cron, 무료 2,000분/월             |
| Deploy      | Railway/Render  | FastAPI 서버 호스팅               |

## MVP Scope

- **Phase 1**: 수집 + 분석 파이프라인 (CLI로 실행)
- **Phase 2**: 이메일 뉴스레터 발송 (Resend)
- **Phase 3**: 피드백 수집 + 프로필 반영
- **Phase 4**: 멀티유저 + 인턴 배포
