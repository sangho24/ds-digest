// 순수 로직 테스트. 실행: deno test supabase/functions/digest-join/
// 단언은 node:assert 를 쓴다(Deno 가 기본 지원하고 외부 내려받기가 필요 없다).

import assert from "node:assert/strict";

import { isValidTokenFormat } from "../digest-feedback/logic.ts";
import {
  buildCorsHeaders,
  constantTimeEqual,
  DEFAULT_MAX_SUBSCRIBERS,
  extractJoinInput,
  generateToken,
  isInviteCodeValid,
  isOriginAllowed,
  MAX_STYLE_NOTE_LENGTH,
  normalizeAllowedOrigin,
  normalizeEmail,
  normalizeInviteCode,
  normalizeName,
  normalizeStyleNote,
  parseMaxSubscribers,
  resolveServiceKey,
  toBase64Url,
  validateApplication,
} from "./logic.ts";
import type { RawJoinInput } from "./logic.ts";

function rawInput(overrides: Partial<RawJoinInput> = {}): RawJoinInput {
  return {
    email: "friend@example.com",
    name: "친구",
    inviteCode: "code",
    consent: true,
    ...overrides,
  };
}

Deno.test("JSON 본문에서 네 필드를 꺼낸다", () => {
  const body = JSON.stringify({
    email: "a@example.com",
    name: "가",
    invite_code: "abc",
    consent: true,
    is_owner: true,
  });
  const result = extractJoinInput("application/json; charset=utf-8", body);
  // is_owner 같은 여분의 필드는 버려진다.
  assert.deepEqual(result, {
    ok: true,
    value: { email: "a@example.com", name: "가", inviteCode: "abc", consent: true },
  });
});

Deno.test("JSON 이 아닌 본문, 깨진 JSON, 객체가 아닌 JSON 은 400", () => {
  const expected = { ok: false, status: 400, error: "invalid_json" };
  assert.deepEqual(extractJoinInput("application/x-www-form-urlencoded", "email=a%40b.c"), expected);
  assert.deepEqual(extractJoinInput(null, "{}"), expected);
  assert.deepEqual(extractJoinInput("application/json", "{not json"), expected);
  assert.deepEqual(extractJoinInput("application/json", "[1,2]"), expected);
  assert.deepEqual(extractJoinInput("application/json", "null"), expected);
});

Deno.test("이메일: 공백을 떼고 소문자로 바꾼다", () => {
  assert.equal(normalizeEmail("  Friend.Name+tag@Example.CO.KR \n"), "friend.name+tag@example.co.kr");
});

Deno.test("이메일: 형식이 틀리면 null", () => {
  const invalid: unknown[] = [
    "",
    "   ",
    "no-at-sign",
    "a@b",
    "@example.com",
    "a@@example.com",
    "a b@example.com",
    "a@exa mple.com",
    "a@example.com\r\nBcc: x@example.com",
    "a@example..com",
    "a..b@example.com",
    ".a@example.com",
    "a.@example.com",
    "<a@example.com>",
    "a@example.com,b@example.com",
    42,
    null,
    undefined,
    ["a@example.com"],
  ];
  for (const value of invalid) {
    assert.equal(normalizeEmail(value), null, `거절해야 하는 값: ${String(value)}`);
  }
});

Deno.test("이메일: 길이 254자까지 받고 255자는 거절, @ 앞은 64자까지", () => {
  const domain = "@example.com";
  const local64 = "a".repeat(64);
  assert.equal(normalizeEmail(local64 + domain), local64 + domain);
  assert.equal(normalizeEmail("a".repeat(65) + domain), null);

  // 254자: 앞 64자 + "@" + 도메인 189자
  const longDomain = `${"d".repeat(60)}.${"d".repeat(60)}.${"d".repeat(63)}.com`;
  const email254 = `${local64}@${longDomain}`;
  assert.equal(email254.length, 254);
  assert.equal(normalizeEmail(email254), email254);
  assert.equal(normalizeEmail(`${local64}@d${longDomain}`), null);
});

Deno.test("이름: 공백을 떼고 1자에서 40자까지 받는다", () => {
  assert.equal(normalizeName("  상호  "), "상호");
  assert.equal(normalizeName("a"), "a");
  assert.equal(normalizeName("가".repeat(40)), "가".repeat(40));
  assert.equal(normalizeName("가".repeat(41)), null);
  assert.equal(normalizeName(""), null);
  assert.equal(normalizeName("   "), null);
  assert.equal(normalizeName(7), null);
  assert.equal(normalizeName(undefined), null);
});

Deno.test("이름: 글자 수는 코드 포인트로 세고 제어 문자는 거절한다", () => {
  // 그림 문자 하나는 UTF-16 으로 2칸이지만 한 글자로 센다.
  assert.equal(normalizeName("😀".repeat(40)), "😀".repeat(40));
  assert.equal(normalizeName("😀".repeat(41)), null);
  assert.equal(normalizeName("줄\n바꿈"), null);
  assert.equal(normalizeName("탭\t문자"), null);
  assert.equal(normalizeName("널\u0000문자"), null);
});

Deno.test("신청 검증: 동의는 true 그 자체만 받는다", () => {
  for (const consent of [false, "true", 1, null, undefined, {}]) {
    assert.deepEqual(validateApplication(rawInput({ consent })), {
      ok: false,
      status: 400,
      error: "consent_required",
    });
  }
});

Deno.test("신청 검증: 통과하면 정리한 이메일과 이름만 남는다", () => {
  const result = validateApplication(rawInput({ email: " Friend@Example.com ", name: " 친구 " }));
  assert.deepEqual(result, { ok: true, value: { email: "friend@example.com", name: "친구" } });
});

Deno.test("신청 검증: 이메일, 이름이 틀리면 각각의 오류", () => {
  assert.deepEqual(validateApplication(rawInput({ email: "nope" })), {
    ok: false,
    status: 400,
    error: "invalid_email",
  });
  assert.deepEqual(validateApplication(rawInput({ name: "" })), {
    ok: false,
    status: 400,
    error: "invalid_name",
  });
});

Deno.test("초대 코드 시크릿: 없거나 공백뿐이면 null", () => {
  assert.equal(normalizeInviteCode(undefined), null);
  assert.equal(normalizeInviteCode(""), null);
  assert.equal(normalizeInviteCode("   "), null);
  assert.equal(normalizeInviteCode(" abc123 "), "abc123");
});

Deno.test("정원: 0 이상의 정수만 읽고 나머지는 기본값 30", () => {
  assert.equal(DEFAULT_MAX_SUBSCRIBERS, 30);
  assert.equal(parseMaxSubscribers(undefined), 30);
  assert.equal(parseMaxSubscribers(""), 30);
  assert.equal(parseMaxSubscribers("abc"), 30);
  assert.equal(parseMaxSubscribers("-5"), 30);
  assert.equal(parseMaxSubscribers("12.5"), 30);
  assert.equal(parseMaxSubscribers("1e3"), 30);
  assert.equal(parseMaxSubscribers(" 50 "), 50);
  assert.equal(parseMaxSubscribers("0"), 0);
});

Deno.test("상수 시간 비교: 같을 때만 true, 길이가 달라도 판정한다", async () => {
  assert.equal(await constantTimeEqual("invite-2026", "invite-2026"), true);
  assert.equal(await constantTimeEqual("invite-2026", "invite-2027"), false);
  assert.equal(await constantTimeEqual("invite", "invite-2026"), false);
  assert.equal(await constantTimeEqual("", "invite-2026"), false);
  assert.equal(await constantTimeEqual("초대", "초대"), true);
});

Deno.test("초대 코드 판정: 문자열이 아니거나 빈 값은 틀린 것", async () => {
  assert.equal(await isInviteCodeValid("abc123", "abc123"), true);
  assert.equal(await isInviteCodeValid("  abc123\n", "abc123"), true);
  assert.equal(await isInviteCodeValid("ABC123", "abc123"), false);
  assert.equal(await isInviteCodeValid("", "abc123"), false);
  assert.equal(await isInviteCodeValid(undefined, "abc123"), false);
  assert.equal(await isInviteCodeValid(123, "123"), false);
  assert.equal(await isInviteCodeValid(["abc123"], "abc123"), false);
});

Deno.test("base64url: +, /, = 가 나오지 않는다", () => {
  assert.equal(toBase64Url(new Uint8Array([0xfb, 0xff, 0xfe])), "-__-");
  assert.equal(toBase64Url(new Uint8Array([0xff])), "_w");
  assert.equal(toBase64Url(new Uint8Array([])), "");
});

Deno.test("token: 43자 base64url 이고 digest-feedback 의 TOKEN_PATTERN 에 맞는다", () => {
  const seen = new Set<string>();
  for (let i = 0; i < 200; i += 1) {
    const token = generateToken();
    assert.equal(token.length, 43);
    assert.match(token, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(isValidTokenFormat(token), true);
    seen.add(token);
  }
  assert.equal(seen.size, 200);
});

Deno.test("ALLOWED_ORIGIN, CORS, Origin 판정은 digest-feedback 과 같은 규칙", () => {
  assert.equal(normalizeAllowedOrigin(undefined), null);
  assert.equal(normalizeAllowedOrigin("  "), null);
  assert.equal(normalizeAllowedOrigin("https://someone.github.io/"), "https://someone.github.io");

  assert.equal(buildCorsHeaders(null)["Access-Control-Allow-Origin"], "*");
  const fixed = buildCorsHeaders("https://someone.github.io");
  assert.equal(fixed["Access-Control-Allow-Origin"], "https://someone.github.io");
  assert.equal(fixed["Vary"], "Origin");
  assert.equal(fixed["Access-Control-Allow-Methods"], "POST, OPTIONS");

  assert.equal(isOriginAllowed(null, "https://someone.github.io"), true);
  assert.equal(isOriginAllowed("https://someone.github.io", "https://someone.github.io"), true);
  assert.equal(isOriginAllowed("https://evil.example", "https://someone.github.io"), false);
  assert.equal(isOriginAllowed("https://evil.example", null), true);
});

Deno.test("서비스 키: 레거시 키 우선, 없으면 SUPABASE_SECRET_KEYS 의 default", () => {
  const env = (values: Record<string, string>) => (name: string): string | undefined => values[name];
  assert.equal(resolveServiceKey(env({ SUPABASE_SERVICE_ROLE_KEY: "legacy" })), "legacy");
  assert.equal(resolveServiceKey(env({ SUPABASE_SECRET_KEYS: '{"default":"new"}' })), "new");
  assert.equal(resolveServiceKey(env({ SUPABASE_SECRET_KEYS: "not json" })), null);
  assert.equal(resolveServiceKey(env({})), null);
});

Deno.test("문체 요청: 본문에 style_note 가 있을 때만 꺼낸다", () => {
  const base = { email: "a@example.com", name: "가", invite_code: "abc", consent: true };
  const withNote = extractJoinInput("application/json", JSON.stringify({ ...base, style_note: "짧게" }));
  assert.deepEqual(withNote, {
    ok: true,
    value: { email: "a@example.com", name: "가", inviteCode: "abc", consent: true, styleNote: "짧게" },
  });
  const withNull = extractJoinInput("application/json", JSON.stringify({ ...base, style_note: null }));
  assert.ok(withNull.ok);
  assert.equal(withNull.value.styleNote, null);
  const without = extractJoinInput("application/json", JSON.stringify(base));
  assert.ok(without.ok);
  assert.equal("styleNote" in without.value, false);
});

Deno.test("문체 요청: 없음, null, 빈 글은 null", () => {
  for (const value of [undefined, null, "", "   ", "\n\r\t", "\u0000\u007f\u2028"]) {
    assert.deepEqual(normalizeStyleNote(value), { ok: true, value: null });
  }
});

Deno.test("문체 요청: 앞뒤 공백을 떼고 제어 문자를 걸러 낸다", () => {
  assert.deepEqual(normalizeStyleNote("  더 짧게, 비전공자도 알 수 있게  "), {
    ok: true,
    value: "더 짧게, 비전공자도 알 수 있게",
  });
  // 줄바꿈과 탭은 공백 한 칸, 나머지 제어 문자는 사라진다.
  assert.deepEqual(normalizeStyleNote("짧게\r\n쉽게\t부탁\u0000해요\u009f"), {
    ok: true,
    value: "짧게 쉽게 부탁해요",
  });
  assert.deepEqual(normalizeStyleNote("\u2028앞\u2029뒤\u0085"), { ok: true, value: "앞 뒤" });
  // <, &, 따옴표 같은 글자는 그대로 둔다(쓰는 쪽이 이스케이프한다).
  assert.deepEqual(normalizeStyleNote(`<b>"짧게" & '쉽게'</b>`), {
    ok: true,
    value: `<b>"짧게" & '쉽게'</b>`,
  });
});

Deno.test("문체 요청: 정리한 값에는 제어 문자가 남지 않는다", () => {
  let noisy = "가";
  for (let code = 0; code <= 0x9f; code += 1) {
    if (code < 0x20 || code >= 0x7f) noisy += `${String.fromCharCode(code)}나`;
  }
  const result = normalizeStyleNote(noisy);
  assert.ok(result.ok);
  assert.ok(result.value !== null);
  // deno-lint-ignore no-control-regex
  assert.equal(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(result.value), false);
});

Deno.test("문체 요청: 200자까지 받고 넘으면 400(코드 포인트 기준)", () => {
  assert.equal(MAX_STYLE_NOTE_LENGTH, 200);
  const rejected = { ok: false, status: 400, error: "invalid_style_note" };
  assert.deepEqual(normalizeStyleNote("가".repeat(200)), { ok: true, value: "가".repeat(200) });
  assert.deepEqual(normalizeStyleNote("가".repeat(201)), rejected);
  // 이모지 한 글자는 UTF-16 으로 두 칸이지만 한 자로 센다(Postgres char_length 와 같다).
  assert.deepEqual(normalizeStyleNote("😀".repeat(200)), { ok: true, value: "😀".repeat(200) });
  assert.deepEqual(normalizeStyleNote("😀".repeat(201)), rejected);
  // 길이는 정리한 뒤에 잰다.
  assert.deepEqual(normalizeStyleNote(`  ${"가".repeat(200)}\n\u0000 `), {
    ok: true,
    value: "가".repeat(200),
  });
});

Deno.test("문체 요청: 문자열이 아니면 400", () => {
  const rejected = { ok: false, status: 400, error: "invalid_style_note" };
  for (const value of [0, 123, true, false, ["짧게"], { note: "짧게" }]) {
    assert.deepEqual(normalizeStyleNote(value), rejected);
  }
});

Deno.test("문체 요청: 신청 검증 결과는 그대로 이메일과 이름뿐이다", () => {
  const result = validateApplication(rawInput({ styleNote: "짧게" }));
  assert.deepEqual(result, { ok: true, value: { email: "friend@example.com", name: "친구" } });
});
