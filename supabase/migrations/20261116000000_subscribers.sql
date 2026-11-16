-- 지인 초대제 구독자, 관심도 표시, 발송 이력 테이블
--
-- 적용 방법은 supabase/README.md 를 본다.
-- 여러 번 실행해도 같은 결과가 되도록 썼다(if not exists, 멱등한 권한 문장).
-- 기존 테이블(seen_urls, feedback, user_profile, digest_records)은 건드리지 않는다.

-- 구독자. token 은 메일 안 링크에 들어가는 비밀값이라 추측할 수 없게 길게 만든다.
create table if not exists subscribers (
  id uuid primary key default gen_random_uuid(),
  email text unique not null,
  name text,
  status text not null default 'active' check (status in ('active', 'unsubscribed')),
  token text unique not null,
  is_owner boolean not null default false,
  created_at timestamptz default now(),
  unsubscribed_at timestamptz
);

-- 관심도 표시 기록. 지우거나 고치지 않고 쌓기만 한다(같은 글의 최신 행이 현재 의사).
create table if not exists feedback_events (
  id bigserial primary key,
  subscriber_id uuid not null references subscribers(id) on delete cascade,
  item_url text not null,
  action text not null check (action in ('want', 'skip')),
  digest_date date,
  created_at timestamptz default now()
);

-- "이 구독자가 이 글에 남긴 가장 최근 표시" 조회용(60초 중복 판정과 선정 되먹임이 쓴다).
create index if not exists idx_feedback_events_subscriber_item_created
  on feedback_events (subscriber_id, item_url, created_at desc);

-- 구독자별 발송 이력(같은 글을 같은 사람에게 두 번 보내지 않기 위함).
create table if not exists sent_items (
  subscriber_id uuid references subscribers(id) on delete cascade,
  item_url text,
  digest_date date,
  primary key (subscriber_id, item_url)
);

-- RLS 를 켜고 정책을 하나도 두지 않는다.
-- 정책이 없으면 anon, authenticated 역할은 어떤 행도 읽거나 쓰지 못한다.
-- service_role 은 RLS 를 우회하므로 Edge Function 과 파이프라인만 접근한다.
alter table subscribers enable row level security;
alter table feedback_events enable row level security;
alter table sent_items enable row level security;

-- 이중 잠금: RLS 가 실수로 꺼져도 공개 키로는 닿지 못하게 테이블 권한 자체를 거둔다.
revoke all on table subscribers from anon, authenticated;
revoke all on table feedback_events from anon, authenticated;
revoke all on table sent_items from anon, authenticated;
revoke all on sequence feedback_events_id_seq from anon, authenticated;

-- service_role 권한을 명시한다(프로젝트의 기본 권한 설정이 달라도 동작하게).
grant all on table subscribers to service_role;
grant all on table feedback_events to service_role;
grant all on table sent_items to service_role;
grant usage, select on sequence feedback_events_id_seq to service_role;
