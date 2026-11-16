-- 발송 실험 구분, 원문 클릭 기록, 가입 경로와 동의 시각
--
-- 앞의 마이그레이션(20261116000000_subscribers.sql) 뒤에 적용한다. 방법은 supabase/README.md 를 본다.
-- 여러 번 실행해도 같은 결과가 되도록 썼다(if not exists, 멱등한 권한 문장).

-- 발송 이력에 "어떤 방식으로 고른 글인지"와 "메일 안 몇 번째였는지"를 남긴다.
--   personal: 그 구독자의 관심도 표시를 반영해 고른 글
--   baseline: 개인화 없이 고른 글(비교 기준)
--   shared:   모든 구독자에게 똑같이 나간 글
-- 이 컬럼이 생기기 전에 쌓인 행은 둘 다 null 로 남는다.
alter table sent_items
  add column if not exists arm text check (arm in ('personal', 'baseline', 'shared'));
alter table sent_items
  add column if not exists position int;

-- 원문 링크 클릭 기록. 지우거나 고치지 않고 쌓기만 한다.
-- 발송 이력(sent_items)에 있는 글만 Edge Function 이 기록한다.
create table if not exists click_events (
  id bigserial primary key,
  subscriber_id uuid not null references subscribers(id) on delete cascade,
  item_url text not null,
  digest_date date,
  created_at timestamptz default now()
);

-- "이 구독자가 이 글을 눌렀는가"와 10분 중복 판정 조회용.
create index if not exists idx_click_events_subscriber_item
  on click_events (subscriber_id, item_url);

-- 가입 경로와 동의 시각.
--   manual: 운영자가 직접 넣은 구독자(기존 행은 전부 이 값이 된다)
--   invite: 가입 페이지(join.html)에서 초대 코드로 들어온 구독자
-- consented_at 은 가입 페이지에서 동의에 체크한 시각이다. 운영자가 직접 넣은 행은 null.
alter table subscribers
  add column if not exists signup_source text not null default 'manual'
    check (signup_source in ('manual', 'invite'));
alter table subscribers
  add column if not exists consented_at timestamptz;

-- 기존 세 테이블과 같은 방식으로 잠근다.
-- RLS 를 켜고 정책을 두지 않으면 anon, authenticated 역할은 어떤 행도 읽거나 쓰지 못한다.
alter table click_events enable row level security;

-- 이중 잠금: RLS 가 실수로 꺼져도 공개 키로는 닿지 못하게 테이블 권한 자체를 거둔다.
revoke all on table click_events from anon, authenticated;
revoke all on sequence click_events_id_seq from anon, authenticated;

-- service_role 권한을 명시한다(프로젝트의 기본 권한 설정이 달라도 동작하게).
grant all on table click_events to service_role;
grant usage, select on sequence click_events_id_seq to service_role;
