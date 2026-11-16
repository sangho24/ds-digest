// digest-feedback: 구독자의 관심도 표시(want, skip), 원문 클릭, 수신거부를 받는 공개 엔드포인트.
//
// - POST, OPTIONS 만 처리한다. GET 은 405.
// - JWT 검증을 끄고 배포한다(supabase/README.md 참고). 인증 수단은 링크에 실린 token 뿐이다.
// - 요청 처리 흐름은 handler.ts, 입력 검증은 logic.ts 에 있고 이 파일은 DB 배선만 맡는다.

import { createClient } from "npm:@supabase/supabase-js@2";
import type { SupabaseClient } from "npm:@supabase/supabase-js@2";

import { createHandler, StoreError } from "./handler.ts";
import type {
  FeedbackStore,
  LatestEvent,
  NewClickEvent,
  NewFeedbackEvent,
  SubscriberRef,
} from "./handler.ts";
import { normalizeAllowedOrigin, resolveServiceKey } from "./logic.ts";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toSubscriberRef(row: unknown): SubscriberRef | null {
  if (!isRecord(row)) return null;
  if (typeof row.id !== "string" || typeof row.status !== "string") return null;
  return { id: row.id, status: row.status };
}

function toLatestEvent(row: unknown): LatestEvent | null {
  if (!isRecord(row)) return null;
  if (typeof row.action !== "string" || typeof row.created_at !== "string") return null;
  return { action: row.action, createdAt: row.created_at };
}

let cachedClient: SupabaseClient | null = null;

/** service role 키로 클라이언트를 만든다. 요청마다 새로 만들지 않고 재사용한다. */
function getClient(): SupabaseClient {
  if (cachedClient !== null) return cachedClient;
  const url = Deno.env.get("SUPABASE_URL");
  const key = resolveServiceKey((name) => Deno.env.get(name));
  if (url === undefined || url.length === 0 || key === null) {
    throw new StoreError("init", "missing_env");
  }
  cachedClient = createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  return cachedClient;
}

const store: FeedbackStore = {
  // email 컬럼은 고르지 않는다. 함수 안으로 이메일이 들어오지 않게 하려는 것이다.
  async findSubscriberByToken(token: string): Promise<SubscriberRef | null> {
    const { data, error } = await getClient()
      .from("subscribers")
      .select("id, status")
      .eq("token", token)
      .maybeSingle();
    if (error) throw new StoreError("find_subscriber", error.code ?? "unknown");
    const row: unknown = data;
    return toSubscriberRef(row);
  },

  async findLatestEvent(subscriberId: string, itemUrl: string): Promise<LatestEvent | null> {
    const { data, error } = await getClient()
      .from("feedback_events")
      .select("action, created_at")
      .eq("subscriber_id", subscriberId)
      .eq("item_url", itemUrl)
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    if (error) throw new StoreError("find_latest_event", error.code ?? "unknown");
    const row: unknown = data;
    return toLatestEvent(row);
  },

  async insertEvent(event: NewFeedbackEvent): Promise<void> {
    const { error } = await getClient().from("feedback_events").insert({
      subscriber_id: event.subscriberId,
      item_url: event.itemUrl,
      action: event.action,
      digest_date: event.digestDate,
    });
    if (error) throw new StoreError("insert_event", error.code ?? "unknown");
  },

  async markUnsubscribed(subscriberId: string, at: Date): Promise<void> {
    // status 조건을 함께 걸어, 동시에 두 번 들어와도 해지 시각이 덮이지 않게 한다.
    const { error } = await getClient()
      .from("subscribers")
      .update({ status: "unsubscribed", unsubscribed_at: at.toISOString() })
      .eq("id", subscriberId)
      .eq("status", "active");
    if (error) throw new StoreError("mark_unsubscribed", error.code ?? "unknown");
  },

  async hasSentItem(subscriberId: string, itemUrl: string): Promise<boolean> {
    // (subscriber_id, item_url) 이 기본 키라 많아야 한 행이다.
    const { data, error } = await getClient()
      .from("sent_items")
      .select("subscriber_id")
      .eq("subscriber_id", subscriberId)
      .eq("item_url", itemUrl)
      .limit(1)
      .maybeSingle();
    if (error) throw new StoreError("find_sent_item", error.code ?? "unknown");
    return data !== null;
  },

  async findLatestClickAt(subscriberId: string, itemUrl: string): Promise<string | null> {
    const { data, error } = await getClient()
      .from("click_events")
      .select("created_at")
      .eq("subscriber_id", subscriberId)
      .eq("item_url", itemUrl)
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    if (error) throw new StoreError("find_latest_click", error.code ?? "unknown");
    const row: unknown = data;
    return isRecord(row) && typeof row.created_at === "string" ? row.created_at : null;
  },

  async insertClick(event: NewClickEvent): Promise<void> {
    const { error } = await getClient().from("click_events").insert({
      subscriber_id: event.subscriberId,
      item_url: event.itemUrl,
      digest_date: event.digestDate,
    });
    if (error) throw new StoreError("insert_click", error.code ?? "unknown");
  },
};

const handler = createHandler({
  store,
  allowedOrigin: normalizeAllowedOrigin(Deno.env.get("ALLOWED_ORIGIN")),
  now: () => new Date(),
  // 단계 이름과 오류 코드만 남긴다. token, 이메일, URL 은 로그에 쓰지 않는다.
  logError: (step, code) => console.error(`digest-feedback error step=${step} code=${code}`),
});

Deno.serve(handler);
