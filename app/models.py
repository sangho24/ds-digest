import re

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from enum import Enum

logger = structlog.get_logger()

# LLM이 새 분류값을 자유 생성하지 못하도록 코드에서 검증하는 닫힌 어휘다.
DOMAINS = [
    "ai-ml",
    "data-eng",
    "software-eng",
    "systems",
    "product",
    "business",
    "research-method",
    "career",
    "tools",
]
CONTENT_TYPES = [
    "paper",
    "case-study",
    "tutorial",
    "talk",
    "news",
    "interview",
    "opinion",
    "release",
]
HALF_LIVES = ["ephemeral", "seasonal", "durable", "foundational"]

# 선지 앞에 붙는 라벨: "A. " "A) " "(A) " "1. " "1) " "가. " 등
# 한글은 열거 기호로 실제 쓰이는 글자만 포함한다. [가-힣] 전체를 넣으면
# "네. 맞습니다" 같은 정상 선지의 첫 어절이 잘린다.
_OPTION_LABEL_RE = re.compile(r"^\s*[\(\[]?\s*(?:[A-Za-z]|[0-9]{1,2}|[가나다라마바사])\s*[\.\)\]]\s+")


class SourceType(str, Enum):
    YOUTUBE = "youtube"
    RSS = "rss"
    NEWSLETTER = "newsletter"


class EvidenceLevel(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    DESCRIPTION = "description"
    TITLE_ONLY = "title_only"


EVIDENCE_DEPTH_CAPS = {
    EvidenceLevel.FULL: 10,
    EvidenceLevel.PARTIAL: 6,
    EvidenceLevel.DESCRIPTION: 3,
    EvidenceLevel.TITLE_ONLY: 1,
}


class RawContent(BaseModel):
    """수집된 원본 콘텐츠"""
    source_type: SourceType
    source_name: str  # 채널명 or 블로그명 (하위호환용 — 기존 독자 유지)
    # 불변 식별자: channel_id·피드 URL·netloc 등 표시명이 바뀌어도 안정적인 키.
    # 그룹핑(_cap_per_channel)·소스 도달률 계측(source_reach)이 이 값을 우선 사용한다.
    source_key: str = ""
    # 사람이 읽는 표시명. 비면 아래 validator가 source_name으로 채운다.
    source_label: str = ""
    title: str
    url: str
    published_at: datetime | None = None
    transcript: str | None = None  # YouTube 자막
    body: str | None = None  # 아티클 본문
    duration_seconds: int | None = None  # 영상 길이

    @model_validator(mode="after")
    def backfill_source_identity(self) -> "RawContent":
        """source_key/source_label가 비면 source_name으로 채워 기존 독자를 깨지 않는다."""
        if not self.source_label:
            self.source_label = self.source_name
        if not self.source_key:
            self.source_key = self.source_label or self.source_name
        return self


class KeyPoint(BaseModel):
    """핵심 포인트 + 타임스탬프"""
    point: str
    timestamp: str | None = None  # "12:34" 형태, 영상인 경우


class QuizItem(BaseModel):
    """내용 확인용 퀴즈"""
    question: str
    options: list[str] = Field(min_length=3, max_length=4)
    answer_index: int
    explanation: str

    @field_validator("options")
    @classmethod
    def strip_option_labels(cls, v: list[str]) -> list[str]:
        """
        선지 앞의 라벨을 제거한다.

        렌더러(HTML 템플릿·Telegram)가 "A) "를 직접 붙이는데 LLM이 반환한
        문자열에도 "A. "가 들어 있어 "A) A. 내용" 같은 이중 라벨이 발생했다.
        프롬프트로 금지해도 새지 않는다는 것이 확인되어(247건 전수) 파싱 시점에 제거한다.
        """
        return [_OPTION_LABEL_RE.sub("", opt).strip() for opt in v]


class ContentAnalysis(BaseModel):
    """Claude가 분석한 결과"""
    relevance_score: int = Field(ge=0, le=10, description="DS 현업 관련도 1-10")
    one_line_summary: str
    tags: list[str] = Field(default_factory=list, max_length=5, description="관련 기술/분야 태그 (예: MLOps, A/B testing, Kubernetes)")
    key_points: list[KeyPoint] = Field(default_factory=list, max_length=5)
    production_ideas: list[str] = Field(default_factory=list, max_length=3)
    quiz: list[QuizItem] = Field(default_factory=list, max_length=3)
    skip_reason: str | None = None
    evidence_level: EvidenceLevel = EvidenceLevel.TITLE_ONLY
    domain: list[str] = Field(default_factory=list, max_length=2)
    content_type: str = "news"
    half_life: str = "seasonal"
    actionability: int = Field(default=0, ge=0, le=10)
    # 상한은 아래 model_validator가 근거 수준별로 clamp한다.
    depth: int = Field(default=0, ge=0)

    @field_validator("domain", mode="before")
    @classmethod
    def validate_domain(cls, value: object) -> list[str]:
        """닫힌 어휘만 남기고, 유효한 도메인은 최대 두 개만 보존한다."""
        if value is None:
            candidates: list[object] = []
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            candidates = [value]

        valid = [item for item in candidates if isinstance(item, str) and item in DOMAINS]
        invalid = [item for item in candidates if item not in DOMAINS]
        if invalid:
            logger.warning("invalid_domain_removed", values=invalid)
        if len(valid) > 2:
            logger.warning("domain_truncated", values=valid[2:])
        return valid[:2]

    @field_validator("content_type", mode="before")
    @classmethod
    def validate_content_type(cls, value: object) -> str:
        """잘못된 콘텐츠 유형은 하위 호환 기본값인 news로 복구한다."""
        if value not in CONTENT_TYPES:
            logger.warning("invalid_content_type_replaced", value=value, fallback="news")
            return "news"
        return value

    @field_validator("half_life", mode="before")
    @classmethod
    def validate_half_life(cls, value: object) -> str:
        """잘못된 반감기 분류는 하위 호환 기본값인 seasonal로 복구한다."""
        if value not in HALF_LIVES:
            logger.warning("invalid_half_life_replaced", value=value, fallback="seasonal")
            return "seasonal"
        return value

    @model_validator(mode="after")
    def enforce_evidence_gate(self) -> "ContentAnalysis":
        """근거 수준에 따라 깊이와 근거 의존 산출물을 코드에서 강제한다."""
        depth_cap = EVIDENCE_DEPTH_CAPS[self.evidence_level]
        if self.depth > depth_cap:
            logger.warning(
                "depth_clamped_by_evidence",
                evidence_level=self.evidence_level.value,
                original_depth=self.depth,
                clamped_depth=depth_cap,
            )
            self.depth = depth_cap

        if self.evidence_level in {EvidenceLevel.DESCRIPTION, EvidenceLevel.TITLE_ONLY}:
            if self.quiz or self.production_ideas:
                logger.warning(
                    "unsupported_outputs_removed",
                    evidence_level=self.evidence_level.value,
                    quiz_count=len(self.quiz),
                    production_idea_count=len(self.production_ideas),
                )
            self.quiz = []
            self.production_ideas = []

        return self


class DigestItem(BaseModel):
    """뉴스레터에 포함될 최종 아이템"""
    raw: RawContent
    analysis: ContentAnalysis


class UserProfile(BaseModel):
    """사용자 선호도 프로필 (피드백으로 업데이트)"""
    user_id: str = "default"
    preferred_topics: list[str] = Field(
        default_factory=lambda: ["data science", "MLOps", "A/B testing", "causal inference"]
    )
    liked_item_ids: list[str] = Field(default_factory=list)
    disliked_item_ids: list[str] = Field(default_factory=list)
    keyword_requests: list[str] = Field(default_factory=list)  # 사용자가 요청한 키워드
    updated_at: datetime = Field(default_factory=datetime.now)


class FeedbackPayload(BaseModel):
    """뉴스레터에서 들어오는 피드백"""
    user_id: str = "default"
    # keyword_request는 특정 아이템에 귀속되지 않으므로 기본값 필요.
    # (필수로 두면 /keyword 처리 시 ValidationError → 같은 배치의 like/dislike까지 중단됨)
    item_url: str = ""
    action: str  # "like" | "dislike" | "keyword_request"
    keyword: str | None = None  # action이 keyword_request일 때
