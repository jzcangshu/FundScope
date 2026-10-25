import { baselineIndex, drawdownAxisFloor, rangeDrawdown, windowPoints } from "@/utils/drawdown";
import {
  chartRangeDrawdown,
  intervalDrawdown,
  performanceDrawdowns,
  returnCutoff,
  type PerformancePoint,
} from "@/pages/FundDetail/performance";

const point = (valuation_date: string, adjusted_nav: string | null): PerformancePoint => ({ valuation_date, adjusted_nav });

/** Ascending series starting on 2026-01-01. */
function series(values: ReadonlyArray<string | null>, from = "2026-01-01"): PerformancePoint[] {
  const start = Date.parse(`${from}T00:00:00Z`);
  return values.map((value, index) => point(new Date(start + index * 86_400_000).toISOString().slice(0, 10), value));
}

function samples(points: readonly PerformancePoint[]) {
  return points.map((item) => ({ valuation_date: item.valuation_date, value: item.adjusted_nav === null ? null : Number(item.adjusted_nav) }));
}

describe("range drawdown — acceptance examples", () => {
  it("A: 1.00 → 1.20 → 1.10 peak-to-trough, not the period-open denominator", () => {
    const result = rangeDrawdown(samples(series(["1.00", "1.20", "1.10"])));
    expect(result.points[0].drawdown).toBe(0);
    expect(result.points[1].drawdown).toBe(0);
    expect(result.points[2].drawdown!).toBeCloseTo(-0.0833333, 6);
    expect(result.currentDrawdown).toBeCloseTo(-0.0833333, 6);
    expect(result.maxDrawdown).toBeCloseTo(-0.0833333, 6);
    // The rejected alternatives: -10% (period-open denominator) and +10% (return).
    expect(result.maxDrawdown).not.toBeCloseTo(-0.1, 4);
  });

  it("B: full window keeps the deep fall, the sub-window resets the peak", () => {
    const points = series(["1.00", "1.20", "0.90", "1.10"]);
    const full = rangeDrawdown(samples(points));
    expect(full.points[0].drawdown).toBe(0);
    expect(full.points[1].drawdown).toBe(0);
    expect(full.points[2].drawdown!).toBeCloseTo(-0.25, 10);
    expect(full.points[3].drawdown!).toBeCloseTo(-0.0833333, 6);
    expect(full.maxDrawdown).toBeCloseTo(-0.25, 10);
    expect(full.currentDrawdown).toBeCloseTo(-0.0833333, 6);

    const sub = rangeDrawdown(samples(points), points[2].valuation_date);
    expect(sub.points.map((p) => p.drawdown)).toEqual([0, 0]);
    expect(sub.currentDrawdown).toBe(0);
    expect(sub.maxDrawdown).toBe(0);
    expect(sub.start).toBe(points[2].valuation_date);
  });

  it("C: current and maximum are not interchangeable", () => {
    const result = rangeDrawdown(samples(series(["1.00", "1.20", "1.05", "1.15"])));
    expect(result.currentDrawdown).toBeCloseTo(-0.0416667, 6);
    expect(result.maxDrawdown).toBeCloseTo(-0.125, 10);
  });

  it("D: a later high never pairs with an earlier low", () => {
    const result = rangeDrawdown(samples(series(["1.00", "0.90", "1.20"])));
    expect(result.currentDrawdown).toBe(0);
    expect(result.maxDrawdown).toBeCloseTo(-0.1, 10);
  });

  it("E: highs before the window never participate", () => {
    const points = series(["2.00", "1.00", "1.10"]);
    const result = rangeDrawdown(samples(points), points[1].valuation_date);
    expect(result.points.map((p) => p.drawdown)).toEqual([0, 0]);
    expect(result.currentDrawdown).toBe(0);
    expect(result.maxDrawdown).toBe(0);
  });

  it("F: normalising the series does not change the drawdown", () => {
    const raw = series(["1.00", "1.20", "0.90", "1.10"]);
    const base = Number(raw[0].adjusted_nav);
    const normalised = raw.map((item) => point(item.valuation_date, String(Number(item.adjusted_nav) / base)));
    expect(rangeDrawdown(samples(normalised)).maxDrawdown).toBeCloseTo(-0.25, 10);
    expect(rangeDrawdown(samples(normalised)).currentDrawdown).toBeCloseTo(
      rangeDrawdown(samples(raw)).currentDrawdown!,
      12,
    );
  });
});

describe("range drawdown — windows and missing data", () => {
  it("returns nothing for an empty sequence", () => {
    const result = rangeDrawdown([]);
    expect(result.status).toBe("insufficient");
    expect(result.currentDrawdown).toBeNull();
    expect(result.maxDrawdown).toBeNull();
  });

  it("treats a single point as a flat window", () => {
    const result = rangeDrawdown(samples(series(["1.40"])));
    expect(result.currentDrawdown).toBe(0);
    expect(result.maxDrawdown).toBe(0);
  });

  it.each([null, "", "0", "-1", "NaN", "Infinity"])("rejects an invalid baseline %s", (value) => {
    const points = series(["1.00", value, "1.10"]);
    const result = rangeDrawdown(samples(points), points[1].valuation_date);
    expect(result.status).toBe("invalid");
    expect(result.currentDrawdown).toBeNull();
    expect(result.maxDrawdown).toBeNull();
    expect(result.reason).toContain("区间起点净值缺失或无效");
  });

  it("keeps a gap for a missing middle point and flags the window incomplete", () => {
    const result = rangeDrawdown(samples(series(["1.00", "1.20", null, "0.96"])));
    expect(result.points[2].drawdown).toBeNull();
    // The previously observed high is still the peak for the later point.
    expect(result.points[3].drawdown!).toBeCloseTo(-0.2, 10);
    expect(result.status).toBe("incomplete");
    expect(result.reason).toContain("仅基于已观测数据");
  });

  it("leaves the current drawdown empty when the end NAV is missing", () => {
    const result = rangeDrawdown(samples(series(["1.00", "1.20", "0.90", null])));
    expect(result.currentDrawdown).toBeNull();
    expect(result.maxDrawdown).toBeCloseTo(-0.25, 10);
  });

  it("reports an unavailable result for an end date without a valuation", () => {
    const points = series(["1.00", "1.10"]);
    const result = rangeDrawdown(samples(points), null, "2026-01-05");
    expect(result.status).toBe("insufficient");
    expect(result.reason).toContain("该估值日没有可用的净值记录");
  });

  it("uses the last valuation on or before the natural-day start", () => {
    const points = [point("2026-09-01", "1.00"), point("2026-09-09", "1.25"), point("2026-09-16", "1.10")];
    const result = intervalDrawdown(points, "2026-09-07", "2026-09-16");
    expect(result.startDate).toBe("2026-09-01");
    expect(result.endDate).toBe("2026-09-16");
    expect(result.targetStart).toBe("2026-09-07");
    expect(Number(result.current)).toBeCloseTo(-0.12, 10);
    expect(Number(result.max)).toBeCloseTo(-0.12, 10);
  });

  it("reports insufficient history instead of skipping to a later baseline", () => {
    const points = [point("2026-09-10", "1.00"), point("2026-09-16", "1.10")];
    const result = intervalDrawdown(points, "2026-09-01", "2026-09-16");
    expect(result.current).toBeNull();
    expect(result.max).toBeNull();
    expect(result.reason).toContain("历史不足");
  });

  it("includes the baseline point in the shared window slice", () => {
    const points = series(["1.00", "1.20", "0.90", "1.10"]);
    const cutoff = "2026-01-03";
    expect(baselineIndex(points, cutoff)).toBe(2);
    expect(windowPoints(points, cutoff).map((p) => p.valuation_date)).toEqual([
      "2026-01-03",
      "2026-01-04",
    ]);
  });
});

describe("range drawdown — page wiring", () => {
  it("calculates observed YTD drawdown when history starts during the year", () => {
    const points = [point("2026-06-01", "1.00"), point("2026-07-01", "1.20"), point("2026-08-01", "0.90"), point("2026-09-01", "1.10")];
    const result = chartRangeDrawdown(points, "ytd");
    expect(result.status).toBe("incomplete");
    expect(result.targetStart).toBe("2026-01-01");
    expect(result.start).toBe("2026-06-01");
    expect(result.reason).toContain("2026-06-01");
    expect(result.reason).toContain("不代表完整区间");
    expect(result.maxDrawdown).toBeCloseTo(-0.25, 10);
    expect(result.currentDrawdown).toBeCloseTo(-1 / 12, 10);
    expect(result.points).toHaveLength(4);
  });

  it("uses the first available January valuation without inventing a holiday NAV", () => {
    const result = chartRangeDrawdown([point("2026-01-05", "1.20"), point("2026-01-06", "1.08")], "ytd");
    expect(result.start).toBe("2026-01-05");
    expect(result.status).toBe("incomplete");
    expect(result.maxDrawdown).toBeCloseTo(-0.1, 10);
  });

  it("uses the January 1st baseline consistently with chart slicing and exports", () => {
    const points = [point("2025-12-31", "2.00"), point("2026-01-01", "1.20"), point("2026-01-06", "1.08")];
    const result = chartRangeDrawdown(windowPoints(points, "2026-01-01"), "ytd");
    expect(result.status).toBe("ok");
    expect(result.start).toBe("2026-01-01");
    expect(result.maxDrawdown).toBeCloseTo(-0.1, 10);
  });

  it("retains a prior-year baseline when it is available", () => {
    const result = chartRangeDrawdown([point("2025-12-31", "1.20"), point("2026-01-05", "1.08")], "ytd");
    expect(result.status).toBe("ok");
    expect(result.start).toBe("2025-12-31");
    expect(result.maxDrawdown).toBeCloseTo(-0.1, 10);
  });

  it("does not skip an invalid first YTD valuation or hide internal gaps", () => {
    const invalid = chartRangeDrawdown([point("2026-01-05", null), point("2026-01-06", "1.08")], "ytd");
    expect(invalid.status).toBe("invalid");
    expect(invalid.maxDrawdown).toBeNull();
    const gap = chartRangeDrawdown([point("2026-01-05", "1.20"), point("2026-01-06", null), point("2026-01-07", "1.08")], "ytd");
    expect(gap.status).toBe("incomplete");
    expect(gap.points[1].drawdown).toBeNull();
    expect(gap.reason).toContain("不代表完整区间");
    expect(gap.reason).toContain("缺失或无效");
  });

  it("recomputes when the chart range changes", () => {
    // 2026-01-01 … 2026-03-02, with the deep fall in January only.
    const values: string[] = [];
    for (let index = 0; index < 61; index += 1) values.push(index < 10 ? String(1 + index * 0.02) : String(1.18 - (index - 9) * 0.001));
    const points = series(values, "2026-01-01");
    const all = chartRangeDrawdown(points, "all");
    const oneMonth = chartRangeDrawdown(points, "1m");
    expect(all.maxDrawdown!).toBeLessThan(oneMonth.maxDrawdown!);
    // The one-month window restarts from its own baseline, so the January high
    // never leaks into it.
    expect(oneMonth.start).toBe("2026-02-02");
    expect(oneMonth.points[0].drawdown).toBe(0);
  });

  it("gives every statistic column its own window", () => {
    // The deep fall on 09-03 belongs to the month window, not to the week one.
    const points = [
      point("2026-08-03", "0.90"),
      point("2026-09-01", "1.00"),
      point("2026-09-03", "0.85"),
      point("2026-09-09", "1.10"),
      point("2026-09-14", "1.05"),
    ];
    const cells = performanceDrawdowns(points, "2026-09-14", null);
    const week = cells.find((cell) => cell.key === "1w")!;
    const month = cells.find((cell) => cell.key === "1m")!;
    const quarter = cells.find((cell) => cell.key === "3m")!;
    expect(week.startDate).toBe("2026-09-03");
    expect(Number(week.max)).toBeCloseTo(1.05 / 1.10 - 1, 10);
    expect(month.startDate).toBe("2026-08-03");
    expect(Number(month.max)).toBeCloseTo(-0.15, 10);
    // The three-month column has no baseline of its own; it stays empty
    // instead of borrowing the one-month window.
    expect(quarter.startDate).toBeNull();
    expect(quarter.reason).toContain("历史不足");
    // Same value for every column would mean the range was copied around.
    expect(week.max).not.toBe(month.max);
  });

  it("keeps inception unavailable until the backend supplies the full path", () => {
    const cells = performanceDrawdowns([point("2026-09-14", "1.10")], "2026-09-14", null);
    const inception = cells.find((cell) => cell.key === "inception")!;
    expect(inception.current).toBeNull();
    expect(inception.max).toBeNull();
    expect(inception.reason).toContain("成立以来");
  });

  it("uses the backend inception figures when history is complete", () => {
    const cells = performanceDrawdowns([point("2026-09-14", "1.10")], "2026-09-14", {
      methodology: "standard_running_peak_in_range",
      status: "ok",
      reason: null,
      target_start: "2024-01-01",
      start: "2024-01-05",
      end: "2026-09-14",
      current_drawdown: "-0.22",
      max_drawdown: "-0.35",
      peak_date: "2025-03-01",
      trough_date: "2025-06-01",
      peak_value: "1.5",
      trough_value: "0.975",
      observed_points: 600,
      missing_points: 0,
      note: null,
    });
    const inception = cells.find((cell) => cell.key === "inception")!;
    expect(inception.current).toBe("-0.22");
    expect(inception.max).toBe("-0.35");
    expect(inception.peakDate).toBe("2025-03-01");
  });

  it("clamps month-end and anchors year-to-date to January 1st", () => {
    expect(returnCutoff("2024-03-31", "1m")).toBe("2024-02-29");
    expect(returnCutoff("2026-09-15", "ytd")).toBe("2025-12-31");
    const points = [point("2025-12-30", "1.00"), point("2026-01-06", "1.25"), point("2026-09-15", "1.05")];
    const ytd = intervalDrawdown(points, returnCutoff("2026-09-15", "ytd")!, "2026-09-15");
    expect(ytd.startDate).toBe("2025-12-30");
    expect(Number(ytd.current)).toBeCloseTo(-0.16, 10);
  });
});

describe("drawdown axis floor", () => {
  it("follows the deepest point of the window", () => {
    expect(drawdownAxisFloor([0, -2.5, -8.1, -1])).toBeCloseTo(-8.1, 10);
  });

  it("never lets a flat window scale into positive territory", () => {
    // Every point at 0% previously produced ticks above the line (e.g. +3.0%).
    expect(drawdownAxisFloor([0, 0, 0])).toBe(-0.5);
    expect(drawdownAxisFloor([])).toBe(-0.5);
    expect(drawdownAxisFloor([null, null])).toBe(-0.5);
  });

  it("ignores gaps and non-finite values when picking the floor", () => {
    expect(drawdownAxisFloor([null, Number.NaN, -3.25])).toBeCloseTo(-3.25, 10);
  });

  it("honours a caller-supplied minimum span", () => {
    expect(drawdownAxisFloor([0, -0.1], 2)).toBe(-2);
    expect(drawdownAxisFloor([0, -9], 2)).toBe(-9);
  });
});
