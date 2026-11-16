// 요청 처리 흐름 테스트. 가짜 저장소를 주입하므로 DB 와 네트워크가 필요 없다.
// 실행: deno test supabase/functions/digest-feedback/

import assert from "node:assert/strict";

import { createHandler, StoreError } from "./handler.ts";
import type {
  FeedbackStore,
  LatestEvent,
  NewClickEvent,
  NewFeedbackEvent,
  SubscriberRef,
} from "./handler.ts";
import { MAX_BODY_BYTES } from "./logic.ts";

const ENDPOINT = "https://project.supabase.co/functions/v1/digest-feedback";
const ACTIVE_TOKEN = "tok_active_0123456789abcdef";
const GONE_TOKEN = "tok_unsubscribed_0123456789";
const ITEM_URL = "https://example.com/post";
const ORIGIN = "https://someone.github.io";
const UNSENT_URL = "https://attacker.example/landing";

interface StoredEvent extends NewFeedbackEvent {
  createdAt: string;
}

interface StoredClick extends NewClickEvent {
  createdAt: string;
}

interface Harness {
  handle: (req: Request) => Promise<Response>;
  events: StoredEvent[];
  clicks: StoredClick[];
  /** 발송 이력. "구독자 id 와 주소를 줄바꿈으로 이은 값"의 집합. */
  sentItems: Set<string>;
  subscribers: Map<string, SubscriberRef>;
  unsubscribeCalls: string[];
  logs: string[];
  clock: { now: Date };
}

function createHarness(options: { allowedOrigin?: string | null; failAt?: string } = {}): Harness {
  const subscribers = new Map<string, SubscriberRef>([
    [ACTIVE_TOKEN, { id: "sub-active", status: "active" }],
    [GONE_TOKEN, { id: "sub-gone", status: "unsubscribed" }],
  ]);
  const events: StoredEvent[] = [];
  const clicks: StoredClick[] = [];
  // 두 구독자 모두 ITEM_URL 은 받은 적이 있고, UNSENT_URL 은 받은 적이 없다.
  const sentItems = new Set<string>([`sub-active\n${ITEM_URL}`, `sub-gone\n${ITEM_URL}`]);
  const unsubscribeCalls: string[] = [];
  const logs: string[] = [];
  const clock = { now: new Date("2026-11-16T09:00:00.000Z") };

  const failIf = (step: string): void => {
    if (options.failAt === step) throw new StoreError(step, "XX000");
  };

  const store: FeedbackStore = {
    findSubscriberByToken(token: string): Promise<SubscriberRef | null> {
      failIf("find_subscriber");
      return Promise.resolve(subscribers.get(token) ?? null);
    },
    findLatestEvent(subscriberId: string, itemUrl: string): Promise<LatestEvent | null> {
      failIf("find_latest_event");
      const matches = events.filter((e) => e.subscriberId === subscriberId && e.itemUrl === itemUrl);
      const last = matches.at(-1);
      return Promise.resolve(last ? { action: last.action, createdAt: last.createdAt } : null);
    },
    insertEvent(event: NewFeedbackEvent): Promise<void> {
      failIf("insert_event");
      events.push({ ...event, createdAt: clock.now.toISOString() });
      return Promise.resolve();
    },
    markUnsubscribed(subscriberId: string): Promise<void> {
      failIf("mark_unsubscribed");
      unsubscribeCalls.push(subscriberId);
      for (const subscriber of subscribers.values()) {
        if (subscriber.id === subscriberId) subscriber.status = "unsubscribed";
      }
      return Promise.resolve();
    },
    hasSentItem(subscriberId: string, itemUrl: string): Promise<boolean> {
      failIf("find_sent_item");
      return Promise.resolve(sentItems.has(`${subscriberId}\n${itemUrl}`));
    },
    findLatestClickAt(subscriberId: string, itemUrl: string): Promise<string | null> {
      failIf("find_latest_click");
      const matches = clicks.filter((c) => c.subscriberId === subscriberId && c.itemUrl === itemUrl);
      return Promise.resolve(matches.at(-1)?.createdAt ?? null);
    },
    insertClick(event: NewClickEvent): Promise<void> {
      failIf("insert_click");
      clicks.push({ ...event, createdAt: clock.now.toISOString() });
      return Promise.resolve();
    },
  };

  const handle = createHandler({
    store,
    allowedOrigin: options.allowedOrigin ?? null,
    now: () => clock.now,
    logError: (step, code) => logs.push(`${step}:${code}`),
  });
  return { handle, events, clicks, sentItems, subscribers, unsubscribeCalls, logs, clock };
}

function jsonPost(body: unknown, headers: Record<string, string> = {}): Request {
  return new Request(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function feedbackBody(action: string, token: string = ACTIVE_TOKEN): Record<string, string> {
  return { token, action, item_url: ITEM_URL, digest_date: "2026-11-16" };
}

async function readJson(res: Response): Promise<Record<string, unknown>> {
  const parsed: unknown = await res.json();
  assert.ok(typeof parsed === "object" && parsed !== null);
  return parsed as Record<string, unknown>;
}

Deno.test("GET 은 405 이고 저장소를 건드리지 않는다", async () => {
  const h = createHarness();
  const url = `${ENDPOINT}?t=${ACTIVE_TOKEN}&a=unsubscribe`;
  const res = await h.handle(new Request(url, { method: "GET" }));
  assert.equal(res.status, 405);
  assert.equal(res.headers.get("allow"), "POST, OPTIONS");
  assert.deepEqual(await readJson(res), { ok: false, error: "method_not_allowed" });
  assert.equal(h.unsubscribeCalls.length, 0);
  assert.equal(h.subscribers.get(ACTIVE_TOKEN)?.status, "active");
});

Deno.test("HEAD, PUT, DELETE 도 405", async () => {
  const h = createHarness();
  for (const method of ["HEAD", "PUT", "DELETE"]) {
    const res = await h.handle(new Request(ENDPOINT, { method }));
    assert.equal(res.status, 405);
  }
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

Deno.test("ALLOWED_ORIGIN 이 없으면 Allow-Origin 은 *", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(feedbackBody("want")));
  assert.equal(res.headers.get("access-control-allow-origin"), "*");
});

Deno.test("want 는 feedback_events 에 한 행을 넣고 200", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(feedbackBody("want")));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "want" });
  assert.equal(h.events.length, 1);
  assert.deepEqual(h.events[0], {
    subscriberId: "sub-active",
    itemUrl: ITEM_URL,
    action: "want",
    digestDate: "2026-11-16",
    createdAt: "2026-11-16T09:00:00.000Z",
  });
});

Deno.test("형식이 틀린 digest_date 는 null 로 저장한다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost({ ...feedbackBody("skip"), digest_date: "2026/11/16" }));
  assert.equal(res.status, 200);
  assert.equal(h.events[0].digestDate, null);
});

Deno.test("같은 표시가 60초 안에 반복되면 insert 를 건너뛰고 200", async () => {
  const h = createHarness();
  await h.handle(jsonPost(feedbackBody("want")));
  h.clock.now = new Date("2026-11-16T09:00:59.000Z");
  const res = await h.handle(jsonPost(feedbackBody("want")));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "want" });
  assert.equal(h.events.length, 1);
});

Deno.test("60초가 지나면 같은 표시도 다시 기록한다", async () => {
  const h = createHarness();
  await h.handle(jsonPost(feedbackBody("want")));
  h.clock.now = new Date("2026-11-16T09:01:00.000Z");
  await h.handle(jsonPost(feedbackBody("want")));
  assert.equal(h.events.length, 2);
});

Deno.test("want, skip, want 로 바꿨다 되돌리면 60초 안이어도 마지막 want 가 남는다", async () => {
  const h = createHarness();
  await h.handle(jsonPost(feedbackBody("want")));
  h.clock.now = new Date("2026-11-16T09:00:10.000Z");
  await h.handle(jsonPost(feedbackBody("skip")));
  h.clock.now = new Date("2026-11-16T09:00:20.000Z");
  await h.handle(jsonPost(feedbackBody("want")));
  assert.deepEqual(h.events.map((e) => e.action), ["want", "skip", "want"]);
});

Deno.test("없는 token 은 404 이고 그 밖의 정보를 주지 않는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(feedbackBody("want", "tok_does_not_exist_000000")));
  assert.equal(res.status, 404);
  assert.deepEqual(await readJson(res), { ok: false, error: "not_found" });
  assert.equal(h.events.length, 0);
});

Deno.test("해지된 구독자의 want, skip 은 409", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(feedbackBody("skip", GONE_TOKEN)));
  assert.equal(res.status, 409);
  assert.deepEqual(await readJson(res), { ok: false, action: "skip", error: "unsubscribed" });
  assert.equal(h.events.length, 0);
});

Deno.test("JSON 수신거부는 상태를 바꾸고 200", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost({ token: ACTIVE_TOKEN, action: "unsubscribe" }));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "unsubscribe" });
  assert.deepEqual(h.unsubscribeCalls, ["sub-active"]);
});

Deno.test("이미 해지된 구독자의 수신거부도 200 이고 다시 쓰지 않는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost({ token: GONE_TOKEN, action: "unsubscribe" }));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "unsubscribe" });
  assert.equal(h.unsubscribeCalls.length, 0);
});

Deno.test("원클릭 수신거부: 쿼리 파라미터와 폼 본문, Origin 없음", async () => {
  const h = createHarness({ allowedOrigin: ORIGIN });
  const res = await h.handle(
    new Request(`${ENDPOINT}?t=${ACTIVE_TOKEN}&a=unsubscribe`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "List-Unsubscribe=One-Click",
    }),
  );
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "unsubscribe" });
  assert.deepEqual(h.unsubscribeCalls, ["sub-active"]);
});

Deno.test("원클릭 형태로는 want 를 넣을 수 없다", async () => {
  const h = createHarness();
  const url = `${ENDPOINT}?t=${ACTIVE_TOKEN}&a=want&u=${encodeURIComponent(ITEM_URL)}`;
  const res = await h.handle(
    new Request(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "List-Unsubscribe=One-Click",
    }),
  );
  assert.equal(res.status, 400);
  assert.equal(h.events.length, 0);
});

Deno.test("ALLOWED_ORIGIN 이 있을 때 다른 출처의 want 는 403, 수신거부는 통과", async () => {
  const h = createHarness({ allowedOrigin: ORIGIN });
  const blocked = await h.handle(
    jsonPost(feedbackBody("want"), { Origin: "https://evil.example" }),
  );
  assert.equal(blocked.status, 403);
  assert.deepEqual(await readJson(blocked), { ok: false, error: "origin_not_allowed" });
  assert.equal(h.events.length, 0);

  const allowed = await h.handle(jsonPost(feedbackBody("want"), { Origin: ORIGIN }));
  assert.equal(allowed.status, 200);

  const noOrigin = await h.handle(jsonPost(feedbackBody("skip")));
  assert.equal(noOrigin.status, 200);

  const unsubscribe = await h.handle(
    jsonPost({ token: ACTIVE_TOKEN, action: "unsubscribe" }, { Origin: "https://evil.example" }),
  );
  assert.equal(unsubscribe.status, 200);
});

Deno.test("본문이 상한을 넘으면 413 이고 저장소를 보지 않는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost({ ...feedbackBody("want"), pad: "x".repeat(MAX_BODY_BYTES) }));
  assert.equal(res.status, 413);
  assert.deepEqual(await readJson(res), { ok: false, error: "payload_too_large" });
  assert.equal(h.events.length, 0);
});

Deno.test("깨진 JSON 은 400", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost("{not json"));
  assert.equal(res.status, 400);
  assert.deepEqual(await readJson(res), { ok: false, error: "invalid_json" });
});

Deno.test("잘못된 item_url 은 token 조회 전에 400", async () => {
  const h = createHarness({ failAt: "find_subscriber" });
  const res = await h.handle(jsonPost({ ...feedbackBody("want"), item_url: "javascript:alert(1)" }));
  assert.equal(res.status, 400);
  assert.deepEqual(await readJson(res), { ok: false, error: "invalid_item_url" });
  assert.equal(h.logs.length, 0);
});

Deno.test("저장소 오류는 500 이고 로그에는 단계와 코드만 남는다", async () => {
  const h = createHarness({ failAt: "insert_event" });
  const res = await h.handle(jsonPost(feedbackBody("want")));
  assert.equal(res.status, 500);
  assert.deepEqual(await readJson(res), { ok: false, error: "server_error" });
  assert.deepEqual(h.logs, ["insert_event:XX000"]);
});

Deno.test("모든 응답은 JSON 이고 캐시하지 않는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(feedbackBody("want")));
  assert.match(res.headers.get("content-type") ?? "", /^application\/json/);
  assert.equal(res.headers.get("cache-control"), "no-store");
});

// 여기부터 원문 클릭(action: "click").

function clickBody(itemUrl: string = ITEM_URL, token: string = ACTIVE_TOKEN): Record<string, string> {
  return { token, action: "click", item_url: itemUrl, digest_date: "2026-11-16" };
}

Deno.test("click: 보낸 적 있는 글이면 click_events 에 한 행을 넣고 200", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(clickBody()));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "click" });
  assert.deepEqual(h.clicks, [{
    subscriberId: "sub-active",
    itemUrl: ITEM_URL,
    digestDate: "2026-11-16",
    createdAt: "2026-11-16T09:00:00.000Z",
  }]);
  assert.equal(h.events.length, 0);
});

Deno.test("click: 보낸 적 없는 주소는 404 이고 기록하지 않는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(clickBody(UNSENT_URL)));
  assert.equal(res.status, 404);
  assert.deepEqual(await readJson(res), { ok: false, error: "not_found" });
  assert.equal(h.clicks.length, 0);
});

Deno.test("click: 다른 구독자에게만 보낸 글은 404", async () => {
  const h = createHarness();
  h.sentItems.clear();
  h.sentItems.add(`sub-gone\n${ITEM_URL}`);
  const res = await h.handle(jsonPost(clickBody()));
  assert.equal(res.status, 404);
  assert.equal(h.clicks.length, 0);
});

Deno.test("click: 주소는 글자 그대로 비교한다(쿼리가 붙으면 다른 글)", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(clickBody(`${ITEM_URL}?next=${encodeURIComponent(UNSENT_URL)}`)));
  assert.equal(res.status, 404);
  assert.equal(h.clicks.length, 0);
});

Deno.test("click: 없는 token 은 404 이고 발송 이력을 보지 않는다", async () => {
  const h = createHarness({ failAt: "find_sent_item" });
  const res = await h.handle(jsonPost(clickBody(ITEM_URL, "tok_does_not_exist_000000")));
  assert.equal(res.status, 404);
  assert.deepEqual(await readJson(res), { ok: false, error: "not_found" });
  assert.equal(h.logs.length, 0);
});

Deno.test("click: 해지된 구독자는 200 이지만 기록하지 않는다", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(clickBody(ITEM_URL, GONE_TOKEN)));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "click" });
  assert.equal(h.clicks.length, 0);
});

Deno.test("click: 해지된 구독자도 보낸 적 없는 주소는 404", async () => {
  const h = createHarness();
  const res = await h.handle(jsonPost(clickBody(UNSENT_URL, GONE_TOKEN)));
  assert.equal(res.status, 404);
  assert.equal(h.clicks.length, 0);
});

Deno.test("click: 10분 안에 반복되면 기록을 건너뛰고 200", async () => {
  const h = createHarness();
  await h.handle(jsonPost(clickBody()));
  h.clock.now = new Date("2026-11-16T09:09:59.000Z");
  const res = await h.handle(jsonPost(clickBody()));
  assert.equal(res.status, 200);
  assert.deepEqual(await readJson(res), { ok: true, action: "click" });
  assert.equal(h.clicks.length, 1);
});

Deno.test("click: 10분이 지나면 다시 기록한다", async () => {
  const h = createHarness();
  await h.handle(jsonPost(clickBody()));
  h.clock.now = new Date("2026-11-16T09:10:00.000Z");
  await h.handle(jsonPost(clickBody()));
  assert.equal(h.clicks.length, 2);
});

Deno.test("click: 다른 글의 클릭은 중복 판정에 걸리지 않는다", async () => {
  const h = createHarness();
  const other = "https://example.com/other";
  h.sentItems.add(`sub-active\n${other}`);
  await h.handle(jsonPost(clickBody()));
  await h.handle(jsonPost(clickBody(other)));
  assert.deepEqual(h.clicks.map((c) => c.itemUrl), [ITEM_URL, other]);
});

Deno.test("click: 잘못된 item_url 은 token 조회 전에 400", async () => {
  const h = createHarness({ failAt: "find_subscriber" });
  const res = await h.handle(jsonPost(clickBody("javascript:alert(1)")));
  assert.equal(res.status, 400);
  assert.deepEqual(await readJson(res), { ok: false, error: "invalid_item_url" });
  assert.equal(h.logs.length, 0);
});

Deno.test("click: 쿼리 파라미터 형태로는 넣을 수 없다", async () => {
  const h = createHarness();
  const url = `${ENDPOINT}?t=${ACTIVE_TOKEN}&a=click&u=${encodeURIComponent(ITEM_URL)}`;
  const res = await h.handle(
    new Request(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "x=1",
    }),
  );
  assert.equal(res.status, 400);
  assert.deepEqual(await readJson(res), { ok: false, error: "invalid_action" });
  assert.equal(h.clicks.length, 0);
});

Deno.test("click: ALLOWED_ORIGIN 이 있을 때 다른 출처는 403, 같은 출처와 Origin 없음은 통과", async () => {
  const h = createHarness({ allowedOrigin: ORIGIN });
  const blocked = await h.handle(jsonPost(clickBody(), { Origin: "https://evil.example" }));
  assert.equal(blocked.status, 403);
  assert.deepEqual(await readJson(blocked), { ok: false, error: "origin_not_allowed" });
  assert.equal(h.clicks.length, 0);

  const allowed = await h.handle(jsonPost(clickBody(), { Origin: ORIGIN }));
  assert.equal(allowed.status, 200);
  assert.equal(h.clicks.length, 1);
});

Deno.test("click: 저장소 오류는 500 이고 로그에는 단계와 코드만 남는다", async () => {
  const h = createHarness({ failAt: "insert_click" });
  const res = await h.handle(jsonPost(clickBody()));
  assert.equal(res.status, 500);
  assert.deepEqual(await readJson(res), { ok: false, error: "server_error" });
  assert.deepEqual(h.logs, ["insert_click:XX000"]);
});

Deno.test("click 은 feedback_events 를, want 는 click_events 를 건드리지 않는다", async () => {
  const h = createHarness();
  await h.handle(jsonPost(clickBody()));
  await h.handle(jsonPost(feedbackBody("want")));
  assert.equal(h.clicks.length, 1);
  assert.equal(h.events.length, 1);
});
