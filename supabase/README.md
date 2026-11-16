# supabase: 구독자 가입, 피드백, 클릭, 수신거부

지인 초대제 구독자의 가입 신청, 메일 안 링크로 남기는 관심도 표시(want, skip), 원문 클릭, 수신거부를 받는 쪽이다.

| 경로 | 역할 |
| --- | --- |
| `supabase/config.toml` | CLI 설정. 두 함수의 JWT 검증을 끈다 |
| `supabase/migrations/20261116000000_subscribers.sql` | `subscribers`, `feedback_events`, `sent_items` 테이블 |
| `supabase/migrations/20261116010000_tracking.sql` | `click_events` 테이블, `sent_items.arm`/`position`, `subscribers.signup_source`/`consented_at` |
| `supabase/migrations/20261116020000_style_note.sql` | `subscribers.style_note`(구독자 문체 요청) |
| `supabase/functions/digest-feedback/` | 관심도 표시, 원문 클릭, 수신거부를 받는 함수 |
| `supabase/functions/digest-join/` | 초대 코드로 가입을 받는 함수 |
| `docs/feedback.html` | 관심도 표시와 수신거부 링크가 여는 페이지(GitHub Pages) |
| `docs/go.html` | 원문 링크가 거쳐 가는 페이지. 클릭을 기록한 뒤 원문으로 넘긴다 |
| `docs/join.html` | 가입 페이지 |

두 함수는 같은 구조다. `index.ts` 는 진입점(DB 배선), `handler.ts` 는 요청 처리 흐름, `logic.ts` 는 입력 검증(순수 함수), `*.test.ts` 는 테스트다. CORS 와 서비스 키를 고르는 코드는 두 함수의 `logic.ts` 에 똑같이 들어 있으므로 한쪽을 고치면 다른 쪽도 고친다.

기존 `supabase_schema.sql` 의 테이블(`seen_urls`, `feedback`, `user_profile`, `digest_records`)은 이 작업으로 바뀌지 않는다.

## 동작 흐름

1. 메일 링크: `{PAGES_BASE}/feedback.html?t={token}&a={want|skip|unsubscribe}&u={인코딩한 item_url}&d={YYYY-MM-DD}`
2. 페이지가 같은 디렉터리의 `feedback-config.json` 에서 `endpoint` 를 읽는다. 이 파일은 파이프라인이 만든다. 없으면 페이지는 "아직 설정되지 않았습니다"를 보인다.
3. 페이지의 JS 가 `endpoint` 로 `POST` 한다. 본문은 JSON `{token, action, item_url, digest_date}`.
4. 메일 헤더의 원클릭 수신거부는 메일 서버가 `POST {endpoint}?t={token}&a=unsubscribe` 로 보낸다. 본문은 `List-Unsubscribe=One-Click`.

함수는 `POST` 와 `OPTIONS` 만 처리하고 `GET` 은 405 로 거절한다. 메일 보안 스캐너가 링크를 미리 열어도 아무 일도 일어나지 않게 하려는 것이다. 수신거부는 페이지에서도 확인 버튼을 눌러야 실행된다.

### 원문 클릭

1. 메일의 원문 링크: `{PAGES_BASE}/go.html?t={token}&u={인코딩한 item_url}&d={YYYY-MM-DD}`
2. `go.html` 이 `feedback-config.json` 의 `endpoint`(digest-feedback)로 JSON `{token, action: "click", item_url, digest_date}` 를 `POST` 한다.
3. 함수는 그 구독자의 `sent_items` 에 `item_url` 이 있을 때만 `click_events` 에 한 행을 넣고 `{ok: true}` 를 준다. 주소는 글자 그대로 비교한다. 없으면 404 다.
4. `go.html` 은 `{ok: true}` 를 받았을 때만 `location.replace` 로 원문에 넘어간다. 404, 오류, 2초 시간 초과, 설정 파일 없음이면 넘어가지 않고 가려던 곳의 호스트를 글자로 보여 준 뒤 "원문 열기"를 누르게 한다.

3번과 4번이 짝이다. 보낸 적 없는 주소로는 자동 이동이 일어나지 않으므로, `go.html` 을 아무 주소로나 넘겨 주는 열린 리다이렉트로 쓸 수 없다.

- 해지한 구독자도 예전 메일의 원문 링크는 열린다(`{ok: true}`). 다만 기록은 남기지 않는다.
- 같은 구독자가 같은 글을 10분 안에 다시 누르면 기록을 건너뛴다(응답은 같다).
- 클릭은 JSON `POST` 로만 받는다. 쿼리 파라미터 형태는 수신거부 전용이다.

### 가입

1. 초대 링크: `{PAGES_BASE}/join.html?code={초대 코드}`. 코드 칸이 미리 채워진다. `?code=` 없이 열고 코드를 직접 적어도 된다.
2. `join.html` 은 `feedback-config.json` 의 `endpoint` 끝 `/digest-feedback` 을 `/digest-join` 으로 바꿔 보낼 곳을 얻는다. 설정 파일이 없으면 "아직 열리지 않았습니다"를 보인다.
3. 페이지가 JSON `{email, name, invite_code, consent: true, style_note?}` 를 `POST` 한다. `style_note` 는 "요약을 어떻게 받고 싶은가요?" 칸에 적었을 때만 싣는다.
4. 함수는 초대 코드를 시크릿 `INVITE_CODE` 와 상수 시간으로 비교하고, 정원(`MAX_SUBSCRIBERS`)을 확인한 뒤 구독자를 넣는다.

| 이메일 상태 | 하는 일 | 응답 |
| --- | --- | --- |
| 처음 보는 이메일 | 새 token(32바이트 난수의 base64url, 43자)으로 `status` active, `signup_source` invite, `consented_at` 현재 시각인 행을 넣는다. `style_note` 는 적은 값, 적지 않았으면 null 이다 | 200 `{ok: true}` |
| 해지한 이메일 | `status` 를 active 로 되돌리고 `consented_at` 을 갱신한다. `style_note` 는 이번에 적었을 때만 덮어쓰고, 비워 두었으면 예전 값이 남는다. 이름, token, `signup_source`, `unsubscribed_at` 은 그대로다 | 200 `{ok: true}` |
| 이미 받고 있는 이메일 | 아무것도 바꾸지 않는다(`style_note` 포함) | 200 `{ok: true}` |

세 경우의 응답이 같아서 응답만으로는 어떤 이메일이 가입돼 있는지 알 수 없다. 함수는 `is_owner` 를 읽지도 쓰지도 않는다. token 은 응답에 싣지 않고 다음 발송 메일의 링크로만 전달된다.

이미 받고 있는 이메일의 `style_note` 를 바꾸지 않는 것은 일부러 그런 것이다. 가입할 때 이메일 소유를 확인하지 않으므로, 덮어쓰게 두면 초대 코드를 가진 사람이 남의 요약 문체를 바꿀 수 있다. 받고 있는 구독자가 문체 요청을 바꾸려면 운영자가 직접 고친다("알아 둘 점"의 문체 요청 항목).

## 응답 규칙

### digest-feedback

응답은 항상 JSON `{ok, action?, error?}` 이다.

| 상황 | 상태 | error |
| --- | --- | --- |
| want, skip 반영(60초 안 같은 표시 반복 포함) | 200 | |
| click 기록(10분 안 반복, 해지된 구독자 포함) | 200 | |
| 수신거부(이미 해지된 경우 포함) | 200 | |
| 본문이 JSON 이 아님 | 400 | `invalid_json` |
| action 이 want, skip, click, unsubscribe 가 아님 | 400 | `invalid_action` |
| 쿼리 파라미터 형태로 온 want, skip, click | 400 | `invalid_action` |
| token 누락 | 400 | `missing_token` |
| item_url 누락, http/https 아님, 2048자 초과 | 400 | `invalid_item_url` |
| 허용하지 않은 Origin 에서 온 want, skip, click | 403 | `origin_not_allowed` |
| 없는 token | 404 | `not_found` |
| click 의 item_url 이 그 구독자의 `sent_items` 에 없음 | 404 | `not_found` |
| GET 등 다른 메서드 | 405 | `method_not_allowed` |
| 해지된 구독자의 want, skip | 409 | `unsubscribed` |
| 본문 4096바이트 초과 | 413 | `payload_too_large` |
| DB 오류, 환경변수 누락 | 500 | `server_error` |

`digest_date` 는 실제로 있는 `YYYY-MM-DD` 일 때만 저장하고, 아니면 비워 둔다. 함수는 `email` 컬럼을 읽지 않으며 로그에는 실패한 단계 이름과 DB 오류 코드만 남긴다.

### digest-join

응답은 항상 JSON `{ok, error?, message?}` 이다. 위에서부터 차례로 판정하므로, 초대 코드가 틀린 요청은 다른 입력이 어떻든 403 만 받는다.

| 순서 | 상황 | 상태 | error |
| --- | --- | --- | --- |
| 1 | GET 등 다른 메서드 | 405 | `method_not_allowed` |
| 2 | 허용하지 않은 Origin | 403 | `origin_not_allowed` |
| 3 | 시크릿 `INVITE_CODE` 가 비어 있음 | 503 | `join_closed` |
| 4 | 본문 2048바이트 초과 | 413 | `payload_too_large` |
| 5 | Content-Type 이 JSON 이 아님, 깨진 JSON | 400 | `invalid_json` |
| 6 | 초대 코드가 틀림, 없음 | 403 | `invalid_code` |
| 7 | consent 가 true 가 아님 | 400 | `consent_required` |
| 8 | 이메일 형식이 틀림, 254자 초과 | 400 | `invalid_email` |
| 9 | 이름이 1자에서 40자 사이가 아님, 제어 문자 포함 | 400 | `invalid_name` |
| 10 | `style_note` 가 문자열이 아님, 정리한 뒤 200자 초과 | 400 | `invalid_style_note` |
| 11 | 활성 구독자 수가 정원 이상 | 409 | `full` (message: "정원이 찼다") |
| 12 | 가입, 재가입, 이미 가입됨 | 200 | |
| | DB 오류, 환경변수 누락 | 500 | `server_error` |

- 이메일은 앞뒤 공백을 떼고 소문자로 바꾼 뒤 본다. 영문, 숫자와 `. _ % + -` 만 받는다(한글 도메인 같은 국제 주소는 받지 않는다).
- `style_note` 는 선택 필드다. 줄바꿈과 탭은 공백 한 칸으로 바꾸고 나머지 제어 문자는 지운 뒤 앞뒤 공백을 뗀다. 없거나 null 이거나 그렇게 정리해서 빈 글이면 null 로 저장한다. 글자 수는 코드 포인트로 센다(Postgres 의 `char_length` 와 같다).
- 정원은 이메일을 찾아보기 전에 따진다. 정원이 찬 동안에는 이미 가입한 이메일도 409 를 받는다.
- 응답과 로그에 이메일, 이름, 초대 코드, token, 문체 요청을 남기지 않는다. 로그에는 실패한 단계 이름과 DB 오류 코드만 남는다.

## 1. 마이그레이션 적용

마이그레이션은 세 개이고 파일 이름 순서대로 적용한다. 두 번째(`20261116010000_tracking.sql`)와 세 번째(`20261116020000_style_note.sql`)는 첫 번째가 만든 테이블에 컬럼을 더하므로 순서를 바꾸면 실패한다.

세 번째는 digest-join 을 다시 배포하기 전에 적용한다. 순서가 바뀌면 새 함수가 없는 컬럼(`style_note`)에 쓰려다 실패해서 신규 가입이 전부 500 이 된다.

두 방법 중 하나만 쓴다. SQL 은 여러 번 실행해도 결과가 같다.

### 방법 A: 대시보드 SQL 편집기

1. Supabase 대시보드에서 프로젝트를 열고 SQL Editor 로 간다.
2. `supabase/migrations/20261116000000_subscribers.sql` 내용을 통째로 붙여넣고 실행한다(이미 적용했으면 건너뛴다).
3. `supabase/migrations/20261116010000_tracking.sql` 내용을 통째로 붙여넣고 실행한다(이미 적용했으면 건너뛴다).
4. `supabase/migrations/20261116020000_style_note.sql` 내용을 통째로 붙여넣고 실행한다.
5. 아래 확인 쿼리로 네 테이블(`subscribers`, `feedback_events`, `sent_items`, `click_events`)의 RLS 가 켜졌는지 본다(`rls_on` 이 모두 true 여야 한다).

```sql
select relname, relrowsecurity as rls_on
from pg_class
where relnamespace = 'public'::regnamespace and relkind = 'r'
order by relname;
```

6. 새 컬럼이 생겼는지 본다(다섯 행이 나와야 한다).

```sql
select table_name, column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
  and (table_name, column_name) in (
    ('sent_items', 'arm'), ('sent_items', 'position'),
    ('subscribers', 'signup_source'), ('subscribers', 'consented_at'),
    ('subscribers', 'style_note')
  )
order by table_name, column_name;
```

7. `style_note` 의 길이 제약이 붙었는지 본다(한 행이 나오고 정의에 `char_length(style_note) <= 200` 이 보여야 한다).

```sql
select conname, pg_get_constraintdef(oid) as definition
from pg_constraint
where conrelid = 'public.subscribers'::regclass
  and contype = 'c'
  and pg_get_constraintdef(oid) like '%style_note%';
```

이 방법으로 적용하면 CLI 의 마이그레이션 이력 테이블에는 기록이 남지 않는다. 나중에 CLI 로 옮길 때는 아래처럼 이력만 맞춘다.

```bash
supabase migration repair --status applied 20261116000000
supabase migration repair --status applied 20261116010000
supabase migration repair --status applied 20261116020000
```

### 방법 B: Supabase CLI

저장소 루트에서 실행한다. `supabase/config.toml` 이 저장소에 들어 있으므로 `supabase init` 은 하지 않는다.

```bash
supabase login
supabase link --project-ref <프로젝트 ref>
supabase db push --dry-run                  # 적용될 마이그레이션만 미리 본다
supabase db push
```

최신 문서는 `supabase db push --remote` 로 적는다. 설치된 CLI 가 `--remote` 를 모르면 플래그 없이 실행한다.

앞의 마이그레이션을 대시보드로 이미 적용했다면 `db push` 전에 `supabase migration repair --status applied <적용한 버전>` 으로 이력을 맞춘다(예: `20261116000000`, `20261116010000`). 맞추지 않아도 SQL 이 멱등이라 다시 실행돼도 결과는 같다.

`config.toml` 은 `project_id` 와 두 함수의 `verify_jwt` 만 담은 최소 파일이다. CLI 가 다른 항목이 없다고 거절하면, 파일을 잠시 옮겨 두고 `supabase init` 으로 전체 파일을 만든 뒤 맨 아래에 두 `[functions.*]` 구획을 붙여 넣는다.

## 2. 함수 배포

두 함수 모두 로그인 없는 사람(구독자, 가입 신청자)과 메일 서버가 호출하므로 JWT 검증을 끄고 배포한다(공개 엔드포인트). 인증 수단은 digest-feedback 은 링크에 실린 token, digest-join 은 초대 코드뿐이다.

`supabase/config.toml` 에 아래 두 구획이 들어 있다. 둘 중 하나라도 빠지면 그 함수는 모든 요청에 401 을 준다.

```toml
[functions.digest-feedback]
verify_jwt = false

[functions.digest-join]
verify_jwt = false
```

마이그레이션을 먼저 적용하고, digest-join 은 3번의 시크릿 `INVITE_CODE` 를 먼저 넣은 뒤 배포한다(순서가 바뀌어도 위험하지는 않다. 시크릿이 없는 동안 가입은 전부 503 이다).

```bash
supabase functions deploy digest-feedback
supabase functions deploy digest-join
```

Docker 가 없어도 CLI 가 API 방식으로 배포한다. 강제하려면 `--use-api` 를 붙인다.

`config.toml` 을 쓰지 않을 때는 `--no-verify-jwt` 플래그로도 끌 수 있다고 알려져 있으나, 이 플래그는 공식 문서에서 `functions serve` 용으로만 확인했다. `config.toml` 방식을 권한다.

배포 주소는 아래와 같다.

- `https://<프로젝트 ref>.supabase.co/functions/v1/digest-feedback`: 파이프라인이 `docs/feedback-config.json` 의 `endpoint` 로 쓴다.
- `https://<프로젝트 ref>.supabase.co/functions/v1/digest-join`: 따로 적어 둘 곳이 없다. `join.html` 이 위 주소의 끝을 바꿔서 얻는다. 그래서 두 함수의 이름을 바꾸면 안 된다.

## 3. 함수 시크릿

| 이름 | 값 | 설명 |
| --- | --- | --- |
| `ALLOWED_ORIGIN` | `https://<사용자>.github.io` | 페이지가 열리는 출처. 경로와 끝 슬래시 없이 적는다. 두 함수가 함께 쓴다. |
| `INVITE_CODE` | 길고 무작위인 문자열 | 가입에 필요한 초대 코드. 비어 있으면 가입을 전부 거절한다(503). digest-join 만 쓴다. |
| `MAX_SUBSCRIBERS` | 0 이상의 정수(기본 30) | 활성 구독자 정원. 운영자 본인도 한 명으로 센다. digest-join 만 쓴다. |

```bash
supabase secrets set ALLOWED_ORIGIN=https://<사용자>.github.io
supabase secrets set MAX_SUBSCRIBERS=30

CODE="$(openssl rand -base64 24 | tr '+/' '-_' | tr -d '=')"
echo "$CODE"                                 # 이 값을 초대 링크에 쓴다. 따로 적어 둔다
supabase secrets set INVITE_CODE="$CODE"

supabase secrets list
```

넣은 시크릿의 값은 나중에 되찾을 수 없다고 보고, 초대 코드는 만들 때 따로 적어 둔다. 잃어버렸으면 새 코드로 바꾸면 된다.

- `ALLOWED_ORIGIN` 값이 있으면 `Access-Control-Allow-Origin` 이 그 출처로 고정되고, 다른 출처의 브라우저가 보낸 want, skip, click, 가입은 403 이 된다.
- 값이 없으면 `*` 가 된다. 동작은 하지만 운영에서는 설정하기를 권한다.
- Origin 헤더가 없는 요청(메일 서버의 원클릭 POST, curl)과 수신거부는 이 값과 무관하게 통과한다. 가입은 Origin 이 없어도 초대 코드가 맞아야 한다.
- 초대 코드는 URL 에 그대로 실리므로 영문, 숫자, `-`, `_` 로만 만든다. 함수에는 호출 빈도 제한이 없으니 짧거나 추측하기 쉬운 코드를 쓰지 않는다(32자 안팎을 권한다).
- `MAX_SUBSCRIBERS` 가 숫자가 아니면 기본값 30 을 쓴다. 0 으로 두면 코드를 살려 둔 채 가입만 닫을 수 있다(409).
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` 는 런타임이 넣어 주므로 설정하지 않는다. `SUPABASE_` 로 시작하는 이름은 직접 만들 수도 없다.
- Supabase 는 `service_role` 키를 레거시로 분류했다. 함수는 `SUPABASE_SERVICE_ROLE_KEY` 가 없으면 `SUPABASE_SECRET_KEYS` 의 `default` 항목을 쓴다.

### 초대 링크

```
{PAGES_BASE}/join.html?code=<초대 코드>
```

`PAGES_BASE` 는 페이지가 올라간 주소다(예: `https://<사용자>.github.io/<저장소>`). 이 링크를 초대할 사람에게 직접 보낸다. 링크를 가진 사람은 누구든 정원 안에서 가입할 수 있으니 공개된 곳에 올리지 않는다.

### 초대 코드 바꾸기

코드가 퍼졌거나 초대를 한 차례 마쳤으면 바꾼다. 이미 가입한 구독자에게는 영향이 없다(코드는 가입할 때만 쓴다).

```bash
CODE="$(openssl rand -base64 24 | tr '+/' '-_' | tr -d '=')"
echo "$CODE"
supabase secrets set INVITE_CODE="$CODE"
```

1. 위 명령으로 새 코드를 넣는다.
2. 4번의 (6) curl 에 예전 코드를 넣어 403 이 나오는지 본다. 예전 코드가 여전히 통하면 `supabase functions deploy digest-join` 으로 다시 배포한다(함수는 시작할 때 시크릿을 한 번 읽는다).
3. 새 초대 링크를 만들어 보낸다. 예전 링크는 "초대 코드가 맞지 않습니다"를 보인다.

가입을 잠시 닫으려면 `supabase secrets unset INVITE_CODE` 를 한다. 그동안 가입은 전부 503 이고 페이지는 "지금은 가입을 받지 않습니다"를 보인다.

## 4. 동작 확인(curl)

`ENDPOINT` 를 배포 주소로 바꿔서 실행한다. (1)에서 (7)까지는 가짜 token 과 틀린 코드만 쓰므로 데이터가 바뀌지 않는다.

```bash
ENDPOINT="https://<프로젝트 ref>.supabase.co/functions/v1/digest-feedback"

# (1) GET 은 405
curl -i "$ENDPOINT?t=fake-token-000000&a=unsubscribe"

# (2) 가짜 token 은 404 {"ok":false,"error":"not_found"}
curl -i -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" \
  -d '{"token":"fake-token-000000","action":"want","item_url":"https://example.com/a","digest_date":"2026-11-16"}'

# (3) 원클릭 수신거부 형태도 가짜 token 이면 404
curl -i -X POST "$ENDPOINT?t=fake-token-000000&a=unsubscribe" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "List-Unsubscribe=One-Click"

# (4) CORS preflight 는 204 와 Access-Control-Allow-Origin 헤더
curl -i -X OPTIONS "$ENDPOINT" \
  -H "Origin: https://<사용자>.github.io" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type"

# (5) 클릭: 가짜 token 은 404 {"ok":false,"error":"not_found"}
curl -i -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" \
  -d '{"token":"fake-token-000000","action":"click","item_url":"https://example.com/a","digest_date":"2026-11-16"}'

JOIN_ENDPOINT="https://<프로젝트 ref>.supabase.co/functions/v1/digest-join"

# (6) 가입: 틀린 코드는 403 {"ok":false,"error":"invalid_code"}
curl -i -X POST "$JOIN_ENDPOINT" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","name":"시험","invite_code":"wrong-code","consent":true}'

# (7) 가입: GET 은 405
curl -i "$JOIN_ENDPOINT"
```

결과 읽는 법:

- (2)나 (6)이 401 이면 그 함수의 JWT 검증이 켜진 채 배포된 것이다. `config.toml` 을 확인하고 다시 배포한다.
- (2)나 (5)가 500 이면 마이그레이션이 적용되지 않았거나 함수가 DB 키를 읽지 못한 것이다. 대시보드의 함수 로그에서 `step=` 값을 본다.
- (6)이 503 이면 `INVITE_CODE` 시크릿이 없는 것이다.

### 실제 흐름: 가입

맞는 코드로 시험용 주소를 한 번 가입시킨다. 실제로 한 행이 생긴다.

```bash
# (8) 맞는 코드면 200 {"ok":true}. style_note 는 선택이라 빼도 된다
curl -i -X POST "$JOIN_ENDPOINT" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","name":"시험","invite_code":"<초대 코드>","consent":true,"style_note":"더 짧게, 비전공자도 알 수 있게"}'
```

SQL Editor 에서 확인한다. `status` active, `signup_source` invite, `is_owner` false, `consented_at` 이 방금 시각, `style_note` 가 보낸 글이어야 한다. 같은 curl 을 한 번 더 보내도 응답은 같고 행은 늘지 않는다. `style_note` 만 다른 글로 바꿔 다시 보내도 저장된 값은 바뀌지 않는다(이미 받고 있는 이메일).

```sql
select status, signup_source, is_owner, consented_at, style_note, length(token) as token_length
from subscribers where email = 'test@example.com';
```

```bash
# (8-1) style_note 가 200자를 넘으면 400 {"ok":false,"error":"invalid_style_note"}
curl -i -X POST "$JOIN_ENDPOINT" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"test@example.com\",\"name\":\"시험\",\"invite_code\":\"<초대 코드>\",\"consent\":true,\"style_note\":\"$(printf 'a%.0s' $(seq 201))\"}"
```

(8)이 500 이고 함수 로그가 `step=insert_subscriber` 면 세 번째 마이그레이션이 적용되지 않은 것이다.

정원 확인은 `supabase secrets set MAX_SUBSCRIBERS=0` 으로 바꾸고 (8)을 다시 보내 409 가 나오는지 본 뒤 값을 되돌린다. 바꾼 값이 바로 반영되지 않으면 digest-join 을 다시 배포한다.

### 실제 흐름: 표시와 클릭

위에서 가입시킨 시험용 구독자의 token 을 꺼내고, 받은 글 한 건을 발송 이력에 넣는다. (가입을 거치지 않고 만들려면 아래 주석의 insert 를 쓴다.)

```sql
select token from subscribers where email = 'test@example.com';

-- 가입 없이 직접 만들 때:
-- insert into subscribers (email, name, token)
-- values ('test@example.com', '시험', replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''))
-- returning token;

insert into sent_items (subscriber_id, item_url, digest_date, arm, position)
select id, 'https://example.com/a', current_date, 'shared', 1
from subscribers where email = 'test@example.com';
```

```bash
TOKEN="<위에서 꺼낸 token>"

# (9) 보낸 적 있는 글의 클릭은 200 {"ok":true,"action":"click"}
curl -i -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOKEN\",\"action\":\"click\",\"item_url\":\"https://example.com/a\",\"digest_date\":\"2026-11-16\"}"

# (10) 보낸 적 없는 주소는 404 {"ok":false,"error":"not_found"}
curl -i -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOKEN\",\"action\":\"click\",\"item_url\":\"https://example.com/never-sent\"}"

# (11) want 는 200 {"ok":true,"action":"want"}
curl -i -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOKEN\",\"action\":\"want\",\"item_url\":\"https://example.com/a\",\"digest_date\":\"2026-11-16\"}"
```

(9)를 바로 한 번 더 보내도 `click_events` 는 한 행이다(10분 중복 판정). 확인하고 지운다.

```sql
select count(*) from click_events
where subscriber_id = (select id from subscribers where email = 'test@example.com');

-- 확인이 끝나면 지운다(feedback_events, sent_items, click_events 는 함께 지워진다)
delete from subscribers where email = 'test@example.com';
```

페이지까지 보려면 브라우저에서 `{PAGES_BASE}/go.html?t=<token>&u=https%3A%2F%2Fexample.com%2Fa&d=2026-11-16` 을 연다. 지우기 전이면 example.com 으로 바로 넘어가고, `u` 를 다른 주소로 바꾸면 넘어가지 않고 "원문을 직접 열어 주세요"가 보인다.

## 5. 테스트

```bash
deno test supabase/functions/
```

함수마다 `logic.test.ts` 는 입력 검증을, `handler.test.ts` 는 가짜 저장소로 요청 흐름 전체를 본다. DB 와 네트워크 권한이 필요 없다. `index.ts` 는 Supabase 배선뿐이라 테스트 대상이 아니며, 4번의 curl 로 확인한다.

`digest-join/logic.test.ts` 는 새로 만든 token 이 digest-feedback 의 token 형식 검사를 통과하는지도 본다. 한쪽 규칙만 바뀌어 가입한 사람의 링크가 죽는 일을 막는다.

## 6. 되돌리기

1. 메일에서 피드백 링크, List-Unsubscribe 헤더를 빼고 원문 링크를 `go.html` 을 거치지 않는 원래 주소로 되돌린다(파이프라인 쪽). 순서를 바꾸면 이미 나간 메일의 링크만 죽는다.
2. 함수를 내린다.

```bash
supabase functions delete digest-join
supabase functions delete digest-feedback
supabase secrets unset INVITE_CODE
supabase secrets unset MAX_SUBSCRIBERS
supabase secrets unset ALLOWED_ORIGIN
```

3. 테이블을 지운다. 구독자 명단과 표시, 클릭 기록이 모두 사라지므로 필요하면 먼저 내보낸다.

```sql
drop table if exists click_events;
drop table if exists sent_items;
drop table if exists feedback_events;
drop table if exists subscribers;
```

세 번째 마이그레이션만 되돌리려면 아래만 실행한다. 구독자가 적은 문체 요청이 모두 사라진다. digest-join 을 `style_note` 를 쓰지 않는 판으로 먼저 되돌려 배포하지 않으면 신규 가입이 500 이 된다.

```sql
alter table subscribers drop column if exists style_note;
```

두 번째 마이그레이션만 되돌리려면 아래만 실행한다.

```sql
drop table if exists click_events;
alter table sent_items drop column if exists arm, drop column if exists position;
alter table subscribers drop column if exists signup_source, drop column if exists consented_at;
```

4. CLI 로 적용했다면 이력도 되돌린다.

```bash
supabase migration repair --status reverted 20261116020000
supabase migration repair --status reverted 20261116010000
supabase migration repair --status reverted 20261116000000
```

5. `docs/feedback.html`, `docs/go.html`, `docs/join.html`, `docs/feedback-config.json` 을 지운다.

급히 멈추는 법:

- 가입만 닫기: `supabase secrets unset INVITE_CODE`. 구독자와 메일 링크는 그대로 동작한다.
- 함수까지 멈추기: 2번만 한다. 테이블은 그대로 남는다. `feedback.html` 은 "연결하지 못했습니다"를 보이고, `go.html` 은 자동으로 넘어가지 않는 대신 "원문 열기" 링크를 보여 주므로 이미 나간 메일의 원문 링크는 한 번 더 누르면 열린다.

## 알아 둘 점

- **파이프라인 키**: 세 테이블은 RLS 가 켜져 있고 정책이 없어서 anon 키로는 읽지도 쓰지도 못한다. 파이프라인(`SUPABASE_KEY`)이 구독자 명단을 읽으려면 service role 키(또는 새 방식의 secret 키)여야 한다.
- **token**: 링크에 실리는 유일한 인증 수단이다. 추측할 수 없게 길고 무작위로 만든다(예: 32바이트 이상의 URL-safe 난수). 함수는 영문, 숫자, `.` `_` `~` `-` 로 된 8자에서 200자 사이 token 만 조회한다.
- **중복 판정**: 같은 구독자가 같은 글에 남긴 직전 표시와 행동이 같고 60초 안이면 insert 를 건너뛴다. want 에서 skip 으로 바꿨다가 다시 want 로 되돌리는 경우는 60초 안이어도 기록한다.
- **재구독**: 해지한 사람이 초대 코드로 `join.html` 에서 다시 가입하면 `active` 로 돌아간다. 코드가 없으면 운영자가 `subscribers.status` 를 `active` 로 직접 바꾼다.
- **이메일 소유 확인 없음**: 가입할 때 확인 메일을 보내지 않는다. 초대 코드를 가진 사람은 남의 이메일을 넣을 수 있고, 해지한 사람의 이메일을 넣어 다시 받게 만들 수도 있다. 초대 코드를 믿을 수 있는 사람에게만 주는 것이 유일한 방어다. 가입자는 `signup_source = 'invite'` 와 `consented_at` 으로 추려 볼 수 있다.
- **정원은 느슨한 상한**: 개수를 센 뒤 넣는 두 단계라, 같은 순간에 여러 명이 가입하면 정원을 한두 명 넘을 수 있다.
- **클릭 주소 비교**: `go.html` 링크의 `u` 를 풀었을 때 `sent_items.item_url` 과 글자 하나까지 같아야 한다. 파이프라인이 한쪽에만 추적용 쿼리를 붙이거나 끝 슬래시를 정리하면 클릭이 전부 404 가 되고 구독자는 "원문을 직접 열어 주세요" 화면을 보게 된다.
- **발송 이력 기록 시점**: 메일을 보내기 전에(또는 직후에 곧바로) `sent_items` 에 넣어야 한다. 늦으면 그 사이의 클릭이 404 가 된다.
- **호출 빈도 제한**: 함수 자체에는 IP 단위 제한이 없다. 본문 크기 상한, 60초 표시 중복 판정, 10분 클릭 중복 판정만 있다. 초대 코드 대입 시도를 막는 것은 코드의 길이뿐이다.
- **JS 를 실행하는 스캐너**: 링크를 열고 JS 까지 실행하는 보안 스캐너는 want, skip, click 을 대신 남길 수 있다. 수신거부는 버튼을 눌러야 해서 영향이 없다. 클릭 수를 읽을 때 이 점을 감안한다.
- **문체 요청(style_note)**: 구독자가 자유롭게 쓴 글이다. 제어 문자만 걸러 내고 `<`, `&`, 따옴표는 그대로 저장하므로 화면이나 메일에 보일 때는 이스케이프한다. 파이프라인은 이 글을 요약 모델에 넘기는데, 가입자가 쓴 글이 그대로 모델 입력이 되므로 지시가 아니라 "문체 참고 자료"로 구분해서 넣고, 요약의 사실 내용이나 다른 구독자의 메일에는 영향이 없게 한다. 받고 있는 구독자의 요청을 바꾸거나 지울 때는 운영자가 SQL Editor 에서 직접 고친다(`update subscribers set style_note = '<새 요청>' where email = '<이메일>';`, 지울 때는 `null`). 이렇게 넣는 값은 함수의 정리를 거치지 않으니 200자 제약만 걸린다.
- **이름 표시**: 이름은 제어 문자만 걸러 내고 `<`, `&` 같은 글자는 그대로 저장한다. 메일 템플릿과 페이지에서 반드시 이스케이프해서 쓴다.
