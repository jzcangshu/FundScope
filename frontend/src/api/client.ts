import { getServerOrigin } from "@/app/runtime";

const API_PREFIX = "/api/v1";

// Business data must pass server authorization on every read. Never serve a
// persistent payload before authorization, including during network failures.
// Keep legacy cleanup for upgrades from clients that cached across accounts.
export const CACHE_KEY_PREFIX = "fund-dashboard.cache.v2:";

function apiUrl(path: string): string {
  return `${getServerOrigin()}${API_PREFIX}${path}`;
}

export async function clearReadCache(): Promise<void> {
  try {
    Object.keys(window.localStorage)
      .filter((key) => key.startsWith("fund-dashboard.cache."))
      .forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // Disabled storage must not prevent login or authorized requests.
  }
  if (typeof indexedDB !== "undefined") {
    try { indexedDB.deleteDatabase("fund-dashboard-cache"); } catch { /* best effort */ }
  }
}

type UnauthorizedHandler = ((requestGeneration: number) => void) | null;

let unauthorizedHandler: UnauthorizedHandler = null;
let sessionGeneration = 0;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface ApiRequestOptions extends Omit<RequestInit, "body"> {
  body?: BodyInit | object | null;
  cacheMode?: "default" | "reload";
}

export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  unauthorizedHandler = handler;
}

export function getSessionGeneration(): number {
  return sessionGeneration;
}

export function advanceSessionGeneration(): number {
  sessionGeneration += 1;
  void clearReadCache();
  return sessionGeneration;
}

function isBodyInit(body: unknown): body is BodyInit {
  return (
    typeof body === "string" ||
    body instanceof Blob ||
    body instanceof FormData ||
    body instanceof URLSearchParams ||
    body instanceof ArrayBuffer ||
    ArrayBuffer.isView(body) ||
    (typeof ReadableStream !== "undefined" && body instanceof ReadableStream)
  );
}

function errorDetail(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return null;
  const detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (!Array.isArray(detail)) return null;
  const messages = detail
    .map((item) => {
      if (!item || typeof item !== "object" || !("msg" in item)) return null;
      return typeof item.msg === "string" ? item.msg.trim() : null;
    })
    .filter((message): message is string => Boolean(message));
  return messages.length ? messages.join("；") : null;
}

async function parseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  return response.ok ? response.blob() : null;
}

export async function apiRequest<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const requestGeneration = sessionGeneration;
  const headers = new Headers(options.headers);
  let body: BodyInit | null | undefined;
  if (options.body == null || isBodyInit(options.body)) {
    body = options.body;
  } else {
    headers.set("content-type", "application/json");
    body = JSON.stringify(options.body);
  }

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 120000);
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...options,
      body,
      credentials: "include",
      cache: "no-store",
      headers,
      signal: options.signal ?? controller.signal,
    });
  } finally {
    window.clearTimeout(timeout);
  }

  if (requestGeneration !== sessionGeneration) throw new DOMException("登录状态已变化，请重新加载", "AbortError");
  if (response.status === 204) return undefined as T;
  const payload = await parseBody(response);
  if (requestGeneration !== sessionGeneration) throw new DOMException("登录状态已变化，请重新加载", "AbortError");
  if (!response.ok) {
    if (response.status === 401) unauthorizedHandler?.(requestGeneration);
    throw new ApiError(
      response.status,
      errorDetail(payload) ?? `请求失败（${response.status}）`,
    );
  }
  return payload as T;
}

export function isApiError(error: unknown, status?: number): error is ApiError {
  return error instanceof ApiError && (status === undefined || error.status === status);
}

export function apiErrorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}
