import { Segmented } from "antd";
import type { TooltipProps } from "recharts";
import { RANGE_OPTIONS, type RangeKey } from "@/utils/constants";

/* ---------- PeriodTabs: shared chart range selector ---------- */
export function PeriodTabs({ value, onChange, ariaLabel = "时间范围" }: { value: RangeKey; onChange: (value: RangeKey) => void; ariaLabel?: string }) {
  return (
    <Segmented
      className="fd-period-tabs"
      value={value}
      onChange={(next) => onChange(next as RangeKey)}
      options={RANGE_OPTIONS.map((option) => ({ label: option.label, value: option.value }))}
      aria-label={ariaLabel}
    />
  );
}

/* ---------- ChartTooltip: consistent tooltip surface for Recharts ---------- */
type TooltipPayload = { name?: string; value?: unknown; color?: string; payload?: Record<string, unknown> };

export function ChartTooltip({
  active,
  payload,
  labelKey = "valuation_date",
  format,
}: TooltipProps<number, string> & {
  labelKey?: string;
  format: (value: unknown, name: string | undefined) => [string, string];
}) {
  if (!active || !payload || payload.length === 0) return null;
  const rows = payload as TooltipPayload[];
  const label = rows[0]?.payload?.[labelKey];
  return (
    <div className="fd-chart-tooltip">
      <div className="fd-chart-tooltip__label">{typeof label === "string" ? label : ""}</div>
      {rows.map((row, index) => {
        const [value, name] = format(row.value, row.name);
        return (
          <div className="fd-chart-tooltip__row" key={`${name}-${index}`}>
            <span style={{ color: row.color ?? "var(--muted)" }}>{name}</span>
            <b>{value}</b>
          </div>
        );
      })}
    </div>
  );
}

export const axisTick = { fill: "var(--muted)", fontSize: 14, fontFamily: "inherit" };
