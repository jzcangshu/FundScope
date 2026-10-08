import { useMemo } from "react";
import { Skeleton, Table, Tooltip } from "antd";
import { ErrorState, Num, Section } from "@/components";
import { dateStr, pct, pctPlain, returnColor } from "@/utils/format";
import type { BenchmarkReturns, RangeDrawdownDetail } from "@/api/types";
import { performanceDrawdowns, performanceReturns, type DrawdownCell, type PerformanceCell, type PerformancePoint } from "../performance";

export interface PerformanceSummary {
  valuationDate: string | null;
  cumulativeReturn: string | null;
  establishmentDate: string | null;
  benchmarkReturns?: BenchmarkReturns | null;
  /** Since-inception drawdown needs the full path, so the backend answers it. */
  inceptionDrawdown?: RangeDrawdownDetail | null;
}

function cellDescription(cell: PerformanceCell): string {
  if (cell.reason) return cell.reason;
  if (cell.key === "inception") {
    return `${cell.startDate ? `${cell.startDate} 至 ` : "截至 "}${cell.endDate} · 采用估值记录的成立以来收益`;
  }
  return `${cell.startDate} 至 ${cell.endDate}${cell.startDate !== cell.targetStart ? `（目标起点 ${cell.targetStart}，取此前最近估值）` : ""}`;
}

function drawdownDescription(cell: DrawdownCell): string {
  if (cell.current === null && cell.max === null) {
    return cell.reason ?? "该区间没有可用的回撤结果";
  }
  const parts = [`${cell.startDate ?? "—"} 至 ${cell.endDate ?? "—"}`, "按所选区间重算"];
  if (cell.peakDate && cell.troughDate) parts.push(`最深回落 ${cell.peakDate} → ${cell.troughDate}`);
  if (cell.status === "incomplete") parts.push("区间数据不完整，仅基于已观测净值");
  if (cell.note) parts.push(cell.note);
  return parts.join(" · ");
}

export function PerformanceSection({
  points,
  summary,
  loading,
  error,
  onRetry,
}: {
  points: readonly PerformancePoint[];
  summary: PerformanceSummary;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const cells = useMemo(
    () => performanceReturns(points, summary.valuationDate, summary.cumulativeReturn, summary.establishmentDate),
    [points, summary.valuationDate, summary.cumulativeReturn, summary.establishmentDate],
  );
  // Each column limits its own window and resets the running peak at its own
  // start; nothing is copied from the chart's range.
  const drawdownCells = useMemo(
    () => performanceDrawdowns(points, summary.valuationDate, summary.inceptionDrawdown ?? null),
    [points, summary.valuationDate, summary.inceptionDrawdown],
  );
  const drawdownByKey = useMemo(
    () => new Map(drawdownCells.map((cell) => [cell.key, cell])),
    [drawdownCells],
  );

  const benchmarkReturns = summary.benchmarkReturns;
  const hasBenchmark = benchmarkReturns != null && Object.values(benchmarkReturns).some((v) => v?.benchmark_return != null);

  return (
    <Section id="performance" title="收益与回撤" count={summary.valuationDate ? <>截至 <Num>{dateStr(summary.valuationDate)}</Num></> : undefined}>
      {error ? <ErrorState message={error} onRetry={onRetry} retrying={loading} /> : loading ? <Skeleton active paragraph={{ rows: 3 }} /> : (
        <>
          <Table
            className="fd-table--read"
            rowKey="key"
            size="small"
            pagination={false}
            scroll={{ x: 760 }}
            dataSource={[
              { key: "returns", label: "本产品收益率" },
              ...(hasBenchmark ? [{ key: "benchmark", label: "沪深300" }] : []),
              { key: "current_drawdown", label: "区间当前回撤" },
              { key: "max_drawdown", label: "区间最大回撤" },
            ]}
            columns={[
              { title: "收益与回撤区间", dataIndex: "label", width: 150 },
              ...cells.map((cell) => ({
                title: cell.label,
                key: cell.key,
                align: "right" as const,
                render: (_: unknown, row: { key: string }) => {
                  if (row.key === "benchmark") {
                    const bm = benchmarkReturns?.[cell.key];
                    const bmValue = bm?.benchmark_return ?? null;
                    const bmReason = bm?.unavailable_reason ?? null;
                    const bmLabel = bmValue != null
                      ? `${pct(bmValue)}`
                      : "—";
                    const bmTip = bmReason
                      ?? (bm?.benchmark_start_date && bm?.benchmark_end_date
                        ? `${bm.benchmark_start_date} 至 ${bm.benchmark_end_date}`
                        : "");
                    return (
                      <Tooltip title={bmTip || undefined}>
                        <span tabIndex={0} aria-label={`${cell.label} 沪深300 ${bmLabel}`}>
                          <Num style={{ color: returnColor(bmValue) }}>{bmLabel}</Num>
                        </span>
                      </Tooltip>
                    );
                  }
                  if (row.key === "current_drawdown" || row.key === "max_drawdown") {
                    const drawdown = drawdownByKey.get(cell.key);
                    const value = row.key === "current_drawdown" ? drawdown?.current ?? null : drawdown?.max ?? null;
                    const label = pctPlain(value);
                    const tip = drawdown ? drawdownDescription(drawdown) : "";
                    return (
                      <Tooltip title={tip || undefined}>
                        <span tabIndex={0} aria-label={`${cell.label}${row.key === "current_drawdown" ? "区间当前回撤" : "区间最大回撤"} ${label}，${tip}`}>
                          <Num style={{ color: returnColor(value) }}>{label}</Num>
                        </span>
                      </Tooltip>
                    );
                  }
                  return (
                    <Tooltip title={cellDescription(cell)}>
                      <span tabIndex={0} aria-label={`${cell.label} ${pct(cell.value)}，${cellDescription(cell)}`}>
                        <Num style={{ color: returnColor(cell.value) }}>{pct(cell.value)}</Num>
                      </span>
                    </Tooltip>
                  );
                },
              })),
            ]}
          />
          <p className="fd-form-note">{summary.valuationDate
            ? "区间收益按复权净值计算；回撤采用标准公式 Vt/Pt−1，在各自区间起点重置滚动峰值；非估值日取此前最近估值，历史不足显示 —。"
            : "该日期没有已发布的估值，暂无收益与回撤指标。"}</p>
        </>
      )}
    </Section>
  );
}
