// 가입 요청 처리 흐름. 저장소(JoinStore)를 주입받으므로 가짜 저장소로 테스트할 수 있다.
// 이메일과 초대 코드는 응답에도 로그에도 남기지 않는다.

import {
  buildCorsHeaders,
  extractJoinInput,
  isInviteCodeValid,
  isOriginAllowed,
  MAX_BODY_BYTES,
  validateApplication,
} from "./logic.ts";
import type { JoinErrorCode } from "./logic.ts";

/** 이미 있는 구독자에 대해 가입 흐름이 알아야 하는 것. is_owner 는 읽지도 않는다. */
export interface ExistingSubscriber {
  id: string;
  status: string;
}

export interface NewSubscriber {
  email: string;
  name: string;
  token: string;
  consentedAt: Date;
}

/** DB 접근 계약. 실패하면 StoreError 를 던진다. */
export interface JoinStore {
  /** status 가 active 인 구독자 수. */
  countActiveSubscribers(): Promise<number>;
  findSubscriberByEmail(email: string): Promise<ExistingSubscriber | null>;
  /**
   * status active, signup_source invite 로 새 행을 넣는다.
   * 그 사이 같은 이메일이 먼저 들어와 유일 조건에 걸리면 던지지 않고 "conflict" 를 돌려준다.
   */
  insertSubscriber(subscriber: NewSubscriber): Promise<"inserted" | "conflict">;
  /** 해지 상태인 행만 active 로 되돌리고 consented_at 을 갱신한다. 다른 컬럼은 건드리지 않는다. */
  reactivateSubscriber(subscriberId: string, consentedAt: Date): Promise<void>;
}

/** 어느 단계에서 어떤 DB 오류 코드로 실패했는지만 담는다(행 내용은 담지 않는다). */
export class StoreError extends Error {
  readonly step: string;
  readonly code: string;

  constructor(step: string, code: string) {
    super(`store failed at ${step} (${code})`);
    this.name = "StoreError";
    this.step = step;
    this.code = code;
  }
}

export interface JoinHandlerDeps {
  store: JoinStore;
  /** 허용할 출처. null 이면 모든 출처("*"). */
  allowedOrigin: string | null;
  /** INVITE_CODE 시크릿. null 이면 가입을 전부 거절한다. */
  inviteCode: string | null;
  /** 활성 구독자 정원. */
  maxSubscribers: number;
  now: () => Date;
  generateToken: () => string;
  /** 단계 이름과 오류 코드만 받는다. 개인정보를 넘기지 않는다. */
  logError: (step: string, code: string) => void;
}

interface ResponseBody {
  ok: boolean;
  error?: JoinErrorCode;
  message?: string;
}

/** 본문을 상한까지만 읽는다. 상한을 넘으면 null. */
async function readLimitedBody(req: Request, maxBytes: number): Promise<string | null> {
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > maxBytes) return null;
  if (req.body === null) return "";

  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(merged);
}

export function createHandler(deps: JoinHandlerDeps): (req: Request) => Promise<Response> {
  const corsHeaders = buildCorsHeaders(deps.allowedOrigin);

  const respond = (
    status: number,
    body: ResponseBody,
    extraHeaders: Record<string, string> = {},
  ): Response =>
    new Response(JSON.stringify(body), {
      status,
      headers: {
        ...corsHeaders,
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        ...extraHeaders,
      },
    });

  return async (req: Request): Promise<Response> => {
    // CORS preflight. 본문 없이 헤더만 돌려준다.
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }
    if (req.method !== "POST") {
      return respond(405, { ok: false, error: "method_not_allowed" }, { Allow: "POST, OPTIONS" });
    }

    try {
      // 다른 사이트의 브라우저가 보낸 가입은 본문을 읽기도 전에 거절한다.
      if (!isOriginAllowed(req.headers.get("origin"), deps.allowedOrigin)) {
        return respond(403, { ok: false, error: "origin_not_allowed" });
      }

      // 초대 코드 시크릿이 없으면 문을 닫아 둔다. "빈 코드끼리 일치"로 열리는 일이 없게 한다.
      if (deps.inviteCode === null) {
        return respond(503, { ok: false, error: "join_closed" });
      }

      const bodyText = await readLimitedBody(req, MAX_BODY_BYTES);
      if (bodyText === null) {
        return respond(413, { ok: false, error: "payload_too_large" });
      }

      const raw = extractJoinInput(req.headers.get("content-type"), bodyText);
      if (!raw.ok) return respond(raw.status, { ok: false, error: raw.error });

      // 코드를 다른 입력보다 먼저 본다. 코드가 없는 사람은 형식 검증 결과도 얻지 못한다.
      const codeMatches = await isInviteCodeValid(raw.value.inviteCode, deps.inviteCode);
      if (!codeMatches) {
        return respond(403, { ok: false, error: "invalid_code" });
      }

      const parsed = validateApplication(raw.value);
      if (!parsed.ok) return respond(parsed.status, { ok: false, error: parsed.error });
      const application = parsed.value;

      // 정원은 이메일을 찾아보기 전에 따진다. 정원이 찬 동안에는 이미 가입한 이메일과
      // 처음 보는 이메일이 똑같이 409 를 받으므로, 응답으로 가입 여부를 알아낼 수 없다.
      const activeCount = await deps.store.countActiveSubscribers();
      if (activeCount >= deps.maxSubscribers) {
        return respond(409, { ok: false, error: "full", message: "정원이 찼다" });
      }

      const now = deps.now();
      const existing = await deps.store.findSubscriberByEmail(application.email);
      if (existing === null) {
        // "conflict"(그 사이 같은 이메일이 먼저 들어옴)도 성공과 똑같이 답한다.
        await deps.store.insertSubscriber({
          email: application.email,
          name: application.name,
          token: deps.generateToken(),
          consentedAt: now,
        });
      } else if (existing.status === "unsubscribed") {
        await deps.store.reactivateSubscriber(existing.id, now);
      }
      // 이미 받고 있는 이메일이면 아무것도 바꾸지 않는다(이름, token, 동의 시각 모두 그대로).

      // 세 경우 모두 같은 응답이다. token 도 돌려주지 않는다(메일로만 전달된다).
      return respond(200, { ok: true });
    } catch (error) {
      if (error instanceof StoreError) {
        deps.logError(error.step, error.code);
      } else {
        deps.logError("handler", error instanceof Error ? error.name : "unknown");
      }
      return respond(500, { ok: false, error: "server_error" });
    }
  };
}
