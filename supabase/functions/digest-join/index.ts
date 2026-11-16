// digest-join: 초대 코드를 가진 사람의 가입 신청을 받는 공개 엔드포인트.
//
// - POST, OPTIONS 만 처리한다. GET 은 405.
// - JWT 검증을 끄고 배포한다(supabase/README.md 참고). 인증 수단은 초대 코드(INVITE_CODE)뿐이다.
// - 요청 처리 흐름은 handler.ts, 입력 검증은 logic.ts 에 있고 이 파일은 DB 배선만 맡는다.

import { createClient } from "npm:@supabase/supabase-js@2";
import type { SupabaseClient } from "npm:@supabase/supabase-js@2";

import { createHandler, StoreError } from "./handler.ts";
import type { ExistingSubscriber, JoinStore, NewSubscriber } from "./handler.ts";
import {
  generateToken,
  normalizeAllowedOrigin,
  normalizeInviteCode,
  parseMaxSubscribers,
  resolveServiceKey,
} from "./logic.ts";

/** Postgres 의 유일 조건 위반 코드. */
const UNIQUE_VIOLATION = "23505";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toExistingSubscriber(row: unknown): ExistingSubscriber | null {
  if (!isRecord(row)) return null;
  if (typeof row.id !== "string" || typeof row.status !== "string") return null;
  return { id: row.id, status: row.status };
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

const store: JoinStore = {
  async countActiveSubscribers(): Promise<number> {
    // head: true 라 행은 가져오지 않고 개수만 센다.
    const { count, error } = await getClient()
      .from("subscribers")
      .select("id", { count: "exact", head: true })
      .eq("status", "active");
    if (error) throw new StoreError("count_active", error.code ?? "unknown");
    if (typeof count !== "number") throw new StoreError("count_active", "no_count");
    return count;
  },

  // id 와 status 만 고른다. is_owner, token 은 읽지 않는다.
  async findSubscriberByEmail(email: string): Promise<ExistingSubscriber | null> {
    const { data, error } = await getClient()
      .from("subscribers")
      .select("id, status")
      .eq("email", email)
      .maybeSingle();
    if (error) throw new StoreError("find_subscriber", error.code ?? "unknown");
    const row: unknown = data;
    return toExistingSubscriber(row);
  },

  async insertSubscriber(subscriber: NewSubscriber): Promise<"inserted" | "conflict"> {
    // is_owner 는 적지 않는다. 컬럼 기본값(false)이 들어간다.
    const { error } = await getClient().from("subscribers").insert({
      email: subscriber.email,
      name: subscriber.name,
      token: subscriber.token,
      status: "active",
      signup_source: "invite",
      consented_at: subscriber.consentedAt.toISOString(),
    });
    if (error) {
      if (error.code === UNIQUE_VIOLATION) return "conflict";
      throw new StoreError("insert_subscriber", error.code ?? "unknown");
    }
    return "inserted";
  },

  async reactivateSubscriber(subscriberId: string, consentedAt: Date): Promise<void> {
    // status 조건을 함께 걸어, 그 사이 상태가 바뀐 행은 건드리지 않는다.
    const { error } = await getClient()
      .from("subscribers")
      .update({ status: "active", consented_at: consentedAt.toISOString() })
      .eq("id", subscriberId)
      .eq("status", "unsubscribed");
    if (error) throw new StoreError("reactivate_subscriber", error.code ?? "unknown");
  },
};

const handler = createHandler({
  store,
  allowedOrigin: normalizeAllowedOrigin(Deno.env.get("ALLOWED_ORIGIN")),
  inviteCode: normalizeInviteCode(Deno.env.get("INVITE_CODE")),
  maxSubscribers: parseMaxSubscribers(Deno.env.get("MAX_SUBSCRIBERS")),
  now: () => new Date(),
  generateToken,
  // 단계 이름과 오류 코드만 남긴다. 이메일, 이름, 초대 코드, token 은 로그에 쓰지 않는다.
  logError: (step, code) => console.error(`digest-join error step=${step} code=${code}`),
});

Deno.serve(handler);
