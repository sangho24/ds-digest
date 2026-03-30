-- DS Digest Supabase Schema
-- Supabase 대시보드 > SQL Editor에서 실행하세요.

-- 발송된 URL 추적 (중복 발송 방지)
create table if not exists seen_urls (
    url         text primary key,
    seen_at     timestamptz not null default now()
);

-- 피드백 로그 (좋아요/싫어요/키워드 요청)
create table if not exists feedback (
    id          bigint generated always as identity primary key,
    user_id     text not null default 'default',
    item_url    text not null,
    action      text not null,  -- 'like' | 'dislike' | 'keyword_request' | 'unsubscribe'
    keyword     text,
    created_at  timestamptz not null default now()
);

-- 사용자 프로필 (선호도 + 피드백 반영)
create table if not exists user_profile (
    user_id             text primary key default 'default',
    preferred_topics    jsonb not null default '["data science", "MLOps", "A/B testing", "causal inference"]',
    liked_item_ids      jsonb not null default '[]',
    disliked_item_ids   jsonb not null default '[]',
    keyword_requests    jsonb not null default '[]',
    updated_at          timestamptz not null default now()
);

-- 기본 사용자 프로필 삽입 (없을 경우)
insert into user_profile (user_id) values ('default')
on conflict (user_id) do nothing;

-- 오래된 seen_urls 자동 정리 (30일 초과)
-- 파이프라인 시작 시 cleanup_seen_urls(days=30) 으로 자동 실행됨.
-- 수동 실행이 필요한 경우:
-- delete from seen_urls where seen_at < now() - interval '30 days';
