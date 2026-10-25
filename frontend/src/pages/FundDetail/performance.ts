import type { NavPoint, RangeDrawdownDetail } from "@/api/types";
import type { RangeKey } from "@/utils/constants";
import { cutoffForRange } from "@/utils/format";
import { drawdownText, rangeDrawdown, type DrawdownStatus, type RangeDrawdown } from "@/utils/drawdown";

export type PerformancePoint = Pick<NavPoint, "valuation_date" | "adjusted_nav">;
export type ReturnPeriod = "1w" | "1m" | "3m" | "1y";
export interface PeriodReturn {
  value: string | null;
  startDate: string | null;
  endDate: string | null;
  targetStart: string | null;
  reason: string | null;
}
export interface PerformanceCell extends PeriodReturn {
  key: ReturnPeriod | "inception";
  label: string;
}
/** One fixed statistic column: both drawdown figures share the column's window. */
export interface DrawdownCell {
  key: ReturnPeriod | "inception";
  label: string;
  current: string | null;
  max: string | null;
  startDate: string | null;
  endDate: string | null;
  targetStart: string | null;
  reason: string | null;
  note: string | null;
  status: DrawdownStatus;
  peakDate: string | null;
  troughDate: string | null;
}

const PERIODS: ReadonlyArray<{ key: ReturnPeriod; label: string }> = [
  { key: "1w", label: "近一周" },
  { key: "1m", label: "近一月" },
  { key: "3m", label: "近三月" },
  { key: "1y", label: "近一年" },
];

function parseDate(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? date : null;
}

function numberValue(value: string | null | undefined): number | null {
  if (value == null || value.trim() === "") return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

/** Calendar periods, clamped at month ends; UTC arithmetic avoids DST shifts. */
export function returnCutoff(end: string, period: ReturnPeriod | "ytd"): string | null {
  const date = parseDate(end);
  if (!date) return null;
  if (period === "1w") {
    date.setUTCDate(date.getUTCDate() - 7);
  } else if (period === "ytd") {
    date.setUTCMonth(0, 0);
  } else {
    const day = date.getUTCDate();
    date.setUTCDate(1);
    date.setUTCMonth(date.getUTCMonth() - (period === "1m" ? 1 : period === "3m" ? 3 : 12));
    const monthEnd = new Date(date.getTime());
    monthEnd.setUTCMonth(monthEnd.getUTCMonth() + 1, 0);
    date.setUTCDate(Math.min(day, monthEnd.getUTCDate()));
  }
  return date.toISOString().slice(0, 10);
}

/** Keep the existing bounded history window, anchored to the displayed valuation. */
export function performanceHistoryStart(end: string): string {
  const date = parseDate(end);
  if (!date) throw new Error("Invalid valuation date");
  date.setUTCDate(date.getUTCDate() - 1824);
  return date.toISOString().slice(0, 10);
}

/** Do not skip a missing NAV at a selected boundary or substitute a later baseline. */
export function intervalReturn(points: readonly PerformancePoint[], targetStart: string, end: string): PeriodReturn {
  const result: PeriodReturn = { value: null, startDate: null, endDate: end, targetStart, reason: null };
  if (!parseDate(targetStart) || !parseDate(end) || targetStart > end) {
    return { ...result, reason: "收益区间日期无效" };
  }
  const last = points.find((point) => point.valuation_date === end);
  if (!last) return { ...result, reason: "该估值日没有可用的净值记录" };
  const first = points.reduce<PerformancePoint | undefined>((selected, point) => (
    point.valuation_date <= targetStart && (!selected || point.valuation_date > selected.valuation_date) ? point : selected
  ), undefined);
  if (!first) return { ...result, reason: "历史不足，缺少区间起点估值" };
  result.startDate = first.valuation_date;
  const startValue = numberValue(first.adjusted_nav);
  const endValue = numberValue(last.adjusted_nav);
  if (startValue === null || endValue === null || startValue <= 0 || endValue <= 0) {
    return { ...result, reason: "区间起止净值缺失或无效" };
  }
  const value = endValue / startValue - 1;
  return Number.isFinite(value) ? { ...result, value: String(value) } : { ...result, reason: "收益结果无效" };
}

export function performanceReturns(
  points: readonly PerformancePoint[],
  end: string | null,
  reportedCumulative: string | null,
  establishmentDate: string | null = null,
): PerformanceCell[] {
  const validEnd = end && parseDate(end) ? end : null;
  const cells: PerformanceCell[] = PERIODS.map(({ key, label }) => ({
    key,
    label,
    ...(validEnd
      ? intervalReturn(points, returnCutoff(validEnd, key)!, validEnd)
      : { value: null, startDate: null, endDate: null, targetStart: null, reason: "该日期没有已发布的估值" }),
  }));
  const inception = validEnd ? numberValue(reportedCumulative) : null;
  cells.push({
    key: "inception",
    label: "成立以来",
    value: inception === null ? null : String(inception),
    startDate: establishmentDate && parseDate(establishmentDate) ? establishmentDate : null,
    endDate: validEnd,
    targetStart: null,
    reason: validEnd ? inception === null ? "估值记录未提供成立以来收益" : null : "该日期没有已发布的估值",
  });
  return cells;
}

/** A chart's “全部” is the loaded interval, never a claim of inception coverage. */
export function chartRangeReturn(points: readonly PerformancePoint[], range: RangeKey): string | null {
  if (!points.length) return null;
  const ordered = [...points].sort((left, right) => left.valuation_date.localeCompare(right.valuation_date));
  const end = ordered[ordered.length - 1].valuation_date;
  const start = range === "all" ? ordered[0].valuation_date : returnCutoff(end, range);
  return start ? intervalReturn(ordered, start, end).value : null;
}

function drawdownSamples(points: readonly PerformancePoint[]): { valuation_date: string; value: number | null }[] {
  return [...points]
    .sort((left, right) => left.valuation_date.localeCompare(right.valuation_date))
    .map((point) => ({ valuation_date: point.valuation_date, value: numberValue(point.adjusted_nav) }));
}

/** Drawdown of one window, on the same dates the return column uses. */
export function intervalDrawdown(
  points: readonly PerformancePoint[],
  targetStart: string,
  end: string,
): Omit<DrawdownCell, "key" | "label"> {
  if (!parseDate(targetStart) || !parseDate(end) || targetStart > end) {
    return { current: null, max: null, startDate: null, endDate: end, targetStart, reason: "回撤区间日期无效", note: null, status: "insufficient", peakDate: null, troughDate: null };
  }
  const result = rangeDrawdown(drawdownSamples(points), targetStart, end);
  return {
    current: drawdownText(result.currentDrawdown),
    max: drawdownText(result.maxDrawdown),
    startDate: result.start,
    endDate: result.end,
    targetStart,
    reason: result.reason,
    note: null,
    status: result.status,
    peakDate: result.peakDate,
    troughDate: result.troughDate,
  };
}

/** Same window as the NAV chart and export. YTD without baseline history
 *  shows the observed drawdown with an explicit incomplete-window warning. */
export function chartRangeDrawdown(points: readonly PerformancePoint[], range: RangeKey): RangeDrawdown {
  if (!points.length) return rangeDrawdown([]);
  const ordered = [...points].sort((left, right) => left.valuation_date.localeCompare(right.valuation_date));
  const end = ordered[ordered.length - 1].valuation_date;
  const targetStart = cutoffForRange(end, range);
  return rangeDrawdown(drawdownSamples(ordered), targetStart, end, { allowPartialStart: range === "ytd" });
}

/** Every column limits its own window and resets the running peak at its own start. */
export function performanceDrawdowns(
  points: readonly PerformancePoint[],
  end: string | null,
  inception: RangeDrawdownDetail | null,
): DrawdownCell[] {
  const validEnd = end && parseDate(end) ? end : null;
  const cells: DrawdownCell[] = PERIODS.map(({ key, label }) => ({
    key,
    label,
    ...(validEnd
      ? intervalDrawdown(points, returnCutoff(validEnd, key)!, validEnd)
      : { current: null, max: null, startDate: null, endDate: null, targetStart: null, reason: "该日期没有已发布的估值", note: null, status: "insufficient" as DrawdownStatus, peakDate: null, troughDate: null }),
  }));
  // Since inception needs the complete path, which the loaded five-year chart
  // window cannot prove; the backend answers it from full history or says why not.
  const inceptionStatus = (inception?.status ?? "insufficient") as DrawdownStatus;
  cells.push({
    key: "inception",
    label: "成立以来",
    current: inception?.current_drawdown ?? null,
    max: inception?.max_drawdown ?? null,
    startDate: inception?.start ?? null,
    endDate: inception?.end ?? validEnd,
    targetStart: inception?.target_start ?? null,
    reason: inception?.reason ?? (validEnd ? "缺少成立以来完整历史，暂不可用" : "该日期没有已发布的估值"),
    note: inception?.note ?? null,
    status: inceptionStatus,
    peakDate: inception?.peak_date ?? null,
    troughDate: inception?.trough_date ?? null,
  });
  return cells;
}
