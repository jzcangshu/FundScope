import { afterEach, describe, expect, it, vi } from "vitest";
import { compactMoney, cutoffForRange, dec, formatBytes, fullSeriesStart, returnColor, timeStr, today } from "@/utils/format";

afterEach(() => {
  vi.useRealTimers();
});

describe("timeStr", () => {
  it("converts backend UTC timestamps to Beijing time", () => {
    expect(timeStr("2026-08-27T01:30:00+00:00")).toBe("2026-08-27 09:30");
  });

  it("treats timezone-less backend timestamps as UTC", () => {
    expect(timeStr("2026-08-27T01:30:00")).toBe("2026-08-27 09:30");
  });

  it("does not disguise invalid timestamps as formatted values", () => {
    expect(timeStr("not-a-date-value")).toBe("not-a-date-value");
  });
});

describe("dec", () => {
  it("renders an empty backend value as missing", () => {
    expect(dec("")).toBe("—");
  });
});

describe("compactMoney", () => {
  it("uses readable Chinese units for large asset values", () => {
    expect(compactMoney("7992130000")).toBe("79.92 亿");
    expect(compactMoney("12000")).toBe("1.20 万");
  });
});

describe("returnColor", () => {
  it("uses red for gains and green for losses (红涨绿跌)", () => {
    expect(returnColor("0.012")).toBe("var(--up)");
    expect(returnColor("-0.012")).toBe("var(--down)");
    expect(returnColor("0")).toBe("var(--muted)");
    expect(returnColor(null)).toBe("var(--muted)");
  });
});

describe("formatBytes", () => {
  it("scales units", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
  });
});

describe("cutoffForRange", () => {
  it("anchors the window on the latest valuation date", () => {
    expect(cutoffForRange("2026-08-31", "1m")).toBe("2026-07-31");
    expect(cutoffForRange("2026-03-31", "1m")).toBe("2026-02-28");
    expect(cutoffForRange("2026-08-31", "ytd")).toBe("2026-01-01");
    expect(cutoffForRange("2026-08-31", "1y")).toBe("2025-08-31");
    expect(cutoffForRange("2026-08-31", "all")).toBeNull();
    expect(cutoffForRange(undefined, "1m")).toBeNull();
  });
});

describe("today", () => {
  it("uses the Shanghai calendar date", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-26T16:30:00Z"));

    expect(today()).toBe("2026-08-27");
  });
});

describe("fullSeriesStart", () => {
  it("anchors five years back, one day inside the backend span limit", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-26T16:30:00Z"));

    expect(fullSeriesStart()).toBe("2021-08-29");
  });
});
