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

    # 메타데이터 랭킹 전용 모델. gpt-oss 계열은 정수 인덱스 배열 출력을 뭉개므로
    # (실측: 범위 밖 인덱스·숫자 이어붙임) 구조화 출력이 안정적인 llama를 쓴다.
    # 분석은 groq_model(gpt-oss-120b) 그대로. 더 가볍게 가려면
    # llama-3.1-8b-instant도 동일하게 유효.
    groq_ranking_model: str = "llama-3.3-70b-versatile"

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
    # 초장편만 사전 차단한다. 임계값이 낮으면 정상 강의(60~90분)까지 막힌다.
    # 주의: YouTube RSS(videos.xml)에는 duration이 없어 이 사전 차단은 대부분
    #   동작하지 않고, 실제로는 2시간+ 영상이 Gemini 400을 맞고 None으로 흡수된다.
    #   따라서 임계값을 넉넉히(4시간) 잡아 정상 영상을 오차단하지 않는 데 목적을 둔다.
    #   장편 필터가 꼭 필요해지면 YouTube Data API videos.list(contentDetails.duration,
    #   datacenter IP 면역)로 duration을 채우는 것이 정공법이다.
    yt_gemini_max_duration_seconds: int = 14400  # 4시간

    # 런당 Gemini 전사 최대 건수. Groq가 랭킹·분석을 담당하므로 Gemini는 전사에만
    # 쓰이고, 이 캡이 곧 Gemini 무료 토큰 사용량 상한이다. 상위 N건만 전사하고
    # 나머지는 설명글(description)로 남긴다.
    yt_transcript_budget: int = 5

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
