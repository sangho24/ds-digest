// HTTP 요청 처리 흐름. 저장소(FeedbackStore)를 주입받으므로 가짜 저장소로 테스트할 수 있다.
// 이메일 주소는 조회하지도, 응답이나 로그에 남기지도 않는다.

import {
  buildCorsHeaders,
  extractRawInput,
  isDuplicateClick,
  isDuplicateEvent,
  isOriginAllowed,
  MAX_BODY_BYTES,
  validateInput,
} from "./logic.ts";
import type { ErrorCode, FeedbackAction, RequestAction } from "./logic.ts";

export interface SubscriberRef {
  id: string;
  status: string;
}

export interface LatestEvent {
  action: string;
  createdAt: string;
}

export interface NewFeedbackEvent {
  subscriberId: string;
  itemUrl: string;
  action: FeedbackAction;
  digestDate: string | null;
}

export interface NewClickEvent {
  subscriberId: string;
  itemUrl: string;
  digestDate: string | null;
}

/** DB 접근 계약. 실패하면 StoreError 를 던진다. */
export interface FeedbackStore {
  findSubscriberByToken(token: string): Promise<SubscriberRef | null>;
  findLatestEvent(subscriberId: string, itemUrl: string): Promise<LatestEvent | null>;
  insertEvent(event: NewFeedbackEvent): Promise<void>;
  markUnsubscribed(subscriberId: string, at: Date): Promise<void>;
  /** 이 구독자에게 이 글을 보낸 적이 있는지(sent_items). 주소는 글자 그대로 비교한다. */
  hasSentItem(subscriberId: string, itemUrl: string): Promise<boolean>;
  /** 이 구독자가 이 글을 마지막으로 누른 시각(ISO 문자열). 없으면 null. */
  findLatestClickAt(subscriberId: string, itemUrl: string): Promise<string | null>;
  insertClick(event: NewClickEvent): Promise<void>;
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

export interface HandlerDeps {
  store: FeedbackStore;
  /** 허용할 출처. null 이면 모든 출처("*"). */
  allowedOrigin: string | null;
  now: () => Date;
  /** 단계 이름과 오류 코드만 받는다. 개인정보를 넘기지 않는다. */
  logError: (step: string, code: string) => void;
}

interface ResponseBody {
  ok: boolean;
  action?: RequestAction;
  error?: ErrorCode;
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

export function createHandler(deps: HandlerDeps): (req: Request) => Promise<Response> {
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
    // GET 을 포함한 나머지는 전부 거절한다. 메일 보안 스캐너가 링크를 미리 열어도
    // 아무 일도 일어나지 않게 하려는 설계다.
    if (req.method !== "POST") {
      return respond(405, { ok: false, error: "method_not_allowed" }, { Allow: "POST, OPTIONS" });
    }

    try {
      const bodyText = await readLimitedBody(req, MAX_BODY_BYTES);
      if (bodyText === null) {
        return respond(413, { ok: false, error: "payload_too_large" });
      }

      const raw = extractRawInput(
        req.headers.get("content-type"),
        bodyText,
        new URL(req.url).searchParams,
      );
      if (!raw.ok) return respond(raw.status, { ok: false, error: raw.error });

      const parsed = validateInput(raw.value);
      if (!parsed.ok) return respond(parsed.status, { ok: false, error: parsed.error });
      const command = parsed.value;

      // 수신거부는 출처를 따지지 않는다(메일 서버, 메일 앱 웹뷰 어디서 오든 받아야 한다).
      // 관심도 표시와 클릭은 브라우저가 보낸 Origin 이 허용 대상일 때만 받는다.
      if (
        command.kind !== "unsubscribe" &&
        !isOriginAllowed(req.headers.get("origin"), deps.allowedOrigin)
      ) {
        return respond(403, { ok: false, error: "origin_not_allowed" });
      }

      const subscriber = await deps.store.findSubscriberByToken(command.token);
      if (subscriber === null) {
        return respond(404, { ok: false, error: "not_found" });
      }

      if (command.kind === "unsubscribe") {
        // 이미 해지된 경우에도 200 을 준다(멱등). 해지 시각은 처음 것을 유지한다.
        if (subscriber.status !== "unsubscribed") {
          await deps.store.markUnsubscribed(subscriber.id, deps.now());
        }
        return respond(200, { ok: true, action: "unsubscribe" });
      }

      if (command.kind === "click") {
        // 보낸 적 없는 주소는 "없는 것"으로 답한다. go.html 은 ok 일 때만 자동으로 이동하므로,
        // 이 확인이 임의 주소로 넘기는 열린 리다이렉트로 쓰이는 일을 막는다.
        const wasSent = await deps.store.hasSentItem(subscriber.id, command.itemUrl);
        if (!wasSent) {
          return respond(404, { ok: false, error: "not_found" });
        }
        // 해지한 뒤에도 예전 메일의 원문 링크는 열려야 한다. 이동만 허용하고 기록은 남기지 않는다.
        if (subscriber.status === "unsubscribed") {
          return respond(200, { ok: true, action: "click" });
        }
        // 새로고침, 뒤로 가기, 연타로 같은 클릭이 반복되면 insert 를 건너뛴다.
        const latestClickAt = await deps.store.findLatestClickAt(subscriber.id, command.itemUrl);
        if (!isDuplicateClick(latestClickAt, deps.now())) {
          await deps.store.insertClick({
            subscriberId: subscriber.id,
            itemUrl: command.itemUrl,
            digestDate: command.digestDate,
          });
        }
        return respond(200, { ok: true, action: "click" });
      }

      if (subscriber.status === "unsubscribed") {
        return respond(409, { ok: false, action: command.action, error: "unsubscribed" });
      }

      // 새로고침이나 연타로 같은 표시가 반복되면 insert 를 건너뛴다.
      // "직전 표시"와 비교하므로 want, skip, want 처럼 바꿨다 되돌린 경우는 정상 기록된다.
      const latest = await deps.store.findLatestEvent(subscriber.id, command.itemUrl);
      if (!isDuplicateEvent(latest, command.action, deps.now())) {
        await deps.store.insertEvent({
          subscriberId: subscriber.id,
          itemUrl: command.itemUrl,
          action: command.action,
          digestDate: command.digestDate,
        });
      }
      return respond(200, { ok: true, action: command.action });
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
