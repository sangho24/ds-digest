// 가입 입력 검증과 초대 코드 비교만 담는 순수 로직. DB, 네트워크, Deno 전역에 의존하지 않는다.
// (crypto 는 Deno 와 브라우저가 함께 가진 웹 표준 전역이다.)
//
// CORS, 서비스 키 고르기는 digest-feedback/logic.ts 와 같은 규칙이다. 함수 하나가 자기 폴더만으로
// 배포되도록 일부러 복사해 두었다. 한쪽을 고치면 다른 쪽도 같이 고친다.

/**
 * 요청 본문 크기 상한(바이트). 이메일 254자, 이름 40자, 초대 코드, 문체 요청 200자를 담고도 남는다
 * (문체 요청이 전부 4바이트 글자여도 800바이트다).
 */
export const MAX_BODY_BYTES = 2048;
/** 이메일 전체 길이 상한. */
export const MAX_EMAIL_LENGTH = 254;
/** 이메일 @ 앞부분 길이 상한. */
export const MAX_EMAIL_LOCAL_LENGTH = 64;
/** 이름(별명) 글자 수 상한. */
export const MAX_NAME_LENGTH = 40;
/** 문체 요청(style_note) 글자 수 상한. DB 의 check 제약과 같은 값이다. */
export const MAX_STYLE_NOTE_LENGTH = 200;
/** MAX_SUBSCRIBERS 가 없거나 숫자가 아닐 때 쓰는 정원. */
export const DEFAULT_MAX_SUBSCRIBERS = 30;
/** token 을 만들 때 쓰는 난수 바이트 수. base64url 로 43자가 된다. */
export const TOKEN_BYTES = 32;

/** 응답의 error 필드에 들어가는 값. 가입 페이지가 이 값으로 안내 문구를 고른다. */
export type JoinErrorCode =
  | "method_not_allowed"
  | "origin_not_allowed"
  | "join_closed"
  | "payload_too_large"
  | "invalid_json"
  | "invalid_code"
  | "consent_required"
  | "invalid_email"
  | "invalid_name"
  | "invalid_style_note"
  | "full"
  | "server_error";

export type Result<T> =
  | { ok: true; value: T }
  | { ok: false; status: number; error: JoinErrorCode };

/** 본문에서 꺼낸 그대로의 값. 아직 아무것도 믿지 않는다. */
export interface RawJoinInput {
  email: unknown;
  name: unknown;
  inviteCode: unknown;
  consent: unknown;
  /** 선택 필드. 본문에 style_note 가 있을 때만 담긴다. */
  styleNote?: unknown;
}

/** 검증을 통과한 가입 신청. 초대 코드는 여기에 담지 않는다(비교가 끝나면 버린다). */
export interface JoinApplication {
  email: string;
  name: string;
}

// 영문 소문자, 숫자와 흔한 기호만 받는다. 줄바꿈이나 따옴표가 섞인 값이 메일 헤더로
// 흘러가지 않게 하려고 일부러 좁게 잡았다.
const EMAIL_PATTERN = /^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$/;
// 제어 문자(줄바꿈, 탭 포함)가 들어간 이름은 받지 않는다.
// deno-lint-ignore no-control-regex
const CONTROL_CHAR_PATTERN = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/;
// 문체 요청은 거절하지 않고 걸러 낸다. 줄바꿈과 탭은 낱말이 붙지 않게 공백으로 바꾸고 나머지는 지운다.
// deno-lint-ignore no-control-regex
const LINE_BREAK_PATTERN = /[\t\n\u000b\u000c\r\u0085\u2028\u2029]+/g;
// deno-lint-ignore no-control-regex
const CONTROL_CHAR_GLOBAL_PATTERN = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g;

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
 * JSON 본문에서 원시 입력을 꺼낸다. JSON 이 아닌 본문은 받지 않는다
 * (단순 폼 요청으로 다른 사이트가 가입을 대신 넣는 일을 막는다).
 */
export function extractJoinInput(contentType: string | null, bodyText: string): Result<RawJoinInput> {
  if (!isJsonContentType(contentType)) {
    return { ok: false, status: 400, error: "invalid_json" };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    return { ok: false, status: 400, error: "invalid_json" };
  }
  if (!isRecord(parsed)) {
    return { ok: false, status: 400, error: "invalid_json" };
  }
  const value: RawJoinInput = {
    email: parsed.email,
    name: parsed.name,
    inviteCode: parsed.invite_code,
    consent: parsed.consent,
  };
  if (parsed.style_note !== undefined) value.styleNote = parsed.style_note;
  return { ok: true, value };
}

/** 앞뒤 공백을 떼고 소문자로 바꾼 뒤 형식을 본다. 통과하면 정리한 값을, 아니면 null. */
export function normalizeEmail(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const email = value.trim().toLowerCase();
  if (email.length === 0 || email.length > MAX_EMAIL_LENGTH) return null;
  if (!EMAIL_PATTERN.test(email)) return null;
  const at = email.indexOf("@");
  if (at > MAX_EMAIL_LOCAL_LENGTH) return null;
  if (email.includes("..") || email.startsWith(".") || email[at - 1] === ".") return null;
  return email;
}

/** 앞뒤 공백을 떼고 1자에서 40자 사이인지 본다(글자 수는 코드 포인트 기준). */
export function normalizeName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const name = value.trim();
  if (CONTROL_CHAR_PATTERN.test(name)) return null;
  const length = Array.from(name).length;
  if (length < 1 || length > MAX_NAME_LENGTH) return null;
  return name;
}

/**
 * 문체 요청(선택 입력)을 정리한다. validateApplication 을 통과한 뒤에 부른다.
 * - 없음, null, 정리하고 나니 빈 글: null(요청 없음)
 * - 제어 문자는 걸러 내고 앞뒤 공백을 뗀다. 줄바꿈과 탭은 공백 한 칸이 된다.
 * - 문자열이 아니거나 정리한 뒤에도 200자를 넘으면 400(글자 수는 코드 포인트 기준).
 */
export function normalizeStyleNote(value: unknown): Result<string | null> {
  if (value === undefined || value === null) return { ok: true, value: null };
  if (typeof value !== "string") {
    return { ok: false, status: 400, error: "invalid_style_note" };
  }
  const note = value
    .replace(LINE_BREAK_PATTERN, " ")
    .replace(CONTROL_CHAR_GLOBAL_PATTERN, "")
    .trim();
  if (note.length === 0) return { ok: true, value: null };
  if (Array.from(note).length > MAX_STYLE_NOTE_LENGTH) {
    return { ok: false, status: 400, error: "invalid_style_note" };
  }
  return { ok: true, value: note };
}

/**
 * 초대 코드가 맞은 뒤에 부르는 검증. 동의, 이메일, 이름 순서로 본다.
 * 초대 코드 비교는 비동기라 handler 가 따로 먼저 한다.
 */
export function validateApplication(raw: RawJoinInput): Result<JoinApplication> {
  // true 그 자체만 동의로 친다. "true", 1 같은 값은 받지 않는다.
  if (raw.consent !== true) {
    return { ok: false, status: 400, error: "consent_required" };
  }
  const email = normalizeEmail(raw.email);
  if (email === null) {
    return { ok: false, status: 400, error: "invalid_email" };
  }
  const name = normalizeName(raw.name);
  if (name === null) {
    return { ok: false, status: 400, error: "invalid_name" };
  }
  return { ok: true, value: { email, name } };
}

/** INVITE_CODE 시크릿을 정리한다. 없거나 공백뿐이면 null(가입을 받지 않는다). */
export function normalizeInviteCode(value: string | undefined): string | null {
  if (value === undefined) return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** MAX_SUBSCRIBERS 를 정수로 읽는다. 0 이상의 정수가 아니면 기본값 30. */
export function parseMaxSubscribers(value: string | undefined): number {
  if (value === undefined) return DEFAULT_MAX_SUBSCRIBERS;
  const trimmed = value.trim();
  if (!/^\d{1,6}$/.test(trimmed)) return DEFAULT_MAX_SUBSCRIBERS;
  return Number(trimmed);
}

async function sha256(text: string): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return new Uint8Array(digest);
}

/**
 * 두 문자열을 상수 시간으로 비교한다.
 * 둘 다 SHA-256 으로 길이를 32바이트로 맞춘 뒤, 중간에 멈추지 않고 끝까지 XOR 해서 본다.
 * 그래서 걸린 시간으로 "몇 글자까지 맞았는지"나 코드 길이를 알아낼 수 없다.
 */
export async function constantTimeEqual(a: string, b: string): Promise<boolean> {
  const [left, right] = await Promise.all([sha256(a), sha256(b)]);
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) {
    diff |= left[i] ^ right[i];
  }
  return diff === 0;
}

/** 제출된 초대 코드가 시크릿과 같은지 본다. 문자열이 아니거나 비어 있으면 틀린 것이다. */
export async function isInviteCodeValid(submitted: unknown, expected: string): Promise<boolean> {
  // 문자열이 아닐 때도 비교는 한 번 돌린다(응답 시간이 입력 종류에 따라 갈리지 않게).
  const candidate = typeof submitted === "string" ? submitted.trim() : "";
  const matched = await constantTimeEqual(candidate, expected);
  return matched && candidate.length > 0;
}

/** 바이트 배열을 채움 문자 없는 base64url 로 바꾼다. */
export function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** 메일 링크에 실릴 구독자 token. 32바이트 난수라 추측할 수 없다. */
export function generateToken(): string {
  const bytes = new Uint8Array(TOKEN_BYTES);
  crypto.getRandomValues(bytes);
  return toBase64Url(bytes);
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
 * Origin 헤더가 없는 요청(curl)은 통과시킨다. 그런 요청도 초대 코드는 맞아야 한다.
 */
export function isOriginAllowed(origin: string | null, allowedOrigin: string | null): boolean {
  if (allowedOrigin === null || origin === null) return true;
  return origin === allowedOrigin;
}

/**
 * DB 접근에 쓸 키를 고른다.
 * SUPABASE_SERVICE_ROLE_KEY 가 우선이고, 없으면 새 방식인 SUPABASE_SECRET_KEYS(JSON 사전)의
 * default 항목을 쓴다.
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
