import { useMemo, useState } from "react";
import { Button, Skeleton, Table, Tooltip } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { Area, AreaChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip as ChartTooltipBox, XAxis, YAxis } from "recharts";
import * as fundsApi from "@/api/funds";
import * as downloads from "@/api/downloads";
import type { NavPoint } from "@/api/types";
import { ChartTooltip, EmptyState, ErrorState, Num, PeriodTabs, Section, axisTick, useAsyncData, useToast } from "@/components";
import { cutoffForRange, dateStr, dec, fullSeriesStart, pct, pctPlain, returnColor } from "@/utils/format";
import { RANGE_OPTIONS, type RangeKey } from "@/utils/constants";
import { drawdownAxisFloor, drawdownText, windowPoints } from "@/utils/drawdown";
import { chartRangeDrawdown, chartRangeReturn, performanceHistoryStart } from "../performance";
import { PerformanceSection, type PerformanceSummary } from "./Performance";

const SERIES_NAME: Record<string, string> = { unitNav: "单位净值", adjustedNav: "复权净值", benchmark: "沪深300" };
const RANGE_LABEL = Object.fromEntries(RANGE_OPTIONS.map((option) => [option.value, option.label])) as Record<RangeKey, string>;

/** One request fetches the full five-year window; `asOf` caps it at that date.
    Range tabs then cut the points client-side without further requests, and
    every figure below is recomputed from the cut window. */
export default function NavSeriesSection({ fundId, asOf, valuationDate, performance }: {
  fundId: number;
  asOf?: string;
  valuationDate: string | null;
  performance: PerformanceSummary;
}) {
  const toast = useToast();
  const [range, setRange] = useState<RangeKey>("all");
  const seriesEnd = valuationDate ?? asOf;
  const seriesStart = seriesEnd ? performanceHistoryStart(seriesEnd) : fullSeriesStart();
  const state = useAsyncData(() => fundsApi.navSeries(fundId, seriesStart, seriesEnd).then((result) => result.data), [fundId, seriesStart, seriesEnd], "净值序列加载失败");
  const { data, loading, error, reload } = state;
  const points = data?.points ?? [];

  // NAV chart, drawdown chart, summaries and the detail column all share this
  // window; the baseline valuation belongs to it even when it is older than
  // the requested natural day.
  const rangeCutoff = useMemo(() => cutoffForRange(points[points.length - 1]?.valuation_date, range), [points, range]);
  const visible = useMemo(() => windowPoints(points, rangeCutoff), [points, rangeCutoff]);

  const chartPoints = useMemo(
    () => visible.map((point) => ({
      ...point,
      label: dateStr(point.valuation_date).slice(5),
      unitNav: point.unit_nav === null ? null : Number(point.unit_nav),
      adjustedNav: point.adjusted_nav === null ? null : Number(point.adjusted_nav),
    })),
    [visible],
  );
  const hasAdjusted = chartPoints.some((point) => point.adjustedNav !== null && point.adjustedNav !== point.unitNav);

  // Benchmark overlay: normalize CSI 300 to match the fund's first visible NAV.
  const benchmarkPoints = data?.benchmark?.points ?? [];
  type ChartPointWithBenchmark = (typeof chartPoints)[number] & { benchmark: number | null };
  const chartPointsWithBenchmark: ChartPointWithBenchmark[] = useMemo(() => {
    if (!chartPoints.length || !benchmarkPoints.length) return chartPoints.map((p) => ({ ...p, benchmark: null }));
    const bmByDate = new Map(benchmarkPoints.map((p) => [p.trade_date, Number(p.close)]));
    const bmDates = benchmarkPoints.map((p) => p.trade_date);
    let bmBase: number | null = null;
    let navBase: number | null = null;
    return chartPoints.map((point) => {
      // Find benchmark close on or before this valuation date.
      const direct = bmByDate.get(point.valuation_date);
      let bmClose = direct ?? null;
      if (bmClose === null) {
        let lo = 0, hi = bmDates.length - 1, idx = -1;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (bmDates[mid] <= point.valuation_date) { idx = mid; lo = mid + 1; }
          else hi = mid - 1;
        }
        if (idx >= 0) bmClose = bmByDate.get(bmDates[idx]) ?? null;
      }
      const navVal = point.unitNav;
      if (bmBase === null && bmClose != null && navVal != null) {
        bmBase = bmClose;
        navBase = navVal;
      }
      const benchmark = bmClose != null && bmBase != null && bmBase !== 0 && navBase != null
        ? (bmClose / bmBase) * navBase : null;
      return { ...point, benchmark };
    });
  }, [chartPoints, benchmarkPoints]);
  const hasBenchmark = chartPointsWithBenchmark.some((p) => p.benchmark !== null);

  async function exportSeries() {
    try {
      const query = new URLSearchParams({ start: seriesStart, range });
      if (seriesEnd) query.set("end", seriesEnd);
      await downloads.downloadBlob(`/exports/funds/${fundId}/nav-series?${query}`, `fund-${fundId}-nav-series.csv`);
      toast.success("净值序列已导出");
    } catch {
      toast.error("导出失败，请稍后重试");
    }
  }

  const rangeReturn = useMemo(() => chartRangeReturn(visible, range), [visible, range]);
  // Recomputed from the window's own NAV: a cropped pre-computed curve would
  // keep highs that belong to an earlier range.
  const drawdown = useMemo(() => chartRangeDrawdown(visible, range), [visible, range]);
  const drawdownChart = useMemo(
    () => drawdown.points.map((point) => ({
      label: dateStr(point.valuation_date).slice(5),
      drawdownPct: point.drawdown === null ? null : point.drawdown * 100,
    })),
    [drawdown],
  );
  const drawdownByDate = useMemo(
    () => new Map(drawdown.points.map((point) => [point.valuation_date, point.drawdown])),
    [drawdown],
  );
  // Pin the axis to the window's own depth; a flat window must not scale into
  // positive percentages.
  const drawdownFloor = useMemo(
    () => drawdownAxisFloor(drawdownChart.map((point) => point.drawdownPct)),
    [drawdownChart],
  );
  const drawdownUnavailable = drawdown.status === "insufficient" || drawdown.status === "invalid";
  const drawdownNote = drawdownUnavailable
    ? drawdown.reason
    : drawdown.status === "incomplete"
      ? drawdown.reason
      : drawdown.start && drawdown.end
        ? `${RANGE_LABEL[range]} · ${drawdown.start} 至 ${drawdown.end}`
        : RANGE_LABEL[range];

  // Only the first load shows a skeleton; silent reloads keep the chart on screen.
  const showSkeleton = loading && !data;

  return (
    <>
      <Section id="nav" ariaLabel="净值与回撤">
        {/* Same shape as the company overview: the head (title + range tabs)
            lives inside the left column, so the tabs line up with the NAV chart
            instead of being pushed to the far edge over the drawdown column. */}
        <div className="fd-split fd-split--charts">
          <div>
            <div className="fd-section__head">
              <div className="fd-section__title">
                净值走势
                {rangeReturn !== null && <small>区间 <span className="num" style={{ color: returnColor(rangeReturn) }}>{pct(rangeReturn)}</span></small>}
              </div>
              <PeriodTabs value={range} onChange={setRange} ariaLabel="净值与回撤时间范围" />
            </div>
            {error ? (
              <ErrorState message={error} onRetry={() => void reload()} retrying={loading} />
            ) : showSkeleton ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : chartPoints.length === 0 ? (
              <div className="fd-chart-empty">{asOf ? "该日期之前没有已发布的净值数据" : "该时间范围内没有已发布的净值数据"}</div>
            ) : (
              <div className="fd-chart fd-chart--tall" role="img" aria-label="基金单位净值与复权净值走势">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartPointsWithBenchmark} margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
                    <CartesianGrid stroke="var(--border)" strokeOpacity={0.55} vertical={false} />
                    <XAxis dataKey="label" minTickGap={32} tick={axisTick} axisLine={false} tickLine={false} dy={6} />
                    <YAxis domain={["auto", "auto"]} tickFormatter={(value: number) => value.toFixed(3)} tick={axisTick} axisLine={false} tickLine={false} width={56} />
                    <ChartTooltipBox
                      content={<ChartTooltip format={(value, name) => [typeof value === "number" ? value.toFixed(4) : "—", SERIES_NAME[name ?? ""] ?? String(name)]} />}
                      cursor={{ stroke: "var(--border-strong)" }}
                    />
                    {/* Above the plot, not below: the date row must be the last
                        thing in the chart so it lines up with the drawdown
                        chart's date row. */}
                    {(hasAdjusted || hasBenchmark) && <Legend verticalAlign="top" iconType="plainline" iconSize={20} height={42} wrapperStyle={{ fontSize: 14, paddingBottom: 12 }} formatter={(value: string) => SERIES_NAME[value] ?? value} />}
                    <Line type="monotone" dataKey="unitNav" name="unitNav" stroke="var(--chart)" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" dot={chartPointsWithBenchmark.length === 1 ? { r: 3 } : false} activeDot={{ r: 4, fill: "var(--accent)", stroke: "var(--panel)", strokeWidth: 2 }} connectNulls isAnimationActive={false} />
                    {hasAdjusted && <Line type="monotone" dataKey="adjustedNav" name="adjustedNav" stroke="var(--chart-secondary)" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" dot={chartPointsWithBenchmark.length === 1 ? { r: 3 } : false} activeDot={{ r: 4, fill: "var(--chart-secondary)", stroke: "var(--panel)", strokeWidth: 2 }} connectNulls isAnimationActive={false} />}
                    {hasBenchmark && <Line type="monotone" dataKey="benchmark" name="benchmark" stroke="var(--chart-benchmark)" strokeWidth={2.25} strokeDasharray="6 3" strokeLinecap="round" strokeLinejoin="round" dot={false} connectNulls isAnimationActive={false} />}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>

          <div>
            <div className="fd-section__head">
              <div className="fd-section__title">
                回撤
                <small>{drawdownNote ? `按所选区间重算 · ${drawdownNote}` : "按所选区间重算"}</small>
              </div>
            </div>
            {error ? (
              <div className="fd-chart-empty">净值序列加载失败，回撤暂不可用</div>
            ) : showSkeleton ? (
              <Skeleton active paragraph={{ rows: 3 }} />
            ) : (
              <>
                <div className="fd-chart-stats">
                  <div>
                    <span>区间最大回撤</span>
                    <Tooltip title="按所选区间计算：区间内最深的一次回落">
                      <strong className="num" tabIndex={0} style={{ color: returnColor(drawdownText(drawdown.maxDrawdown)) }}>{pctPlain(drawdownText(drawdown.maxDrawdown))}</strong>
                    </Tooltip>
                  </div>
                  <div>
                    <span>区间当前回撤</span>
                    <Tooltip title="按所选区间计算：截止估值日净值距离区间最高净值的跌幅">
                      <strong className="num" tabIndex={0} style={{ color: returnColor(drawdownText(drawdown.currentDrawdown)) }}>{pctPlain(drawdownText(drawdown.currentDrawdown))}</strong>
                    </Tooltip>
                  </div>
                </div>
                {drawdownChart.length === 0 || drawdownUnavailable ? (
                  <div className="fd-chart-empty">{drawdownUnavailable ? drawdown.reason : "该时间范围内没有可用的回撤数据"}</div>
                ) : (
                  <div className="fd-chart fd-chart--short" role="img" aria-label={`区间回撤走势，${RANGE_LABEL[range]}`}>
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={drawdownChart} margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
                        <defs>
                          <linearGradient id="fd-fund-drawdown-gradient" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor="var(--down)" stopOpacity={0} />
                            <stop offset="100%" stopColor="var(--down)" stopOpacity={0.18} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid stroke="var(--border)" strokeOpacity={0.55} vertical={false} />
                        <XAxis dataKey="label" minTickGap={32} tick={axisTick} axisLine={false} tickLine={false} dy={6} />
                        <YAxis domain={[drawdownFloor, 0]} tickFormatter={(value: number) => `${value.toFixed(1)}%`} tick={axisTick} axisLine={false} tickLine={false} width={56} />
                        <ReferenceLine y={0} stroke="var(--border-strong)" />
                        <ChartTooltipBox content={<ChartTooltip format={(value) => [typeof value === "number" ? `${value.toFixed(2)}%` : "—", "区间回撤"]} />} cursor={{ stroke: "var(--border-strong)" }} />
                        <Area type="monotone" dataKey="drawdownPct" name="区间回撤" stroke="var(--down)" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" fill="url(#fd-fund-drawdown-gradient)" dot={drawdownChart.length === 1 ? { r: 3 } : false} activeDot={{ r: 4, fill: "var(--down)", stroke: "var(--panel)", strokeWidth: 2 }} connectNulls={false} isAnimationActive={false} />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </Section>

      <PerformanceSection points={points} summary={performance} loading={showSkeleton} error={error} onRetry={() => void reload()} />

      <Section
        id="nav-table"
        title="净值明细"
        tools={<Button size="small" type="text" icon={<DownloadOutlined />} onClick={() => void exportSeries()}>导出 CSV</Button>}
      >
        <Table<NavPoint>
          className="fd-table--read"
          rowKey="valuation_date"
          size="small"
          loading={loading && !data}
          dataSource={[...visible].reverse()}
          pagination={{ pageSize: 20, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 720 }}
          locale={{ emptyText: <EmptyState compact hint={asOf ? "该日期之前没有已发布的净值记录。" : "该产品尚无已发布的净值记录。"} /> }}
          columns={[
            { title: "估值日", dataIndex: "valuation_date", width: 120, render: (value: string) => <Num className="fd-table-meta">{dateStr(value)}</Num> },
            { title: "单位净值", dataIndex: "unit_nav", align: "right", render: (value: string | null) => <Num>{dec(value, 4)}</Num> },
            { title: "日收益", dataIndex: "daily_return", align: "right", render: (value: string | null) => <Num style={{ color: returnColor(value) }}>{pct(value)}</Num> },
            { title: "累计收益", dataIndex: "cumulative_return", align: "right", render: (value: string | null) => <Num style={{ color: returnColor(value) }}>{pct(value)}</Num> },
            {
              title: "区间回撤",
              key: "range_drawdown",
              align: "right",
              render: (_: unknown, row: NavPoint) => {
                const value = drawdownByDate.get(row.valuation_date) ?? null;
                const text = pctPlain(value === null ? null : String(value));
                return (
                  <Tooltip title={`按所选区间（${RANGE_LABEL[range]}）重算，区间起点 ${drawdown.start ?? "—"}`}>
                    <span tabIndex={0} aria-label={`区间回撤 ${text}`}>
                      <Num style={{ color: returnColor(value === null ? null : String(value)) }}>{text}</Num>
                    </span>
                  </Tooltip>
                );
              },
            },
          ]}
        />
      </Section>
    </>
  );
}
