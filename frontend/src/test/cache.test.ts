import { afterEach, describe, expect, it, vi } from "vitest";
import { apiRequest, advanceSessionGeneration, CACHE_KEY_PREFIX, clearReadCache } from "@/api/client";

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
}

describe("authorized reads", () => {
  afterEach(async () => { vi.restoreAllMocks(); await clearReadCache(); });

  it.each(["/dashboard/overview", "/dashboard/series", "/dashboard/fund-drawdowns", "/funds", "/funds/1/positions", "/risk/events"])("reauthorizes %s instead of serving another user's legacy cache", async (path) => {
    window.localStorage.setItem(`${CACHE_KEY_PREFIX}${path}`, JSON.stringify({ key: path, storedAt: Date.now(), payload: { data: { secret: "other-account" } } }));
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => json({ data: { permitted: true } }));
    await expect(apiRequest(path)).resolves.toEqual({ data: { permitted: true } });
    await expect(apiRequest(path)).resolves.toEqual({ data: { permitted: true } });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenLastCalledWith(expect.any(String), expect.objectContaining({ cache: "no-store", credentials: "include" }));
  });

  it("does not fall back to previously authorized data while offline", async () => {
    const path = "/funds/1/nav-series";
    window.localStorage.setItem(`${CACHE_KEY_PREFIX}${path}`, JSON.stringify({ storedAt: Date.now(), payload: { data: { old: true } } }));
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    await expect(apiRequest(path)).rejects.toThrow("offline");
    await expect(apiRequest(path, { cacheMode: "reload" })).rejects.toThrow("offline");
  });

  it("rejects revoked access without exposing cached data", async () => {
    const path = "/dashboard/overview";
    window.localStorage.setItem(`${CACHE_KEY_PREFIX}${path}`, JSON.stringify({ storedAt: Date.now(), payload: { data: { old: true } } }));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ detail: "Forbidden" }, 403));
    await expect(apiRequest(path)).rejects.toMatchObject({ status: 403 });
  });

  it("discards successful in-flight responses after a different account logs in", async () => {
    let resolve!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise((done) => { resolve = done; }));
    const pending = apiRequest("/dashboard/overview");
    advanceSessionGeneration();
    resolve(json({ data: { secret: "previous-account" } }));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(window.localStorage.getItem(`${CACHE_KEY_PREFIX}/dashboard/overview`)).toBeNull();
  });

  it("also discards a response when the account changes during JSON parsing", async () => {
    let resolve!: (data: unknown) => void;
    const response = json({});
    vi.spyOn(response, "json").mockImplementation(() => new Promise((done) => { resolve = done; }));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response);
    const pending = apiRequest("/funds/1");
    await vi.waitFor(() => expect(resolve).toBeDefined());
    advanceSessionGeneration();
    resolve({ data: { secret: "previous-account" } });
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("purges old local caches when the session changes but preserves user preferences", () => {
    window.localStorage.setItem("fund-dashboard.cache.v1:/funds", "old");
    window.localStorage.setItem(`${CACHE_KEY_PREFIX}/funds`, "old");
    window.localStorage.setItem("fundscope.sidebar", "collapsed");
    advanceSessionGeneration();
    expect(window.localStorage.getItem("fund-dashboard.cache.v1:/funds")).toBeNull();
    expect(window.localStorage.getItem(`${CACHE_KEY_PREFIX}/funds`)).toBeNull();
    expect(window.localStorage.getItem("fundscope.sidebar")).toBe("collapsed");
  });
});
