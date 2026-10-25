/** Range drawdown — one caliber for the company chart, the product chart,
 *  the statistic columns and the NAV detail column.
 *
 *  Standard formula, evaluated inside the selected window only:
 *    Pt = max(Vs) for t0 <= s <= t     running peak, reset at the window start
 *    Dt = Vt / Pt - 1                  0 at the first point, never positive
 *    max drawdown     = min(Dt)        deepest observed fall in the window
 *    current drawdown = DT             fall from the window high as of the end date
 *
 *  The window start is the latest valuation on or before the requested natural
 *  day; that baseline is part of the window. Highs recorded before it never
 *  participate, so switching 近1月 / 近3月 really recomputes instead of
 *  cropping a pre-computed curve. Normalising the series (to 1 or 100) does
 *  not change any of these numbers.
 *
 *  Values are ratios (e.g. -0.0833…); the UI renders them as percentages. */

export type DrawdownStatus = "ok" | "incomplete" | "insufficient" | "invalid";

export interface DrawdownSample {
  valuation_date: string;
  value: number | null;
}

export interface DrawdownPoint {
  valuation_date: string;
  value: number | null;
  peak: number | null;
  /** Ratio, 0 or negative. `null` keeps a gap in the chart. */
  drawdown: number | null;
}

export interface RangeDrawdown {
  status: DrawdownStatus;
  reason: string | null;
  /** Requested natural-day start; the real baseline may be earlier. */
  targetStart: string | null;
  /** Real first valuation day of the window. */
  start: string | null;
  /** Real last valuation day of the window. */
  end: string | null;
  points: DrawdownPoint[];
  currentDrawdown: number | null;
  maxDrawdown: number | null;
  peakDate: string | null;
  troughDate: string | null;
  peakValue: number | null;
  troughValue: number | null;
  observed: number;
  missing: number;
}

const EMPTY: RangeDrawdown = {
  status: "insufficient",
  reason: null,
  targetStart: null,
  start: null,
  end: null,
  points: [],
  currentDrawdown: null,
  maxDrawdown: null,
  peakDate: null,
  troughDate: null,
  peakValue: null,
  troughValue: null,
  observed: 0,
  missing: 0,
};

function unavailable(reason: string, targetStart: string | null, end: string | null): RangeDrawdown {
  return { ...EMPTY, reason, targetStart, end };
}

/** Missing, zero, negative and non-finite NAV never enter a peak or a ratio. */
function usable(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

/** Latest point on or before `cutoff`; the window baseline. */
export function baselineIndex<T extends { valuation_date: string }>(
  points: readonly T[],
  cutoff: string | null | undefined,
  endIndex: number = points.length - 1,
): number {
  if (!cutoff) return 0;
  for (let index = Math.min(endIndex, points.length - 1); index >= 0; index -= 1) {
    if (points[index].valuation_date <= cutoff) return index;
  }
  return -1;
}

/** Window slice shared by the NAV chart, the drawdown chart and the summaries.
 *  The baseline valuation belongs to the window even when it is earlier than
 *  the requested natural day. Falls back to the whole series when the history
 *  never reaches the cutoff, so a short product still renders its data. */
export function windowPoints<T extends { valuation_date: string }>(
  points: readonly T[],
  cutoff: string | null | undefined,
): T[] {
  if (!cutoff) return [...points];
  const index = baselineIndex(points, cutoff);
  return index < 0 ? [...points] : points.slice(index);
}

export function rangeDrawdown(
  samples: readonly DrawdownSample[],
  targetStart: string | null = null,
  end: string | null = null,
  options: { allowPartialStart?: boolean } = {},
): RangeDrawdown {
  if (!samples.length) {
    return unavailable("区间内没有可用的净值记录", targetStart, end);
  }
  const last = end === null ? samples.length - 1 : samples.findIndex((point) => point.valuation_date === end);
  if (last < 0) {
    return unavailable("该估值日没有可用的净值记录", targetStart, end);
  }
  const end_date = samples[last].valuation_date;
  if (targetStart && end_date < targetStart) {
    return unavailable(`该区间内没有估值记录（最新估值日 ${end_date}）`, targetStart, end_date);
  }
  let first = baselineIndex(samples, targetStart, last);
  const partialStart = first < 0 && options.allowPartialStart === true;
  if (first < 0) {
    if (!partialStart) return unavailable("历史不足，缺少区间起点估值", targetStart, end_date);
    first = 0;
  }
  const window = samples.slice(first, last + 1);
  if (!usable(window[0].value)) {
    // Never skip the baseline and present a shorter window as complete.
    return {
      ...unavailable("区间起点净值缺失或无效", targetStart, end_date),
      status: "invalid",
      start: window[0].valuation_date,
    };
  }

  let peak: number | null = null;
  let peakDate: string | null = null;
  let worst: number | null = null;
  let worstPeakDate: string | null = null;
  let worstTroughDate: string | null = null;
  let worstPeakValue: number | null = null;
  let worstTroughValue: number | null = null;
  let observed = 0;
  let missing = 0;
  const points: DrawdownPoint[] = [];

  for (const sample of window) {
    if (!usable(sample.value)) {
      missing += 1;
      points.push({ valuation_date: sample.valuation_date, value: null, peak, drawdown: null });
      continue;
    }
    observed += 1;
    if (peak === null || sample.value > peak) {
      peak = sample.value;
      peakDate = sample.valuation_date;
    }
    const drawdown = sample.value / peak - 1;
    if (worst === null || drawdown < worst) {
      worst = drawdown;
      worstPeakDate = peakDate;
      worstTroughDate = sample.valuation_date;
      worstPeakValue = peak;
      worstTroughValue = sample.value;
    }
    points.push({ valuation_date: sample.valuation_date, value: sample.value, peak, drawdown });
  }

  const reasons: string[] = [];
  if (partialStart) reasons.push(`缺少区间起点估值，自 ${window[0].valuation_date} 起计算已观测回撤，不代表完整区间`);
  if (missing) reasons.push("区间内存在缺失或无效的净值，回撤仅基于已观测数据");
  const status: DrawdownStatus = reasons.length ? "incomplete" : "ok";
  return {
    status,
    reason: reasons.join("；") || null,
    targetStart,
    start: window[0].valuation_date,
    end: end_date,
    points,
    currentDrawdown: points[points.length - 1]?.drawdown ?? null,
    maxDrawdown: worst,
    peakDate: worstPeakDate,
    troughDate: worstTroughDate,
    peakValue: worstPeakValue,
    troughValue: worstTroughValue,
    observed,
    missing,
  };
}

/** Drawdown as a ratio string, keeping the backend's string-decimal convention. */
export function drawdownText(value: number | null | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return String(value);
}

/** Lower bound for a drawdown axis, so the axis never reaches into positive
 *  territory. A window that only rises sits at 0 for every point, which would
 *  otherwise let the chart pick its own floor and render a positive scale
 *  (e.g. a "+3.0%" tick above a flat 0% line). `minimumSpan` keeps that flat
 *  case from collapsing into a degenerate axis; callers pass percentage
 *  values, so the default is half a percentage point. */
export function drawdownAxisFloor(
  values: readonly (number | null)[],
  minimumSpan = 0.5,
): number {
  const numbers = values.filter((value): value is number => value !== null && Number.isFinite(value));
  return Math.min(-Math.abs(minimumSpan), ...numbers);
}
