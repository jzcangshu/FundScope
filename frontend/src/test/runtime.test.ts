import { afterEach, describe, expect, it } from "vitest";
import { getDesktopServerOriginKey, getServerOrigin, isDesktopRuntime, setServerOrigin } from "@/app/runtime";

describe("desktop runtime configuration", () => {
  afterEach(() => {
    window.localStorage.clear();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("uses same-origin API requests in the browser", () => {
    expect(isDesktopRuntime()).toBe(false);
    expect(getServerOrigin()).toBe("");
  });

  it("uses the production service by default in Tauri", () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    expect(isDesktopRuntime()).toBe(true);
    expect(getServerOrigin()).toBe("https://fundscope.example.com");
  });

  it("normalizes an explicitly configured server origin", () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    setServerOrigin("https://example.test/some/path");
    expect(window.localStorage.getItem(getDesktopServerOriginKey())).toBe("https://example.test");
    expect(getServerOrigin()).toBe("https://example.test");
  });
});
