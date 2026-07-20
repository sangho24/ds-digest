import re

from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from enum import Enum

# 선지 앞에 붙는 라벨: "A. " "A) " "(A) " "1. " "1) " "가. " 등
# 한글은 열거 기호로 실제 쓰이는 글자만 포함한다. [가-힣] 전체를 넣으면
# "네. 맞습니다" 같은 정상 선지의 첫 어절이 잘린다.
_OPTION_LABEL_RE = re.compile(r"^\s*[\(\[]?\s*(?:[A-Za-z]|[0-9]{1,2}|[가나다라마바사])\s*[\.\)\]]\s+")


class SourceType(str, Enum):
    YOUTUBE = "youtube"
    RSS = "rss"
    NEWSLETTER = "newsletter"


class RawContent(BaseModel):
    """수집된 원본 콘텐츠"""
    source_type: SourceType
    source_name: str  # 채널명 or 블로그명
    title: str
    url: str
    published_at: datetime | None = None
    transcript: str | None = None  # YouTube 자막
    body: str | None = None  # 아티클 본문
    duration_seconds: int | None = None  # 영상 길이


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
