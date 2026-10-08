const SERVER_ORIGIN_KEY = "fund-dashboard.server-origin";
const DEFAULT_SERVER_ORIGIN = "https://fundscope.example.com";

export function isDesktopRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function getServerOrigin(): string {
  if (!isDesktopRuntime()) return "";
  try {
    const configured = window.localStorage.getItem(SERVER_ORIGIN_KEY)?.trim();
    if (configured) return new URL(configured).origin;
  } catch {
    // Fall through to the production default when storage contains bad data.
  }
  return DEFAULT_SERVER_ORIGIN;
}

export function setServerOrigin(origin: string): void {
  const normalized = new URL(origin.trim()).origin;
  if (!/^https?:$/.test(new URL(normalized).protocol)) throw new Error("服务器地址必须使用 HTTP 或 HTTPS");
  window.localStorage.setItem(SERVER_ORIGIN_KEY, normalized);
}

export function getDesktopServerOriginKey(): string {
  return SERVER_ORIGIN_KEY;
}
