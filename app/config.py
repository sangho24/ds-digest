from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # AI (Gemini)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # AI (Groq — OpenAI-compatible, 무료 30 RPM / 14,400 RPD)
    # GROQ_API_KEY 설정 시 Gemini 대신 Groq 사용
    groq_api_key: str = ""
    # qwen/qwen3-32b는 2026-07-17 셧다운되어 404를 반환한다(2026-06-17 폐기 공지).
    # 그 여파로 다이제스트가 3일간 발행되지 못했다.
    # Groq가 공식 지정한 마이그레이션 대상이 gpt-oss-120b이며, Production 등급이라
    # Preview 등급(예: qwen3.6-27b)보다 폐기 위험이 낮다.
    # Qwen 계열은 qwen-2.5-32b -> qwen-qwq-32b -> qwen3-32b 로 세 번 연속 폐기됐다.
    groq_model: str = "openai/gpt-oss-120b"

    # Database
    supabase_url: str = ""
    supabase_key: str = ""

    # Email
    resend_api_key: str = ""
    email_from: str = "digest@yourdomain.com"
    email_to: str = "you@example.com"

    # App (피드백 링크 기반 URL — 배포 후 Railway/Render URL로 교체)
    base_url: str = "http://localhost:8000"

    # Content Sources
    youtube_channels: str = ""
    rss_feeds: str = ""

    # ArXiv
    arxiv_categories: str = "cs.LG,stat.ML"

    # HackerNews
    hackernews_keywords: str = "machine learning,MLOps,data science,LLM"
    hackernews_min_score: int = 50

    # Settings
    max_items_per_digest: int = 5
    relevance_threshold: int = 7
    log_level: str = "INFO"

    # seen_urls 보존 기간. 이 값이 짧으면 만료된 URL이 재수집되어 중복 발송된다.
    # 30일이었을 때 전체 아이템의 19.8%가 30~37일 주기로 재유입되었다.
    seen_url_ttl_days: int = 365

    # YouTube 수집 설정
    yt_fetch_per_channel: int = 10   # 채널당 최근 N개 가져오기
    yt_new_per_channel: int = 3      # dedup 후 채널당 최대 분석 대상

    # YouTube 자막 Gemini 폴백
    # datacenter IP 차단으로 youtube-transcript-api 성공률이 0.9%(116건 중 1건)라
    # 기본 활성화한다. GEMINI_API_KEY가 없거나 DRY_RUN이면 자동으로 건너뛴다.
    yt_gemini_transcript: bool = True
    # 2시간 이상 영상은 1M 토큰을 초과해 400으로 실패하므로 그 앞에서 사전 차단한다.
    yt_gemini_max_duration_seconds: int = 6000  # 100분

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # 배송 채널: "telegram" | "email" | "telegram,email"
    delivery_channels: str = "telegram"

    # Dry-run: 외부 API 호출 없이 파이프라인 전체 흐름 검증
    dry_run: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def youtube_channel_list(self) -> list[str]:
        return [c.strip() for c in self.youtube_channels.split(",") if c.strip()]

    @property
    def rss_feed_list(self) -> list[str]:
        return [f.strip() for f in self.rss_feeds.split(",") if f.strip()]

    @property
    def arxiv_category_list(self) -> list[str]:
        return [c.strip() for c in self.arxiv_categories.split(",") if c.strip()]

    @property
    def hackernews_keyword_list(self) -> list[str]:
        return [k.strip() for k in self.hackernews_keywords.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
