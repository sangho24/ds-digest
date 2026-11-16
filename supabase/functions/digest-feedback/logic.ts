// 입력 파싱과 검증만 담는 순수 로직. DB, 네트워크, Deno 전역에 의존하지 않는다.
// deno test 로 그대로 검증할 수 있게 index.ts(런타임 배선)와 분리했다.

/** 요청 본문 크기 상한(바이트). 정상 요청은 URL 2048자를 포함해도 이 안에 들어온다. */
export const MAX_BODY_BYTES = 4096;
/** item_url 길이 상한. */
export const MAX_ITEM_URL_LENGTH = 2048;
/** 같은 표시가 이 시간 안에 반복되면 insert 를 건너뛴다. */
export const DEDUP_WINDOW_SECONDS = 60;
/** 같은 글의 원문 클릭이 이 시간 안에 반복되면 기록을 건너뛴다. */
export const CLICK_DEDUP_WINDOW_SECONDS = 600;

export type FeedbackAction = "want" | "skip";
export type RequestAction = FeedbackAction | "unsubscribe" | "click";

/** 응답의 error 필드에 들어가는 값. 페이지가 이 값으로 안내 문구를 고른다. */
export type ErrorCode =
  | "method_not_allowed"
  | "payload_too_large"
  | "invalid_json"
  | "invalid_action"
  | "missing_token"
  | "not_found"
  | "invalid_item_url"
  | "origin_not_allowed"
  | "unsubscribed"
  | "server_error";

/** 입력이 어디서 왔는지. query 는 메일 서버가 보내는 원클릭 수신거부 형태다. */
export type InputSource = "json" | "query";

export interface RawInput {
  source: InputSource;
  token: unknown;
  action: unknown;
  itemUrl: unknown;
  digestDate: unknown;
}

export type ParsedCommand =
  | {
    kind: "feedback";
    token: string;
    action: FeedbackAction;
    itemUrl: string;
    digestDate: string | null;
  }
  | { kind: "click"; token: string; itemUrl: string; digestDate: string | null }
  | { kind: "unsubscribe"; token: string };

export type Result<T> =
  | { ok: true; value: T }
  | { ok: false; status: number; error: ErrorCode };

const TOKEN_PATTERN = /^[A-Za-z0-9._~-]{8,200}$/;
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Content-Type 이 JSON 인지 본다(charset 같은 매개변수는 무시). */
export function isJsonContentType(contentType: string | null): boolean {
  if (contentType === null) return false;
  const mediaType = contentType.split(";")[0].trim().toLowerCase();
  return mediaType === "application/json";
}

/**
 * 본문과 쿼리에서 원시 입력을 꺼낸다.
 * - JSON 본문이면 본문 값만 쓴다(페이지가 보내는 형태).
 * - 그 밖의 본문(예: "List-Unsubscribe=One-Click")이면 쿼리 파라미터를 쓴다.
 */
export function extractRawInput(
  contentType: string | null,
  bodyText: string,
  query: URLSearchParams,
): Result<RawInput> {
  if (isJsonContentType(contentType)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(bodyText);
    } catch {
      return { ok: false, status: 400, error: "invalid_json" };
    }
    if (!isRecord(parsed)) {
      return { ok: false, status: 400, error: "invalid_json" };
    }
    return {
      ok: true,
      value: {
        source: "json",
        token: parsed.token,
        action: parsed.action,
        itemUrl: parsed.item_url,
        digestDate: parsed.digest_date,
      },
    };
  }
  return {
    ok: true,
    value: {
      source: "query",
      token: query.get("t"),
      action: query.get("a"),
      itemUrl: query.get("u"),
      digestDate: query.get("d"),
    },
  };
}

/** token 이 링크에 실릴 수 있는 문자와 길이인지만 본다(존재 여부는 DB 가 판정). */
export function isValidTokenFormat(token: string): boolean {
  return TOKEN_PATTERN.test(token);
}

/** http, https 만 허용하고 길이 상한을 넘으면 거절한다. */
export function isValidItemUrl(value: string): boolean {
  if (value.length === 0 || value.length > MAX_ITEM_URL_LENGTH) return false;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  return url.protocol === "http:" || url.protocol === "https:";
}

/** YYYY-MM-DD 이고 실제로 있는 날짜일 때만 그대로 돌려준다. 아니면 null(저장하지 않음). */
export function normalizeDigestDate(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = DATE_PATTERN.exec(value);
  if (match === null) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  const isRealDate = date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day;
  return isRealDate ? value : null;
}

/**
 * 원시 입력을 실행할 명령으로 바꾼다. DB 를 보기 전에 걸러낼 수 있는 것은 여기서 다 거른다.
 * 쿼리 파라미터 경로는 원클릭 수신거부 전용이다. 관심도 표시와 클릭은 JSON POST 로만 받는다
 * (단순 폼 요청으로 다른 사이트가 표시를 대신 넣는 일을 막는다).
 */
export function validateInput(raw: RawInput): Result<ParsedCommand> {
  const action = raw.action;
  if (action !== "want" && action !== "skip" && action !== "unsubscribe" && action !== "click") {
    return { ok: false, status: 400, error: "invalid_action" };
  }
  if (raw.source === "query" && action !== "unsubscribe") {
    return { ok: false, status: 400, error: "invalid_action" };
  }
  if (typeof raw.token !== "string" || raw.token.length === 0) {
    return { ok: false, status: 400, error: "missing_token" };
  }
  // 형식이 틀린 token 도 "없는 token" 과 똑같이 답한다(형식 정보를 흘리지 않는다).
  if (!isValidTokenFormat(raw.token)) {
    return { ok: false, status: 404, error: "not_found" };
  }
  if (action === "unsubscribe") {
    return { ok: true, value: { kind: "unsubscribe", token: raw.token } };
  }
  if (typeof raw.itemUrl !== "string" || !isValidItemUrl(raw.itemUrl)) {
    return { ok: false, status: 400, error: "invalid_item_url" };
  }
  if (action === "click") {
    return {
      ok: true,
      value: {
        kind: "click",
        token: raw.token,
        itemUrl: raw.itemUrl,
        digestDate: normalizeDigestDate(raw.digestDate),
      },
    };
  }
  return {
    ok: true,
    value: {
      kind: "feedback",
      token: raw.token,
      action,
      itemUrl: raw.itemUrl,
      digestDate: normalizeDigestDate(raw.digestDate),
    },
  };
}

/** ALLOWED_ORIGIN 값을 정리한다. 비어 있으면 null(모든 출처 허용). */
export function normalizeAllowedOrigin(value: string | undefined): string | null {
  if (value === undefined) return null;
  const trimmed = value.trim().replace(/\/+$/, "");
  return trimmed.length > 0 ? trimmed : null;
}

/** CORS 응답 헤더. ALLOWED_ORIGIN 이 있으면 그것만, 없으면 "*". */
export function buildCorsHeaders(allowedOrigin: string | null): Record<string, string> {
  const headers: Record<string, string> = {
    "Access-Control-Allow-Origin": allowedOrigin ?? "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type, authorization, apikey, x-client-info",
    "Access-Control-Max-Age": "86400",
  };
  if (allowedOrigin !== null) headers["Vary"] = "Origin";
  return headers;
}

/**
 * 브라우저가 보낸 Origin 이 허용 대상인지 본다.
 * Origin 헤더가 없는 요청(메일 서버의 원클릭 POST, curl)은 통과시킨다.
 */
export function isOriginAllowed(origin: string | null, allowedOrigin: string | null): boolean {
  if (allowedOrigin === null || origin === null) return true;
  return origin === allowedOrigin;
}

/** 직전 표시가 같은 행동이고 60초 안이면 중복으로 본다. */
export function isDuplicateEvent(
  latest: { action: string; createdAt: string } | null,
  action: FeedbackAction,
  now: Date,
): boolean {
  if (latest === null || latest.action !== action) return false;
  const createdMs = Date.parse(latest.createdAt);
  if (Number.isNaN(createdMs)) return false;
  return now.getTime() - createdMs < DEDUP_WINDOW_SECONDS * 1000;
}

/** 같은 글의 직전 클릭 기록이 10분 안이면 중복으로 본다. */
export function isDuplicateClick(latestCreatedAt: string | null, now: Date): boolean {
  if (latestCreatedAt === null) return false;
  const createdMs = Date.parse(latestCreatedAt);
  if (Number.isNaN(createdMs)) return false;
  return now.getTime() - createdMs < CLICK_DEDUP_WINDOW_SECONDS * 1000;
}

/**
 * DB 접근에 쓸 키를 고른다.
 * SUPABASE_SERVICE_ROLE_KEY 가 우선이고, 없으면 새 방식인 SUPABASE_SECRET_KEYS(JSON 사전)의
 * default 항목을 쓴다. 레거시 키가 사라진 프로젝트에서도 함수가 살아 있게 하기 위함이다.
 */
export function resolveServiceKey(getEnv: (name: string) => string | undefined): string | null {
  const legacy = getEnv("SUPABASE_SERVICE_ROLE_KEY");
  if (legacy !== undefined && legacy.length > 0) return legacy;
  const secretKeys = getEnv("SUPABASE_SECRET_KEYS");
  if (secretKeys === undefined || secretKeys.length === 0) return null;
  try {
    const parsed: unknown = JSON.parse(secretKeys);
    if (isRecord(parsed) && typeof parsed.default === "string" && parsed.default.length > 0) {
      return parsed.default;
    }
  } catch {
    // 형식이 예상과 다르면 키가 없는 것으로 취급한다.
  }
  return null;
}
