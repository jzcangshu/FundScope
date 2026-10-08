import {
  chartRangeReturn,
  intervalReturn,
  performanceHistoryStart,
  performanceReturns,
  returnCutoff,
  type PerformancePoint,
} from "@/pages/FundDetail/performance";

const point = (valuation_date: string, adjusted_nav: string | null): PerformancePoint => ({ valuation_date, adjusted_nav });

describe("report performance periods", () => {
  it("uses calendar months and years with leap-year and month-end clamping", () => {
    expect(returnCutoff("2024-03-31", "1m")).toBe("2024-02-29");
    expect(returnCutoff("2025-03-31", "1m")).toBe("2025-02-28");
    expect(returnCutoff("2024-05-31", "3m")).toBe("2024-02-29");
    expect(returnCutoff("2024-02-29", "1y")).toBe("2023-02-28");
    expect(returnCutoff("2026-01-04", "1w")).toBe("2025-12-28");
    expect(returnCutoff("2026-09-15", "ytd")).toBe("2025-12-31");
    expect(returnCutoff("2026-02-30", "1m")).toBeNull();
  });

  it("anchors the bounded history to the displayed historical date", () => {
    const start = performanceHistoryStart("2020-08-31");
    expect((Date.parse("2020-08-31") - Date.parse(start)) / 86_400_000).toBe(1824);
    expect(start < "2020-01-01").toBe(true);
  });

  it("uses the last valuation on or before the calendar baseline, ignoring later points", () => {
    const result = intervalReturn([
      point("2026-09-14", "1.10"),
      point("2026-09-08", "1.08"),
      point("2026-09-04", "1.00"),
      point("2026-09-15", "9.00"),
    ], "2026-09-07", "2026-09-14");
    expect(Number(result.value)).toBeCloseTo(0.10);
    expect(result.startDate).toBe("2026-09-04");
    expect(result.endDate).toBe("2026-09-14");
    expect(result.targetStart).toBe("2026-09-07");
  });

  it("uses payout-adjusted values, not falling unit NAV or summed daily returns", () => {
    const points = [
      { ...point("2026-08-14", "1.00"), unit_nav: "1.00", cumulative_payout: "0" },
      { ...point("2026-09-14", "1.10"), unit_nav: "0.90", cumulative_payout: "0.20" },
    ];
    const cell = performanceReturns(points, "2026-09-14", null)[1];
    expect(Number(cell.value)).toBeCloseTo(0.10);
    expect(Number(chartRangeReturn(points, "1m"))).toBeCloseTo(0.10);
  });

  it("leaves a period empty when history starts after its baseline", () => {
    const cells = performanceReturns([point("2026-09-10", "1"), point("2026-09-14", "1.10")], "2026-09-14", "0.25");
    expect(cells.slice(0, 4).every((cell) => cell.value === null && cell.reason?.includes("历史不足"))).toBe(true);
    expect(cells[4].value).toBe("0.25");
  });

  it("does not manufacture inception returns from the fetched window", () => {
    const cells = performanceReturns([point("2025-09-12", "1"), point("2026-09-14", "1.50")], "2026-09-14", null);
    expect(cells[4].value).toBeNull();
    expect(cells[4].reason).toContain("未提供");
  });

  it("requires the exact displayed valuation date and clears all cells for a date without a version", () => {
    const points = [point("2026-09-04", "1"), point("2026-09-11", "1.1"), point("2026-09-15", "1.2")];
    expect(intervalReturn(points, "2026-09-04", "2026-09-14").value).toBeNull();
    expect(performanceReturns(points, null, "0.20").every((cell) => cell.value === null)).toBe(true);
  });

  it.each([null, "", "0", "-1", "NaN", "Infinity"])("rejects invalid baseline %s without falling back to an earlier value", (value) => {
    const result = intervalReturn([point("2026-09-03", "1"), point("2026-09-04", value), point("2026-09-14", "1.1")], "2026-09-07", "2026-09-14");
    expect(result.value).toBeNull();
    expect(result.startDate).toBe("2026-09-04");
  });

  it("keeps a missing final NAV empty instead of carrying the earlier NAV forward", () => {
    const points = [point("2026-09-04", "1"), point("2026-09-11", "1.1"), point("2026-09-14", null)];
    expect(intervalReturn(points, "2026-09-07", "2026-09-14").value).toBeNull();
    expect(chartRangeReturn(points, "all")).toBeNull();
  });

  it("includes the preceding year-end valuation when calculating year-to-date chart return", () => {
    const points = [point("2025-12-30", "1"), point("2026-01-02", "1.1"), point("2026-09-14", "1.2")];
    expect(Number(chartRangeReturn(points, "ytd"))).toBeCloseTo(0.2);
  });
});
