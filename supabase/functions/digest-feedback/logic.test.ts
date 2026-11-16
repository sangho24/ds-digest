// 순수 로직 테스트. 실행: deno test supabase/functions/digest-feedback/
// 단언은 node:assert 를 쓴다(Deno 가 기본 지원하고 외부 내려받기가 필요 없다).

import assert from "node:assert/strict";

import {
  buildCorsHeaders,
  extractRawInput,
  isDuplicateClick,
  isDuplicateEvent,
  isJsonContentType,
  isOriginAllowed,
  isValidItemUrl,
  MAX_ITEM_URL_LENGTH,
  normalizeAllowedOrigin,
  normalizeDigestDate,
  resolveServiceKey,
  validateInput,
} from "./logic.ts";
import type { RawInput } from "./logic.ts";

const TOKEN = "tok_abcdefghijklmnop0123456789";

function jsonInput(overrides: Partial<RawInput>): RawInput {
  return {
    source: "json",
    token: TOKEN,
    action: "want",
    itemUrl: "https://example.com/post?a=1",
    digestDate: "2026-11-16",
    ...overrides,
  };
}

Deno.test("JSON Content-Type 은 매개변수와 대소문자를 무시하고 인식한다", () => {
  assert.equal(isJsonContentType("application/json"), true);
  assert.equal(isJsonContentType("Application/JSON; charset=utf-8"), true);
  assert.equal(isJsonContentType("application/x-www-form-urlencoded"), false);
  assert.equal(isJsonContentType(null), false);
});

Deno.test("JSON 본문에서 네 필드를 꺼낸다", () => {
  const body = JSON.stringify({
    token: TOKEN,
    action: "skip",
    item_url: "https://example.com/a",
    digest_date: "2026-11-16",
  });
  const result = extractRawInput("application/json", body, new URLSearchParams("t=other"));
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.deepEqual(result.value, {
      source: "json",
      token: TOKEN,
      action: "skip",
      itemUrl: "https://example.com/a",
      digestDate: "2026-11-16",
    });
  }
});

Deno.test("깨진 JSON 과 객체가 아닌 JSON 은 400 invalid_json", () => {
  for (const body of ["{", "[1,2]", "\"text\"", "null", ""]) {
    const result = extractRawInput("application/json", body, new URLSearchParams());
    assert.deepEqual(result, { ok: false, status: 400, error: "invalid_json" });
  }
});

Deno.test("원클릭 수신거부 형태는 쿼리 파라미터에서 읽는다", () => {
  const result = extractRawInput(
    "application/x-www-form-urlencoded",
    "List-Unsubscribe=One-Click",
    new URLSearchParams(`t=${TOKEN}&a=unsubscribe`),
  );
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.equal(result.value.source, "query");
    const parsed = validateInput(result.value);
    assert.deepEqual(parsed, { ok: true, value: { kind: "unsubscribe", token: TOKEN } });
  }
});

Deno.test("정상 want 입력은 feedback 명령이 된다", () => {
  assert.deepEqual(validateInput(jsonInput({})), {
    ok: true,
    value: {
      kind: "feedback",
      token: TOKEN,
      action: "want",
      itemUrl: "https://example.com/post?a=1",
      digestDate: "2026-11-16",
    },
  });
});

Deno.test("알 수 없는 action 과 resubscribe 는 400", () => {
  for (const action of ["resubscribe", "like", "", null, undefined, 1]) {
    assert.deepEqual(validateInput(jsonInput({ action })), {
      ok: false,
      status: 400,
      error: "invalid_action",
    });
  }
});

Deno.test("쿼리 파라미터 경로로는 want, skip 을 받지 않는다", () => {
  const raw: RawInput = {
    source: "query",
    token: TOKEN,
    action: "want",
    itemUrl: "https://example.com/a",
    digestDate: null,
  };
  assert.deepEqual(validateInput(raw), { ok: false, status: 400, error: "invalid_action" });
});

Deno.test("token 이 없으면 400, 형식이 틀리면 없는 token 과 같은 404", () => {
  assert.deepEqual(validateInput(jsonInput({ token: "" })), {
    ok: false,
    status: 400,
    error: "missing_token",
  });
  assert.deepEqual(validateInput(jsonInput({ token: null })), {
    ok: false,
    status: 400,
    error: "missing_token",
  });
  for (const token of ["short", "has space in token", "a".repeat(201), "semi;colon;token"]) {
    assert.deepEqual(validateInput(jsonInput({ token })), {
      ok: false,
      status: 404,
      error: "not_found",
    });
  }
});

Deno.test("item_url 은 http, https 만, 2048자 이하만 받는다", () => {
  assert.equal(isValidItemUrl("https://example.com/a"), true);
  assert.equal(isValidItemUrl("http://example.com/a"), true);
  assert.equal(isValidItemUrl("javascript:alert(1)"), false);
  assert.equal(isValidItemUrl("ftp://example.com/a"), false);
  assert.equal(isValidItemUrl("data:text/html,hi"), false);
  assert.equal(isValidItemUrl("example.com/a"), false);
  assert.equal(isValidItemUrl(""), false);

  const base = "https://example.com/";
  const atLimit = base + "a".repeat(MAX_ITEM_URL_LENGTH - base.length);
  assert.equal(atLimit.length, MAX_ITEM_URL_LENGTH);
  assert.equal(isValidItemUrl(atLimit), true);
  assert.equal(isValidItemUrl(atLimit + "a"), false);
});

Deno.test("want, skip 에 item_url 이 없거나 틀리면 400", () => {
  for (const itemUrl of [undefined, null, "", "javascript:alert(1)", 42]) {
    assert.deepEqual(validateInput(jsonInput({ itemUrl })), {
      ok: false,
      status: 400,
      error: "invalid_item_url",
    });
  }
});

Deno.test("unsubscribe 는 item_url 없이도 통과한다", () => {
  const result = validateInput(jsonInput({ action: "unsubscribe", itemUrl: undefined }));
  assert.deepEqual(result, { ok: true, value: { kind: "unsubscribe", token: TOKEN } });
});

Deno.test("digest_date 는 실제로 있는 YYYY-MM-DD 일 때만 남긴다", () => {
  assert.equal(normalizeDigestDate("2026-11-16"), "2026-11-16");
  assert.equal(normalizeDigestDate("2028-02-29"), "2028-02-29");
  assert.equal(normalizeDigestDate("2026-02-30"), null);
  assert.equal(normalizeDigestDate("2026-13-01"), null);
  assert.equal(normalizeDigestDate("2026-1-5"), null);
  assert.equal(normalizeDigestDate("20261116"), null);
  assert.equal(normalizeDigestDate("2026-11-16T00:00:00Z"), null);
  assert.equal(normalizeDigestDate(""), null);
  assert.equal(normalizeDigestDate(null), null);
  assert.equal(normalizeDigestDate(20261116), null);
});

Deno.test("형식이 틀린 digest_date 는 요청을 막지 않고 null 로 저장한다", () => {
  const result = validateInput(jsonInput({ digestDate: "어제" }));
  assert.equal(result.ok, true);
  if (result.ok && result.value.kind === "feedback") {
    assert.equal(result.value.digestDate, null);
  }
});

Deno.test("ALLOWED_ORIGIN 정리: 공백과 끝 슬래시를 떼고, 비면 null", () => {
  assert.equal(normalizeAllowedOrigin(undefined), null);
  assert.equal(normalizeAllowedOrigin("   "), null);
  assert.equal(normalizeAllowedOrigin(" https://someone.github.io/ "), "https://someone.github.io");
});

Deno.test("CORS 헤더: 설정이 있으면 그 출처만, 없으면 *", () => {
  const open = buildCorsHeaders(null);
  assert.equal(open["Access-Control-Allow-Origin"], "*");
  assert.equal(open["Vary"], undefined);

  const fixed = buildCorsHeaders("https://someone.github.io");
  assert.equal(fixed["Access-Control-Allow-Origin"], "https://someone.github.io");
  assert.equal(fixed["Vary"], "Origin");
  assert.equal(fixed["Access-Control-Allow-Methods"], "POST, OPTIONS");
});

Deno.test("Origin 판정: 헤더가 없으면 통과, 있으면 일치해야 통과", () => {
  assert.equal(isOriginAllowed(null, "https://someone.github.io"), true);
  assert.equal(isOriginAllowed("https://someone.github.io", "https://someone.github.io"), true);
  assert.equal(isOriginAllowed("https://evil.example", "https://someone.github.io"), false);
  assert.equal(isOriginAllowed("https://evil.example", null), true);
});

Deno.test("중복 판정: 직전 표시가 같은 행동이고 60초 안일 때만 중복", () => {
  const now = new Date("2026-11-16T00:01:00.000Z");
  assert.equal(isDuplicateEvent(null, "want", now), false);
  assert.equal(
    isDuplicateEvent({ action: "want", createdAt: "2026-11-16T00:00:30.000Z" }, "want", now),
    true,
  );
  assert.equal(
    isDuplicateEvent({ action: "skip", createdAt: "2026-11-16T00:00:30.000Z" }, "want", now),
    false,
  );
  assert.equal(
    isDuplicateEvent({ action: "want", createdAt: "2026-11-16T00:00:00.000Z" }, "want", now),
    false,
  );
  assert.equal(isDuplicateEvent({ action: "want", createdAt: "깨진 시각" }, "want", now), false);
});

Deno.test("서비스 키: 레거시 키 우선, 없으면 SUPABASE_SECRET_KEYS 의 default", () => {
  const env = (values: Record<string, string>) => (name: string): string | undefined =>
    values[name];
  assert.equal(resolveServiceKey(env({ SUPABASE_SERVICE_ROLE_KEY: "legacy" })), "legacy");
  assert.equal(
    resolveServiceKey(env({ SUPABASE_SECRET_KEYS: JSON.stringify({ default: "sb_secret_x" }) })),
    "sb_secret_x",
  );
  assert.equal(
    resolveServiceKey(env({
      SUPABASE_SERVICE_ROLE_KEY: "legacy",
      SUPABASE_SECRET_KEYS: JSON.stringify({ default: "sb_secret_x" }),
    })),
    "legacy",
  );
  assert.equal(resolveServiceKey(env({ SUPABASE_SECRET_KEYS: "not json" })), null);
  assert.equal(resolveServiceKey(env({ SUPABASE_SECRET_KEYS: "{}" })), null);
  assert.equal(resolveServiceKey(env({})), null);
});

Deno.test("click 은 JSON 으로만 받고 item_url 을 요구한다", () => {
  const ok = validateInput(jsonInput({ action: "click" }));
  assert.deepEqual(ok, {
    ok: true,
    value: {
      kind: "click",
      token: TOKEN,
      itemUrl: "https://example.com/post?a=1",
      digestDate: "2026-11-16",
    },
  });

  const viaQuery = validateInput(jsonInput({ action: "click", source: "query" }));
  assert.deepEqual(viaQuery, { ok: false, status: 400, error: "invalid_action" });

  const noUrl = validateInput(jsonInput({ action: "click", itemUrl: undefined }));
  assert.deepEqual(noUrl, { ok: false, status: 400, error: "invalid_item_url" });

  const badDate = validateInput(jsonInput({ action: "click", digestDate: "어제" }));
  assert.equal(badDate.ok && badDate.value.kind === "click" && badDate.value.digestDate === null, true);
});

Deno.test("클릭 중복 판정: 직전 클릭이 10분 안일 때만 중복", () => {
  const now = new Date("2026-11-16T09:10:00.000Z");
  assert.equal(isDuplicateClick(null, now), false);
  assert.equal(isDuplicateClick("2026-11-16T09:00:01.000Z", now), true);
  assert.equal(isDuplicateClick("2026-11-16T09:00:00.000Z", now), false);
  assert.equal(isDuplicateClick("2026-11-15T09:09:59.000Z", now), false);
  assert.equal(isDuplicateClick("날짜 아님", now), false);
});
