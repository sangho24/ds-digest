from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache

# arXiv 기본 카테고리. 워크플로가 ARXIV_CATEGORIES를 **빈 값으로라도 항상**
# 내려보내므로(시크릿 미설정 시 빈 문자열), 이 기본값이 그냥 필드 기본값이면
# 조용히 덮여 사라진다. 빈 값이 오면 여기로 되돌린다 — 아래 property 참조.
DEFAULT_ARXIV_CATEGORIES = (
    "cs.LG,stat.ML,cs.CL,cs.AI,cs.IR,cs.DB,cs.DC,cs.SE,stat.ME,econ.EM,"
    "stat.AP,cs.HC,cs.SI,cs.CV"
)


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

    # 메타데이터 랭킹 전용 모델.
    # 이력: gpt-oss 계열이 json_object 모드에서 정수 인덱스 배열을 뭉개서
    # (실측: 범위 밖 인덱스·숫자 이어붙임) llama-3.3-70b-versatile을 썼다.
    # 그런데 그 모델이 2026-08-16 셧다운됐다(2026-06-17 공지). 같은 공지에서
    # llama-3.1-8b-instant도 함께 폐기됐으므로 llama 계열엔 탈출구가 없다.
    #
    # 해법은 모델 교체가 아니라 출력 강제다. Groq의 strict 구조화 출력
    # (response_format=json_schema, constrained decoding)은 지원 모델이 정확히
    # gpt-oss 20b/120b 둘뿐이다 — 즉 문제가 있던 그 모델에만 있는 기능이
    # 그 문제를 없앤다. 그래서 분석용과 같은 120b로 합친다. 추적해야 할
    # 폐기 대상도 하나로 준다. (analyzer._RANKING_JSON_SCHEMA 참조)
    groq_ranking_model: str = "openai/gpt-oss-120b"

    # Database
    supabase_url: str = ""
    supabase_key: str = ""
    # 구독자 테이블(subscribers, feedback_events, sent_items)은 RLS 로 anon 접근이
    # 막혀 있어 service role 키로만 읽고 쓴다. 비어 있으면 supabase_key 로 폴백한다
    # (아래 supabase_service_key_effective).
    supabase_service_key: str = ""

    # Email
    resend_api_key: str = ""
    email_from: str = "digest@yourdomain.com"
    email_to: str = "you@example.com"
    # 운영 알림(워크플로 실패, 품질 게이트) 수신자. 비어 있으면 EMAIL_TO 로 폴백한다.
    # 실제로 읽는 곳은 evals/alert.py(os.environ 직접)이고, 여기 필드는 .env 등록용이다.
    # 구독자 주소는 여기에 절대 넣지 않는다.
    alert_email_to: str = ""
    # 발송 경로: "resend"(기본) | "smtp". app/mailer.py 가 읽는다.
    email_provider: str = "resend"
    # SMTP 발송 설정(EMAIL_PROVIDER=smtp 일 때만 쓴다). 587 은 STARTTLS, 465 는 SSL.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # App (피드백 링크 기반 URL — 배포 후 Railway/Render URL로 교체)
    base_url: str = "http://localhost:8000"

    # Content Sources
    youtube_channels: str = ""
    rss_feeds: str = ""
    # Stibee 뉴스레터 대상(쉼표 구분). 비우면 DEFAULT_NEWSLETTERS를 사용한다.
    newsletters: str = ""

    # 링크 기사 본문 fetch 토글. HN 외부 링크·짧은 RSS 요약일 때만 원문을
    # 추가로 가져온다. 테스트/네트워크 절약이 필요하면 False로 끈다.
    fetch_link_body: bool = True

    # ArXiv — DS 현업자에게 닿는 범위로 확장.
    # cs.LG/stat.ML만 보면 모델링 논문에 갇힌다. 실무에서 실제로 쓰이는 건
    # NLP·검색/추천·데이터 시스템·실험설계 쪽이 더 많다.
    #   cs.LG   머신러닝            stat.ML  통계적 머신러닝
    #   cs.CL   자연어처리          cs.AI    인공지능 일반
    #   cs.IR   정보검색·추천       cs.DB    데이터베이스
    #   cs.DC   분산·병렬 처리      cs.SE    소프트웨어 공학
    #   stat.ME 통계 방법론(인과추론·실험설계)
    #   econ.EM 계량경제(인과추론)
    #   stat.AP 통계 적용 사례      cs.HC    HCI(AI 제품·인터페이스)
    #   cs.SI   네트워크·그래프 분석   cs.CV    컴퓨터 비전(멀티모달)
    arxiv_categories: str = DEFAULT_ARXIV_CATEGORIES
    # 런당 arXiv 후보 상한.
    # 라운드로빈이 목록 순서대로 채우므로 **상한이 카테고리 수보다 작으면 뒤쪽
    # 카테고리는 영영 한 건도 못 나온다**. 카테고리 14개면 상한도 그 이상이어야
    # 확장한 의미가 있다.
    arxiv_max_items: int = 16
    # 카테고리 요청 사이 대기(초). arXiv 이용약관은 **3초에 1회, 단일 연결**이다.
    # 지키지 않아도 당장은 200이 오지만, arXiv가 "접근을 제한하거나 차단할 수
    # 있다"고 명시하고 있다. 카테고리 10개를 4초 만에 몰아치면 7배 초과다.
    # 하루 1회 배치라 30초 늘어나는 건 아무 문제가 아니고, 조용히 차단당해
    # 또 5개월을 0건으로 보내는 쪽이 비교할 수 없이 나쁘다.
    arxiv_request_delay: float = 3.0

    # HackerNews
    hackernews_keywords: str = "machine learning,MLOps,data science,LLM"
    hackernews_min_score: int = 50
    # 런당 HN 후보 상한.
    # 없을 때는 키워드 4개 × Algolia 기본 20건 = 최대 80건이 후보 풀에 들어왔다.
    # RSS 블로그 하나가 48시간에 1~3건, YouTube 채널이 3건으로 캡되는 것과 자릿수가
    # 다르다. 그래서 HN이 상위 점수대에 압도적으로 많이 걸렸고, 실측 40일 165건에서
    # 32.3%를 차지했다. 그건 HN이 더 좋아서가 아니라 후보를 훨씬 많이 냈기 때문이다.
    # 점수(points) 상위 N건만 남겨 다른 소스와 같은 급으로 맞춘다.
    hackernews_max_items: int = 10

    # Settings
    max_items_per_digest: int = 5
    # 다이제스트 한 번에 보낼 퀴즈 문항 수 상한.
    # 아이템 5건 × 2문항이면 헤더 1 + 아이템 5 + 퀴즈 10 = 16개 메시지를 연달아
    # 쏜다. 채널이 퀴즈로 뒤덮이고 Discord 레이트리밋에도 걸린다(실측
    # 2026-09-02 발송에서 한 건이 429로 사라졌다). 아이템을 번갈아 채우므로
    # 상한이 걸려도 모든 아이템이 최소 한 문항은 낸다.
    max_quiz_per_digest: int = 5
    # 다양성 정규화 강도. 편중의 원인은 품질이 아니라 **물량**이다 — 후보를 많이
    # 내는 소스가 상위 점수대에 더 많이 걸릴 뿐이다(실측: HN 한 소스가 40일
    # 165건의 32.3%).
    #
    # 하드 상한으로 자르면 "5건 중 2건까지"처럼 딱 떨어지지만 거칠다. 대신
    # 후보 풀에서의 물량에 비례해 **같은 소스를 반복해 뽑을수록 감점**한다.
    # 물량이 평균인 소스는 배수 1, 평균의 5배를 낸 소스는 5배로 감점된다.
    # 물량 이점 없이 정말 좋은 소스라면 여전히 여러 건을 가져갈 수 있다.
    #
    # 0이면 정규화를 끄고 순수 점수순이 된다.
    source_diversity_strength: float = 1.0
    # 그래도 남겨두는 하드 상한(안전판). 정규화가 예상 밖으로 약할 때
    # 한 소스가 다이제스트를 통째로 먹는 것만 막는다.
    max_items_per_source: int = 3
    # 필터링용으로는 폐기됨(상대 랭킹으로 대체됨). RELEVANCE_THRESHOLD GitHub
    # secret이 이 값을 설정하므로 필드는 표시/호환용으로만 유지한다.
    relevance_threshold: int = 7
    # 상대 랭킹의 최소 바닥값. 절대 문턱(relevance_threshold) 대신, 후보를
    # 점수순으로 정렬해 상위 max_items_per_digest건을 뽑되 이 값 미만(명백한
    # 저품질: 제목만 2점 등)만 제외한다. 약한 날에도 가용 후보 중 최선을 발송해
    # 빈 다이제스트를 구조적으로 없앤다.
    relevance_floor: int = 4
    log_level: str = "INFO"

    # seen_urls 보존 기간. 이 값이 짧으면 만료된 URL이 재수집되어 중복 발송된다.
    # 30일이었을 때 전체 아이템의 19.8%가 30~37일 주기로 재유입되었다.
    seen_url_ttl_days: int = 365
    # 실험용 seen 완화 다이얼. 0이면 기존 동작(한 번 발송된 URL은 영구 제외).
    # N > 0이면 **최근 N일 안에 발송된 것만** 중복으로 친다 — 그보다 오래된
    # 것은 다시 후보가 된다. 수집·선정 변경을 실측하려는데 후보가 이미 다
    # 소진돼 몇 건 안 남는 상황을 풀기 위한 것이다.
    #
    # 삭제가 아니라 조회 조건이라 되돌릴 게 없다. seen_urls 행은 그대로 남고,
    # 다음 런에서 0으로 되돌리면 즉시 원래 동작이다.
    seen_recent_days: int = 0

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

    # Discord (hermes 봇 재사용)
    # 게이트웨이 없이 REST만으로 발송·수거가 된다 — 상시 프로세스가 필요 없어
    # GitHub Actions 배치 모델에 그대로 맞는다.
    discord_bot_token: str = ""
    discord_channel_id: str = ""

    # 배송 채널: "discord" | "telegram" | "email" (쉼표 구분)
    # 피드백 수거는 Discord로 한정한다 — 두 채널에서 동시에 받으면 같은 아이템에
    # 대한 신호가 갈리고, 어느 쪽이 정본인지 판정할 근거가 없다.
    delivery_channels: str = "discord"

    # Dry-run: 외부 API 호출 없이 파이프라인 전체 흐름 검증
    dry_run: bool = False

    # 개인화 효과 측정(PROGRESS §57). 구독자 선정에서 개인화 목록 대신 기준 선정(관심도
    # 조정을 끈 것)의 아이템으로 바꿔 넣는 자리 수. 0 이면 바꿔 넣지 않는다.
    # 관심도 모델이 켜져 있고 두 선정이 다른 날에만 쓰인다(app/subscriber_delivery.py).
    effect_holdout_slots: int = 1
    # 실험 대상 자리 하나가 기준 선정 쪽 아이템으로 나갈 확률(0~1). 나머지는 개인화 쪽이
    # 나간다. 구독자, 날짜, 자리 번호로 정해지는 결정적 난수라 재실행해도 결과가 같다.
    effect_holdout_rate: float = 0.5
    # 구독자 메일의 원문 링크를 클릭 기록 페이지(docs/go.html)로 돌릴지.
    # 끄면 원문 주소 그대로 나간다. EMAIL_TO, 웹 아카이브, Discord, Telegram 은 늘 원문이다.
    track_clicks: bool = True

    # 구독자별 요약 문체 다듬기(PROGRESS §59, app/restyle.py). 문체 요청이 있는 사람의 메일
    # 본문에만 적용한다. 끄면 전원이 공용 요약을 받고 LLM 호출이 생기지 않는다.
    restyle_enabled: bool = True
    # 한 번의 실행에서 문체를 다듬어 줄 구독자 수 상한. 한 사람당 LLM 호출 1회라 Groq
    # 8K TPM 에서 1명에 20초 안팎이 든다. 넘는 사람은 공용 요약으로 보내고 경고를 남긴다.
    restyle_max_users_per_run: int = 15

    # LLM 심판(evals/judge.py) 전용 키·설정. 파이프라인은 쓰지 않는다. .env 에 같이 두므로
    # Settings 가 알아야 한다(모르면 extra_forbidden 으로 Settings 생성 자체가 실패한다).
    # extra="ignore" 로 풀지 않는 이유: 오타 난 .env 키를 잡아 주는 보호가 사라진다.
    # judge.py 는 이 값을 os.environ 으로 직접 읽으므로 여기 필드는 등록 목적이다.
    deepseek_api_key: str | None = None
    dashscope_api_key: str | None = None
    judge_deepseek_model: str | None = None
    judge_qwen_model: str | None = None
    judge_deepseek_thinking: str | None = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # 워크플로는 `X: ${{ secrets.X }}` 로 항상 내려보내므로 시크릿이 없으면 빈 문자열이
    # 온다. 빈 값이 기본값을 덮으면 SMTP_PORT 는 int 파싱에서 Settings 생성 자체가
    # 죽고(= 다이제스트 전체 실패), EMAIL_PROVIDER 는 알 수 없는 발송 경로가 된다.
    # 그래서 빈 값은 기본값으로 되돌린다(arxiv_category_list 와 같은 사고의 예방).
    @field_validator("email_provider", mode="before")
    @classmethod
    def _default_email_provider(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "resend"
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("smtp_port", mode="before")
    @classmethod
    def _default_smtp_port(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 587
        return value

    @field_validator("effect_holdout_slots", mode="before")
    @classmethod
    def _default_effect_holdout_slots(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 1
        return value

    @field_validator("effect_holdout_rate", mode="before")
    @classmethod
    def _clamp_effect_holdout_rate(cls, value: object) -> object:
        # 빈 값과 숫자가 아닌 값은 기본값으로, 범위를 벗어난 값은 0~1 로 자른다.
        # 측정용 설정 하나가 Settings 생성(= 다이제스트 전체)을 죽이면 안 된다.
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0.5
        try:
            rate = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5
        if rate != rate:  # NaN
            return 0.5
        return max(0.0, min(1.0, rate))

    @field_validator("track_clicks", mode="before")
    @classmethod
    def _default_track_clicks(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return True
        return value

    @field_validator("restyle_enabled", mode="before")
    @classmethod
    def _default_restyle_enabled(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return True
        return value

    @field_validator("restyle_max_users_per_run", mode="before")
    @classmethod
    def _default_restyle_max_users(cls, value: object) -> object:
        # 빈 값과 숫자가 아닌 값은 기본값으로, 음수는 0(전원 공용 요약)으로 둔다.
        # 부가 기능의 설정 하나가 Settings 생성(= 다이제스트 전체)을 죽이면 안 된다.
        if value is None or (isinstance(value, str) and not value.strip()):
            return 15
        try:
            return max(0, int(value))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return 15

    @property
    def supabase_service_key_effective(self) -> str:
        """구독자 테이블용 키. SUPABASE_SERVICE_KEY 가 없으면 SUPABASE_KEY 로 폴백한다."""
        return self.supabase_service_key.strip() or self.supabase_key

    @property
    def subscribers_enabled(self) -> bool:
        """구독자 발송을 시도할 수 있는 설정인가(Supabase 주소와 키가 있는가)."""
        return bool(self.supabase_url and self.supabase_service_key_effective)

    @property
    def youtube_channel_list(self) -> list[str]:
        return [c.strip() for c in self.youtube_channels.split(",") if c.strip()]

    @property
    def rss_feed_list(self) -> list[str]:
        return [f.strip() for f in self.rss_feeds.split(",") if f.strip()]

    @property
    def newsletter_list(self) -> list[str]:
        return [n.strip() for n in self.newsletters.split(",") if n.strip()]

    @property
    def arxiv_category_list(self) -> list[str]:
        """빈 값이면 코드 기본값으로 되돌린다.

        워크플로가 `ARXIV_CATEGORIES: ${{ secrets.ARXIV_CATEGORIES }}`로 항상
        내려보내는데, 시크릿이 없으면 **빈 문자열**이 env에 들어온다. 그러면
        pydantic이 필드 기본값 대신 ""를 쓰고 카테고리가 0개가 된다 — arXiv가
        통째로 사라지는데 예외는 나지 않는다(이미 5개월을 그렇게 잃었다).

        되돌리는 쪽을 택한 이유: 워크플로에 목록을 하드코딩해두면 config와
        두 벌이 되고, 실제로 그 이중화 때문에 코드에서 14개로 늘린 카테고리가
        워크플로의 낡은 `cs.LG,stat.ML` 문자열에 덮여 있었다.
        """
        parsed = [c.strip() for c in self.arxiv_categories.split(",") if c.strip()]
        return parsed or [c.strip() for c in DEFAULT_ARXIV_CATEGORIES.split(",")]

    @property
    def hackernews_keyword_list(self) -> list[str]:
        return [k.strip() for k in self.hackernews_keywords.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
