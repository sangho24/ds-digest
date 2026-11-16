// 가입 요청 처리 흐름 테스트. 가짜 저장소를 주입하므로 DB 와 네트워크가 필요 없다.
// 실행: deno test supabase/functions/digest-join/

import assert from "node:assert/strict";

import { createHandler, StoreError } from "./handler.ts";
import type { ExistingSubscriber, JoinStore, NewSubscriber } from "./handler.ts";
import { MAX_BODY_BYTES } from "./logic.ts";

const ENDPOINT = "https://project.supabase.co/functions/v1/digest-join";
const ORIGIN = "https://someone.github.io";
const INVITE_CODE = "friends-only-7f3a91c2";
const NOW = new Date("2026-11-16T09:00:00.000Z");
const FIXED_TOKEN = "fixedtoken_0123456789abcdefghijklmnopqrstuv";

/** 가짜 subscribers 테이블의 한 행. */
interface Row {
  id: string;
  email: string;
  name: string | null;
  status: string;
  token: string;
  isOwner: boolean;
  signupSource: string;
  consentedAt: string | null;
  unsubscribedAt: string | null;
}

interface Harness {
  handle: (req: Request) => Promise<Response>;
  rows: Row[];
  logs: string[];
  /** 저장소 메서드가 불린 순서. */
  calls: string[];
  /** 구독자 id 별 style_note 컬럼 값. 행 모양을 바꾸지 않으려고 따로 둔다. */
  styleNotes: Map<string, string | null>;
}

interface HarnessOptions {
  allowedOrigin?: string | null;
  inviteCode?: string | null;
  maxSubscribers?: number;
  failAt?: string;
  /** true 면 insert 가 유일 조건 충돌을 흉내 낸다. */
  insertConflict?: boolean;
}

function seedRows(): Row[] {
  return [
    {
      id: "sub-owner",
      email: "owner@example.com",
      name: "운영자",
      status: "active",
      token: "tok_owner_0123456789",
      isOwner: true,
      signupSource: "manual",
      consentedAt: null,
      unsubscribedAt: null,
    },
    {
      id: "sub-active",
      email: "active@example.com",
      name: "받는중",
      status: "active",
      token: "tok_active_0123456789",
      isOwner: false,
      signupSource: "invite",
      consentedAt: "2026-11-01T00:00:00.000Z",
      unsubscribedAt: null,
    },
    {
      id: "sub-gone",
      email: "gone@example.com",
      name: "그만둠",
      status: "unsubscribed",
      token: "tok_gone_0123456789",
      isOwner: false,
      signupSource: "manual",
      consentedAt: null,
      unsubscribedAt: "2026-11-10T00:00:00.000Z",
    },
  ];
}

function createHarness(options: HarnessOptions = {}): Harness {
  const rows = seedRows();
  const logs: string[] = [];
  const calls: string[] = [];
  const styleNotes = new Map<string, string | null>([
    ["sub-owner", null],
    ["sub-active", "원래 요청"],
    ["sub-gone", "예전 요청"],
  ]);

  const failIf = (step: string): void => {
    if (options.failAt === step) throw new StoreError(step, "XX000");
  };

  const store: JoinStore = {
    countActiveSubscribers(): Promise<number> {
      calls.push("count");
      failIf("count_active");
      return Promise.resolve(rows.filter((row) => row.status === "active").length);
    },
    findSubscriberByEmail(email: string): Promise<ExistingSubscriber | null> {
      calls.push("find");
      failIf("find_subscriber");
      const row = rows.find((candidate) => candidate.email === email);
      return Promise.resolve(row ? { id: row.id, status: row.status } : null);
    },
    insertSubscriber(subscriber: NewSubscriber): Promise<"inserted" | "conflict"> {
      calls.push("insert");
      failIf("insert_subscriber");
      if (options.insertConflict === true) return Promise.resolve("conflict");
      styleNotes.set(`sub-new-${rows.length}`, subscriber.styleNote);
      rows.push({
        id: `sub-new-${rows.length}`,
        email: subscriber.email,
        name: subscriber.name,
        status: "active",
        token: subscriber.token,
        isOwner: false,
        signupSource: "invite",
        consentedAt: subscriber.consentedAt.toISOString(),
        unsubscribedAt: null,
      });
      return Promise.resolve("inserted");
    },
    reactivateSubscriber(
      subscriberId: string,
      consentedAt: Date,
      styleNote: string | null,
    ): Promise<void> {
      calls.push("reactivate");
      failIf("reactivate_subscriber");
      for (const row of rows) {
        if (row.id === subscriberId && row.status === "unsubscribed") {
          row.status = "active";
          row.consentedAt = consentedAt.toISOString();
          if (styleNote !== null) styleNotes.set(row.id, styleNote);
        }
      }
      return Promise.resolve();
    },
  };

  const handle = createHandler({
    store,
    allowedOrigin: options.allowedOrigin ?? null,
    inviteCode: options.inviteCode === undefined ? INVITE_CODE : options.inviteCode,
    maxSubscribers: options.maxSubscribers ?? 30,
    now: () => NOW,
    generateToken: () => FIXED_TOKEN,
    logError: (step, code) => logs.push(`${step}:${code}`),
  });
  return { handle, rows, logs, calls, styleNotes };
}

function joinBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    email: "new@example.com",
    name: "새친구",
    invite_code: INVITE_CODE,
    consent: true,
    ...overrides,
  };
}

function jsonPost(body: unknown, headers: Record<string, string> = {}): Request {
  return new Request(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

async function readText(res: Response): Promise<string> {
  return await res.text();
}

async function readJson(res: Response): Promise<Record<string, unknown>> {
  const parsed: unknown = JSON.parse(await readText(res));
  assert.ok(typeof parsed === "object" && parsed !== null);
  return parsed as Record<string, unknown>;
}

function rowByEmail(h: Harness, email: string): Row {
  const row = h.rows.find((candidate) => candidate.email === email);
  assert.ok(row !== undefined, "행이 있어야 한다");
  return row;
}

Deno.test("GET, HEAD, PUT, DELETE 는 405 이고 저장소를 건드리지 않는다", async () => {
  const h = createHarness();
  for (const method of ["GET", "HEAD", "PUT", "DELETE"]) {
    const res = await h.handle(new Request(ENDPOINT, { method }));
    assert.equal(res.status, 405);
    assert.equal(res.headers.get("allow"), "POST, OPTIONS");
  }
  assert.deepEqual(h.calls, []);
  assert.equal(h.rows.length, 3);
});

Deno.test("OPTIONS preflight 는 204 와 CORS 헤더를 준다", async () => {
  const h = createHarness({ allowedOrigin: ORIGIN });
  const res = await h.handle(
    new Request(ENDPOINT, {
      method: "OPTIONS",
      headers: { Origin: ORIGIN, "Access-Control-Request-Method": "POST" },
    }),
  );
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("access-control-allow-origin"), ORIGIN);
  assert.equal(res.headers.get("access-control-allow-methods"), "POST, OPTIONS");
  assert.match(res.headers.get("access-control-allow-headers") ?? "", /content-type/);
});

Deno.test("신규 가입: active, invite, 동의 시각, 새 token 으로 한 행을 넣고 {ok:true}", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(joinBody({ email: "  New@Example.com ", name: " 새친구 " })));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true });
  assert.equal(h.rows.length, 4);
  assert.deepEqual(rowByEmail(h, "new@example.com"), {
    id: "sub-new-3",
    email: "new@example.com",
    name: "새친구",
    status: "active",
    token: FIXED_TOKEN,
    isOwner: false,
    signupSource: "invite",
    consentedAt: NOW.toISOString(),
    unsubscribedAt: null,
  });
  assert.deepEqual(h.calls, ["count", "find", "insert"]);
});

Deno.test("이미 받고 있는 이메일: 아무것도 바꾸지 않고 신규와 같은 응답", async () => {
  const h = createHarness();
  const before = structuredClone(h.rows);
  const res = await h.handle(jsonPost(joinBody({ email: "ACTIVE@example.com", name: "다른이름" })));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true });
  assert.deepEqual(h.rows, before);
  assert.deepEqual(h.calls, ["count", "find"]);
});

Deno.test("해지한 이메일: active 로 되돌리고 동의 시각만 갱신, 응답은 신규와 같다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(joinBody({ email: "gone@example.com", name: "새이름" })));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true });
  assert.equal(h.rows.length, 3);
  assert.deepEqual(rowByEmail(h, "gone@example.com"), {
    id: "sub-gone",
    email: "gone@example.com",
    name: "그만둠",
    status: "active",
    token: "tok_gone_0123456789",
    isOwner: false,
    signupSource: "manual",
    consentedAt: NOW.toISOString(),
    unsubscribedAt: "2026-11-10T00:00:00.000Z",
  });
  assert.deepEqual(h.calls, ["count", "find", "reactivate"]);
});

Deno.test("신규, 기존, 재가입의 응답은 상태와 본문과 헤더가 모두 같다", async () => {
  const snapshots: string[] = [];
  for (const email of ["new@example.com", "active@example.com", "gone@example.com"]) {
    const h = createHarness();
    const res = await h.handle(jsonPost(joinBody({ email })));
    const headers = [...res.headers.entries()].sort().map(([k, v]) => `${k}=${v}`).join("|");
    snapshots.push(`${res.status} ${await readText(res)} ${headers}`);
  }
  assert.equal(snapshots[0], snapshots[1]);
  assert.equal(snapshots[0], snapshots[2]);
});

Deno.test("운영자 이메일로 가입해도 is_owner 와 다른 값은 그대로다", async () => {
  const h = createHarness();
  const before = structuredClone(rowByEmail(h, "owner@example.com"));
  const res = await h.handle(jsonPost(joinBody({ email: "owner@example.com", is_owner: false })));
  assert.equal(res.status, 200);
  assert.deepEqual(rowByEmail(h, "owner@example.com"), before);
});

Deno.test("본문에 is_owner, status, token 을 실어 보내도 무시한다", async () => {
  const h = createHarness();
  const res = await h.handle(
    jsonPost(joinBody({ is_owner: true, status: "unsubscribed", token: "attacker-chosen-token" })),
  );
  assert.equal(res.status, 200);
  const row = rowByEmail(h, "new@example.com");
  assert.equal(row.isOwner, false);
  assert.equal(row.status, "active");
  assert.equal(row.token, FIXED_TOKEN);
});

Deno.test("동시에 같은 이메일이 먼저 들어와 insert 가 충돌해도 {ok:true}", async () => {
  const h = createHarness({ insertConflict: true });
  const res = await h.handle(jsonPost(joinBody()));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true });
  assert.equal(h.rows.length, 3);
  assert.deepEqual(h.logs, []);
});

Deno.test("초대 코드가 틀리면 403 이고 저장소를 보지 않는다", async () => {
  const h = createHarness();
  for (const code of ["wrong-code", "", INVITE_CODE.toUpperCase(), `${INVITE_CODE}x`, null, 12345]) {
    const res = await h.handle(jsonPost(joinBody({ invite_code: code })));
    assert.equal(res.status, 403);
    assert.deepEqual(await readJson(res), { ok: false, error: "invalid_code" });
  }
  const missing = joinBody();
  delete missing.invite_code;
  assert.equal((await h.handle(jsonPost(missing))).status, 403);
  assert.deepEqual(h.calls, []);
  assert.equal(h.rows.length, 3);
});

Deno.test("코드가 틀리면 다른 입력이 엉망이어도 403 만 돌려준다", async () => {
  const h = createHarness();
  const res = await h.handle(
    jsonPost({ email: "nope", name: "", invite_code: "wrong", consent: false }),
  );
  assert.equal(res.status, 403);
  assert.deepEqual(await readJson(res), { ok: false, error: "invalid_code" });
});

Deno.test("INVITE_CODE 시크릿이 없으면 어떤 코드로도 503", async () => {
  const h = createHarness({ inviteCode: null });
  for (const code of ["", INVITE_CODE, "anything"]) {
    const res = await h.handle(jsonPost(joinBody({ invite_code: code })));
    assert.equal(res.status, 503);
    assert.deepEqual(await readJson(res), { ok: false, error: "join_closed" });
  }
  assert.deepEqual(h.calls, []);
  assert.equal(h.rows.length, 3);
});

Deno.test("동의가 true 가 아니면 400", async () => {
  const h = createHarness();
  for (const consent of [false, "true", 1, null]) {
    const res = await h.handle(jsonPost(joinBody({ consent })));
    assert.equal(res.status, 400);
    assert.deepEqual(await readJson(res), { ok: false, error: "consent_required" });
  }
  const missing = joinBody();
  delete missing.consent;
  assert.equal((await h.handle(jsonPost(missing))).status, 400);
  assert.deepEqual(h.calls, []);
});

Deno.test("이메일, 이름 형식이 틀리면 400 이고 저장소를 보지 않는다", async () => {
  const h = createHarness();
  const badEmail = await h.handle(jsonPost(joinBody({ email: "not-an-email" })));
  assert.equal(badEmail.status, 400);
  assert.deepEqual(await readJson(badEmail), { ok: false, error: "invalid_email" });

  const longEmail = await h.handle(jsonPost(joinBody({ email: `${"a".repeat(250)}@example.com` })));
  assert.equal(longEmail.status, 400);

  const emptyName = await h.handle(jsonPost(joinBody({ name: "   " })));
  assert.equal(emptyName.status, 400);
  assert.deepEqual(await readJson(emptyName), { ok: false, error: "invalid_name" });

  const longName = await h.handle(jsonPost(joinBody({ name: "가".repeat(41) })));
  assert.equal(longName.status, 400);
  assert.deepEqual(h.calls, []);
});

Deno.test("정원이 차면 409 와 '정원이 찼다', 행은 늘지 않는다", async () => {
  // 기본 행 가운데 active 는 둘이다.
  const h = createHarness({ maxSubscribers: 2 });
  const res = await h.handle(jsonPost(joinBody()));
  assert.equal(res.status, 409);
  assert.deepEqual(await readJson(res), { ok: false, error: "full", message: "정원이 찼다" });
  assert.equal(h.rows.length, 3);
  assert.deepEqual(h.calls, ["count"]);
});

Deno.test("정원이 찬 동안에는 기존 이메일과 해지한 이메일도 똑같이 409", async () => {
  for (const email of ["active@example.com", "gone@example.com", "owner@example.com"]) {
    const h = createHarness({ maxSubscribers: 2 });
    const before = structuredClone(h.rows);
    const res = await h.handle(jsonPost(joinBody({ email })));
    assert.equal(res.status, 409);
    assert.deepEqual(await readJson(res), { ok: false, error: "full", message: "정원이 찼다" });
    assert.deepEqual(h.rows, before);
  }
});

Deno.test("정원 바로 아래면 받고, 그 다음 사람은 409", async () => {
  const h = createHarness({ maxSubscribers: 3 });
  const first = await h.handle(jsonPost(joinBody({ email: "one@example.com" })));
  assert.equal(first.status, 200);
  const second = await h.handle(jsonPost(joinBody({ email: "two@example.com" })));
  assert.equal(second.status, 409);
  assert.equal(h.rows.filter((row) => row.status === "active").length, 3);
});

Deno.test("해지한 구독자는 정원에 세지 않는다", async () => {
  const h = createHarness({ maxSubscribers: 3 });
  const res = await h.handle(jsonPost(joinBody()));
  assert.equal(res.status, 200);
});

Deno.test("ALLOWED_ORIGIN 이 있을 때 다른 출처의 가입은 403, 같은 출처와 Origin 없음은 통과", async () => {
  const h = createHarness({ allowedOrigin: ORIGIN });
  const blocked = await h.handle(jsonPost(joinBody(), { Origin: "https://evil.example" }));
  assert.equal(blocked.status, 403);
  assert.deepEqual(await readJson(blocked), { ok: false, error: "origin_not_allowed" });
  assert.equal(blocked.headers.get("access-control-allow-origin"), ORIGIN);
  assert.deepEqual(h.calls, []);
  assert.equal(h.rows.length, 3);

  const allowed = await h.handle(jsonPost(joinBody({ email: "a@example.com" }), { Origin: ORIGIN }));
  assert.equal(allowed.status, 200);

  const noOrigin = await h.handle(jsonPost(joinBody({ email: "b@example.com" })));
  assert.equal(noOrigin.status, 200);
  assert.equal(h.rows.length, 5);
});

Deno.test("ALLOWED_ORIGIN 이 없으면 Allow-Origin 은 * 이고 어느 출처든 받는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(joinBody(), { Origin: "https://anywhere.example" }));
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("access-control-allow-origin"), "*");
});

Deno.test("본문이 2048바이트를 넘으면 413 이고 저장소를 보지 않는다", async () => {
  assert.equal(MAX_BODY_BYTES, 2048);
  const h = createHarness();
  const res = await h.handle(jsonPost(joinBody({ pad: "x".repeat(MAX_BODY_BYTES) })));
  assert.equal(res.status, 413);
  assert.deepEqual(await readJson(res), { ok: false, error: "payload_too_large" });
  assert.deepEqual(h.calls, []);
});

Deno.test("Content-Length 를 속여도 실제로 읽은 크기로 413", async () => {
  const h = createHarness();
  const body = JSON.stringify(joinBody({ pad: "x".repeat(MAX_BODY_BYTES) }));
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(body));
      controller.close();
    },
  });
  const init: RequestInit & { duplex: "half" } = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: stream,
    duplex: "half",
  };
  const res = await h.handle(new Request(ENDPOINT, init));
  assert.equal(res.status, 413);
  assert.deepEqual(h.calls, []);
});

Deno.test("깨진 JSON 과 JSON 이 아닌 본문은 400", async () => {
  const h = createHarness();
  const broken = await h.handle(jsonPost("{not json"));
  assert.equal(broken.status, 400);
  assert.deepEqual(await readJson(broken), { ok: false, error: "invalid_json" });

  const form = await h.handle(
    new Request(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `email=new%40example.com&name=a&invite_code=${INVITE_CODE}&consent=true`,
    }),
  );
  assert.equal(form.status, 400);
  assert.deepEqual(await readJson(form), { ok: false, error: "invalid_json" });
  assert.deepEqual(h.calls, []);
});

Deno.test("저장소 오류는 500 이고 로그에는 단계와 코드만 남는다", async () => {
  for (const step of ["count_active", "find_subscriber", "insert_subscriber"]) {
    const h = createHarness({ failAt: step });
    const res = await h.handle(jsonPost(joinBody()));
    assert.equal(res.status, 500);
    assert.deepEqual(await readJson(res), { ok: false, error: "server_error" });
    assert.deepEqual(h.logs, [`${step}:XX000`]);
  }
  const h = createHarness({ failAt: "reactivate_subscriber" });
  const res = await h.handle(jsonPost(joinBody({ email: "gone@example.com" })));
  assert.equal(res.status, 500);
  assert.deepEqual(h.logs, ["reactivate_subscriber:XX000"]);
});

Deno.test("어떤 응답과 로그에도 이메일, 이름, 초대 코드, token 이 없다", async () => {
  const secrets = ["leak-check@example.com", "유출확인", INVITE_CODE, FIXED_TOKEN];
  const body = joinBody({ email: "leak-check@example.com", name: "유출확인" });
  const scenarios: Array<{ options: HarnessOptions; body: Record<string, unknown> }> = [
    { options: {}, body },
    { options: { maxSubscribers: 0 }, body },
    { options: { inviteCode: null }, body },
    { options: { failAt: "insert_subscriber" }, body },
    { options: { failAt: "count_active" }, body },
    { options: {}, body: { ...body, consent: false } },
    { options: {}, body: { ...body, name: "유출확인".repeat(20) } },
    { options: {}, body: { ...body, invite_code: `${INVITE_CODE}-wrong` } },
  ];
  for (const scenario of scenarios) {
    const h = createHarness(scenario.options);
    const res = await h.handle(jsonPost(scenario.body));
    const visible = `${await readText(res)} ${[...res.headers.values()].join(" ")} ${h.logs.join(" ")}`;
    for (const secret of secrets) {
      assert.equal(visible.includes(secret), false, `status=${res.status} 에서 값이 드러났다`);
    }
  }
});

Deno.test("모든 응답은 JSON 이고 캐시하지 않는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(joinBody()));
  assert.match(res.headers.get("content-type") ?? "", /^application\/json/);
  assert.equal(res.headers.get("cache-control"), "no-store");
});

Deno.test("문체 요청: 신규 가입이면 정리한 값을 저장하고 응답은 {ok:true} 그대로다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(joinBody({ style_note: "  더 짧게,\n비전공자도 알 수 있게\u0007  " })));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true });
  assert.equal(h.styleNotes.get("sub-new-3"), "더 짧게, 비전공자도 알 수 있게");
  assert.deepEqual(h.calls, ["count", "find", "insert"]);
});

Deno.test("문체 요청: 없거나 null 이거나 비어 있으면 null 로 저장한다", async () => {
  for (const styleNote of [undefined, null, "", "   ", "\n\t", "\u0000\u001f"]) {
    const h = createHarness();
    const body = joinBody();
    if (styleNote !== undefined) body.style_note = styleNote;
    const res = await h.handle(jsonPost(body));
    assert.equal(res.status, 200);
    assert.equal(h.styleNotes.has("sub-new-3"), true);
    assert.equal(h.styleNotes.get("sub-new-3"), null);
  }
});

Deno.test("문체 요청: 200자는 받고 201자와 문자열이 아닌 값은 400, 저장소를 보지 않는다", async () => {
  const ok = createHarness();
  const exact = await ok.handle(jsonPost(joinBody({ style_note: "가".repeat(200) })));
  assert.equal(exact.status, 200);
  assert.equal(ok.styleNotes.get("sub-new-3"), "가".repeat(200));

  const h = createHarness();
  for (const styleNote of ["가".repeat(201), "😀".repeat(201), 123, true, ["짧게"], { a: 1 }]) {
    const res = await h.handle(jsonPost(joinBody({ style_note: styleNote })));
    assert.equal(res.status, 400);
    assert.deepEqual(await readJson(res), { ok: false, error: "invalid_style_note" });
  }
  assert.deepEqual(h.calls, []);
  assert.equal(h.rows.length, 3);
});

Deno.test("문체 요청: 해지한 이메일의 재가입은 적었을 때만 덮어쓴다", async () => {
  const filled = createHarness();
  const first = await filled.handle(
    jsonPost(joinBody({ email: "gone@example.com", style_note: " 새 요청 " })),
  );
  assert.equal(first.status, 200);
  assert.equal(filled.styleNotes.get("sub-gone"), "새 요청");
  assert.equal(rowByEmail(filled, "gone@example.com").status, "active");

  for (const styleNote of [undefined, null, "", "   "]) {
    const h = createHarness();
    const body = joinBody({ email: "gone@example.com" });
    if (styleNote !== undefined) body.style_note = styleNote;
    const res = await h.handle(jsonPost(body));
    assert.equal(res.status, 200);
    assert.equal(h.styleNotes.get("sub-gone"), "예전 요청");
    assert.equal(rowByEmail(h, "gone@example.com").status, "active");
  }
});

Deno.test("문체 요청: 이미 받고 있는 이메일과 운영자의 값은 바뀌지 않는다", async () => {
  const h = createHarness();
  const before = new Map(h.styleNotes);
  for (const email of ["active@example.com", "owner@example.com"]) {
    const res = await h.handle(jsonPost(joinBody({ email, style_note: "남이 바꾼 요청" })));
    assert.equal(res.status, 200);
    assert.deepEqual(await readJson(res), { ok: true });
  }
  assert.deepEqual(h.styleNotes, before);
  assert.deepEqual(h.calls, ["count", "find", "count", "find"]);
});

Deno.test("문체 요청: 코드, 동의, 이메일, 이름을 먼저 본다", async () => {
  const h = createHarness();
  const long = "가".repeat(201);
  const cases: Array<[Record<string, unknown>, number, string]> = [
    [{ invite_code: "wrong", style_note: long }, 403, "invalid_code"],
    [{ consent: false, style_note: long }, 400, "consent_required"],
    [{ email: "nope", style_note: long }, 400, "invalid_email"],
    [{ name: "", style_note: long }, 400, "invalid_name"],
  ];
  for (const [overrides, status, error] of cases) {
    const res = await h.handle(jsonPost(joinBody(overrides)));
    assert.equal(res.status, status);
    assert.deepEqual(await readJson(res), { ok: false, error });
  }
  assert.deepEqual(h.calls, []);
});

Deno.test("문체 요청: 정원이 차면 요청이 있어도 409 이고 값은 그대로다", async () => {
  const h = createHarness({ maxSubscribers: 2 });
  const before = new Map(h.styleNotes);
  const res = await h.handle(jsonPost(joinBody({ email: "gone@example.com", style_note: "새 요청" })));
  assert.equal(res.status, 409);
  assert.deepEqual(h.styleNotes, before);
});

Deno.test("문체 요청: 4바이트 글자 200자를 꽉 채워도 본문 상한 안이다", async () => {
  const h = createHarness();
  const body = joinBody({
    email: `${"a".repeat(64)}@${"b".repeat(180)}.example`,
    name: "😀".repeat(40),
    style_note: "😀".repeat(200),
  });
  assert.ok(new TextEncoder().encode(JSON.stringify(body)).byteLength < MAX_BODY_BYTES);
  const res = await h.handle(jsonPost(body));
  assert.equal(res.status, 200);
  assert.equal(h.styleNotes.get("sub-new-3"), "😀".repeat(200));
});

Deno.test("문체 요청: 어떤 응답과 로그에도 드러나지 않는다", async () => {
  const note = "유출되면안되는문체요청";
  const scenarios: Array<{ options: HarnessOptions; body: Record<string, unknown> }> = [
    { options: {}, body: joinBody({ style_note: note }) },
    { options: {}, body: joinBody({ email: "gone@example.com", style_note: note }) },
    { options: {}, body: joinBody({ email: "active@example.com", style_note: note }) },
    { options: {}, body: joinBody({ style_note: note.repeat(30) }) },
    { options: { maxSubscribers: 0 }, body: joinBody({ style_note: note }) },
    { options: { inviteCode: null }, body: joinBody({ style_note: note }) },
    { options: { failAt: "insert_subscriber" }, body: joinBody({ style_note: note }) },
    {
      options: { failAt: "reactivate_subscriber" },
      body: joinBody({ email: "gone@example.com", style_note: note }),
    },
    { options: {}, body: joinBody({ invite_code: "wrong", style_note: note }) },
  ];
  for (const scenario of scenarios) {
    const h = createHarness(scenario.options);
    const res = await h.handle(jsonPost(scenario.body));
    const visible = `${await readText(res)} ${[...res.headers.values()].join(" ")} ${h.logs.join(" ")}`;
    assert.equal(visible.includes(note), false, `status=${res.status} 에서 값이 드러났다`);
  }
});
