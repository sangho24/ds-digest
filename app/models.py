from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


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


class ContentAnalysis(BaseModel):
    """Claude가 분석한 결과"""
    relevance_score: int = Field(ge=0, le=10, description="DS 현업 관련도 1-10")
    one_line_summary: str
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
    item_url: str
    action: str  # "like" | "dislike" | "keyword_request"
    keyword: str | None = None  # action이 keyword_request일 때
