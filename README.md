# DS Digest 🗞️

**Data Science 현업자를 위한 개인화 큐레이션 뉴스레터**

매일 아침 YouTube, RSS, ArXiv, HackerNews에서 DS 관련 콘텐츠를 수집 → AI 필터링 → 분석하여
핵심 요약 + 현업 적용 아이디어 + 퀴즈를 이메일/Telegram으로 전달합니다.

## Architecture

```
GitHub Actions (07:10 KST)
        │
        ▼
  collect_all()
  ├── fetch_youtube_recent()     YouTube RSS → description fallback
  ├── fetch_rss_recent()         RSS 피드 (48h 필터)
  ├── fetch_arxiv_recent()       ArXiv cs.LG, stat.ML (48h 필터)
  └── fetch_hackernews_recent()  HN 키워드 스토리 (24h, 점수≥50)
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
        │
        ▼
  docs/ 저장 → git push        GitHub Pages 아카이브 자동 업데이트
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

# 4. API 서버 (피드백 수신 + 아카이브 뷰어)
uvicorn app.main:app --reload
# http://localhost:8000/archive
```

## Tech Stack

| Component   | Tool                       | Notes                              |
|-------------|----------------------------|------------------------------------|
| Framework   | FastAPI                    | Webhook + API + 아카이브 뷰어       |
| AI          | Gemini 2.5 Flash / Groq    | relevance 필터링 + 요약/퀴즈 생성   |
| DB          | Supabase                   | seen_urls 중복 제거, 사용자 프로필   |
| Email       | Resend                     | HTML 뉴스레터, 월 3,000통 무료      |
| Messaging   | Telegram Bot               | 스포일러 퀴즈 + 피드백 버튼          |
| Scheduler   | GitHub Actions (cron)      | 매일 07:10 KST 자동 실행           |
| Archive     | GitHub Pages               | `docs/` 자동 push → 웹 공개        |

## Environment Variables

| 변수 | 필수 | 설명 |
|------|------|------|
| `GEMINI_API_KEY` | ✅ | Google AI Studio |
| `GROQ_API_KEY` | - | Groq (Gemini 대안) |
| `SUPABASE_URL` | ✅ | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | ✅ | Supabase anon key |
| `RESEND_API_KEY` | - | 이메일 발송 |
| `EMAIL_FROM` / `EMAIL_TO` | - | 이메일 주소 |
| `TELEGRAM_BOT_TOKEN` | - | BotFather 발급 |
| `TELEGRAM_CHAT_ID` | - | 수신 채팅 ID |
| `DELIVERY_CHANNELS` | ✅ | `telegram,email` |
| `YOUTUBE_CHANNELS` | - | 채널 ID 쉼표 구분 |
| `RSS_FEEDS` | - | RSS URL 쉼표 구분 |
| `ARXIV_CATEGORIES` | - | 기본값 `cs.LG,stat.ML` |
| `HACKERNEWS_KEYWORDS` | - | 기본값 `machine learning,MLOps,...` |

## Archive Viewer

매일 생성된 다이제스트는 `docs/` 폴더에 저장되어 GitHub Pages로 공개됩니다.

**설정 방법**: 레포 → Settings → Pages → Source: `main` / `/docs` → Save

이후 Actions 실행마다 `https://[유저명].github.io/[레포명]/` 이 자동 업데이트됩니다.

로컬에서는 FastAPI 서버를 통해 `http://localhost:8000/archive` 에서 확인 가능합니다.
