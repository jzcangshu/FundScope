/** Formatting helpers. Decimals arrive as strings from the backend
 * (str(Decimal), e.g. "90000.0000000000"); we parse and reformat for display. */

/** Trim trailing zeros from a decimal string, keep at least 2dp for currency. */
export function dec(value: string | null | undefined, dp = 2): string {
  if (value === "" || value === null || value === undefined) return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  });
}

/** Percentage from a ratio string like "0.0123456789" → "+1.23%". */
export function pct(value: string | null | undefined, dp = 2): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  const s = (n * 100).toFixed(dp);
  return `${n >= 0 ? "+" : ""}${s}%`;
}

/** Signed percentage without the leading + for negatives. */
export function pctPlain(value: string | null | undefined, dp = 2): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return `${(n * 100).toFixed(dp)}%`;
}

/** Weight in percent. */
export function weight(value: string | null | undefined, dp = 2): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return `${(n * 100).toFixed(dp)}%`;
}

/** Compact number for large nav, e.g. 9,000,000 → 9,000,000 (keep full). */
export function money(value: string | null | undefined, unit = "元"): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return `${dec(value, 2)} ${unit}`;
}

/** Compact large currency values for dashboard KPI cards. */
export function compactMoney(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  const absolute = Math.abs(n);
  if (absolute >= 100000000) return `${(n / 100000000).toFixed(2)} 亿`;
  if (absolute >= 10000) return `${(n / 10000).toFixed(2)} 万`;
  return `${n.toFixed(2)} 元`;
}

/** Format an ISO date/datetime string to YYYY-MM-DD. */
export function dateStr(value: string | null | undefined): string {
  if (!value) return "—";
  return value.slice(0, 10);
}

/** Format a backend UTC ISO datetime as Beijing local time. */
export function timeStr(value: string | null | undefined): string {
  if (!value) return "—";
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const get = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
}

/** Sign-colored value for returns — 红涨绿跌 (red for gains, green for losses). */
export function returnColor(value: string | null | undefined): string {
  if (value === null || value === undefined) return "var(--muted)";
  const n = Number(value);
  if (!isFinite(n)) return "var(--muted)";
  if (n > 0) return "var(--up)";
  if (n < 0) return "var(--down)";
  return "var(--muted)";
}

/** Human-readable byte size. */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

/** Date range cutoff (inclusive) for a chart period, anchored on the latest date in the series. */
export function cutoffForRange(latest: string | undefined, range: "1m" | "3m" | "ytd" | "1y" | "all"): string | null {
  if (!latest || range === "all") return null;
  const date = new Date(`${latest}T00:00:00`);
  if (Number.isNaN(date.getTime())) return null;
  if (range === "ytd") return `${date.getFullYear()}-01-01`;
  const originalDay = date.getDate();
  date.setDate(1);
  if (range === "1m") date.setMonth(date.getMonth() - 1);
  if (range === "3m") date.setMonth(date.getMonth() - 3);
  if (range === "1y") date.setFullYear(date.getFullYear() - 1);
  const lastDay = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
  date.setDate(Math.min(originalDay, lastDay));
  return [date.getFullYear(), date.getMonth() + 1, date.getDate()]
    .map((part, index) => (index === 0 ? String(part) : String(part).padStart(2, "0")))
    .join("-");
}

/** Today as ISO date. */
export function today(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** Five-year trailing anchor for series requests: one day inside the backend's
    ">5 years is rejected" limit, so a single request always covers every
    published point and the range tabs can cut it client-side. */
export function fullSeriesStart(): string {
  const date = new Date(`${today()}T00:00:00`);
  date.setDate(date.getDate() - 1824);
  return [date.getFullYear(), date.getMonth() + 1, date.getDate()]
    .map((part, index) => (index === 0 ? String(part) : String(part).padStart(2, "0")))
    .join("-");
}
