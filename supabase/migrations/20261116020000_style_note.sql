-- 구독자 문체 요청(style_note)
--
-- 앞의 두 마이그레이션 뒤에 적용한다. 방법은 supabase/README.md 를 본다.
-- 여러 번 실행해도 같은 결과가 되도록 썼다(if not exists).

-- 구독자가 가입 페이지(join.html)에서 적은 "요약을 어떻게 받고 싶은지" 한 줄 요청.
--   null: 요청 없음(기본 요약으로 받는다). 기존 행은 전부 null 이 된다.
--   값:   앞뒤 공백과 제어 문자를 뗀 200자 이하의 글(글자 수는 코드 포인트 기준).
-- 빈 문자열은 저장하지 않는다(Edge Function 이 null 로 바꾼다).
-- 이 값은 구독자가 자유롭게 쓴 글이다. 요약 모델에 넘길 때는 지시가 아니라 자료로 다룬다.
-- 컬럼이 이미 있으면 check 를 포함해 아무것도 하지 않는다.
alter table subscribers
  add column if not exists style_note text
    check (style_note is null or char_length(style_note) <= 200);

-- 권한과 RLS 는 테이블 단위라 새로 할 일이 없다(subscribers 는 첫 마이그레이션에서 잠갔다).
