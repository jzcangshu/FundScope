import { useMemo, useState } from "react";
import { Button, DatePicker, Table, Tooltip as AntdTooltip } from "antd";
import { Area, AreaChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Link } from "react-router-dom";
import { DownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import * as dashboardApi from "@/api/dashboard";
import * as fundsApi from "@/api/funds";
import * as downloads from "@/api/downloads";
import {
  AnalysisBadge,
  ChartTooltip,
  EmptyState,
  Num,
  PageError,
  PageHeader,
  PeriodTabs,
  QualityBadge,
  Stat,
  axisTick,
  useAsyncData,
  useInterval,
  useToast,
} from "@/components";
import { useAuth } from "@/app/auth";
import { can, isProductScoped } from "@/utils/permissions";
import { cutoffForRange, dateStr, dec, fullSeriesStart, pct, pctPlain, returnColor } from "@/utils/format";
import { RANGE_OPTIONS, toAnalysisStatus, type RangeKey } from "@/utils/constants";
import { drawdownAxisFloor, drawdownText, rangeDrawdown, windowPoints } from "@/utils/drawdown";
import type { DashboardFund, DashboardFundDrawdown, DashboardOverviewResponse, DashboardSeriesResponse, FundListItem } from "@/api/types";

/** Silent refresh cadence: faster while analysis is still pending. */
const REFRESH_READY_MS = 60_000;
const REFRESH_PENDING_MS = 15_000;

const RANGE_LABEL = Object.fromEntries(RANGE_OPTIONS.map((option) => [option.value, option.label])) as Record<RangeKey, string>;

interface DashboardData {
  overview: DashboardOverviewResponse;
  series: DashboardSeriesResponse;
  qualityByFund: Map<number, FundListItem["quality_status"]>;
}

/** `asOf` pins every figure to that exact valuation date (backend matches the day, not "latest on or before"). */
async function loadDashboard(asOf?: string): Promise<DashboardData> {
  const [overview, series, funds] = await Promise.all([
    dashboardApi.getOverview(asOf),
    dashboardApi.getSeries({ start: fullSeriesStart(), end: asOf }),
    fundsApi.list({ status: "active", as_of: asOf, page: 1, page_size: 100 }),
  ]);
  return {
    overview,
    series,
    qualityByFund: new Map(funds.data.map((fund) => [fund.id, fund.quality_status])),
  };
}

/** Split "77.45 亿" into figure + unit so the unit can be set smaller. */
function splitMoney(value: string | null | undefined): { figure: string; unit: string } {
  if (value === null || value === undefined || value === "") return { figure: "—", unit: "" };
  const n = Number(value);
  if (!isFinite(n)) return { figure: "—", unit: "" };
  const abs = Math.abs(n);
  if (abs >= 1e8) return { figure: (n / 1e8).toFixed(2), unit: "亿" };
  if (abs >= 1e4) return { figure: (n / 1e4).toFixed(2), unit: "万" };
  return { figure: n.toFixed(2), unit: "元" };
}

export default function Dashboard() {
  const { session } = useAuth();
  const toast = useToast();
  const [range, setRange] = useState<RangeKey>("3m");
  const [asOf, setAsOf] = useState<Dayjs | null>(null);
  const asOfValue = asOf ? asOf.format("YYYY-MM-DD") : undefined;
  const state = useAsyncData(() => loadDashboard(asOfValue), [asOfValue], "总览数据加载失败");
  const { data, loading, error, refreshing, reload } = state;

  const overview = data?.overview.data;
  const meta = data?.overview.meta;
  const analysisStatus = toAnalysisStatus(meta?.analysis_status);
  // A pinned historical date is not waiting on analysis, so it keeps the slow cadence.
  useInterval(() => void reload({ silent: true }), analysisStatus === "ready" || asOfValue ? REFRESH_READY_MS : REFRESH_PENDING_MS, Boolean(data));

  const fundRows = overview?.funds ?? [];
  /** The chosen date has no published valuation at all. */
  const noDataForDate = Boolean(asOfValue) && overview !== undefined && overview.fund_count === 0;
  const latestAsOf = useMemo(
    () => fundRows.reduce<string | null>((latest, fund) => (fund.valuation_date && (!latest || fund.valuation_date > latest) ? fund.valuation_date : latest), null),
    [fundRows],
  );
  const coverage = useMemo(() => {
    const total = meta?.coverage.total ?? fundRows.length;
    const available = latestAsOf ? fundRows.filter((fund) => fund.valuation_date === latestAsOf).length : 0;
    return { available, total };
  }, [fundRows, latestAsOf, meta]);

  const allPoints = data?.series.data.points ?? [];
  // The window is resolved first (baseline valuation on or before the natural
  // day), then every figure below is recomputed from exactly those points.
  const rangeCutoff = useMemo(
    () => cutoffForRange(allPoints[allPoints.length - 1]?.valuation_date, range),
    [allPoints, range],
  );
  const visiblePoints = useMemo(() => windowPoints(allPoints, rangeCutoff), [allPoints, rangeCutoff]);
  const assetsChart = useMemo(
    () => visiblePoints.map((point) => ({ ...point, assetsYi: point.total_assets == null || !Number.isFinite(Number(point.total_assets)) ? null : Number(point.total_assets) / 1e8, label: point.valuation_date.slice(5) })),
    [visiblePoints],
  );
  // Recomputed for the selected window: a cropped pre-computed curve would
  // keep highs from outside the range.
  const companyDrawdown = useMemo(
    () => rangeDrawdown(
      visiblePoints.map((point) => ({
        valuation_date: point.valuation_date,
        value: point.company_index === null ? null : Number(point.company_index),
      })),
    ),
    [visiblePoints],
  );
  const drawdownChart = useMemo(
    () => visiblePoints.map((point, index) => {
      const value = companyDrawdown.points[index]?.drawdown ?? null;
      return { ...point, drawdownPct: value === null ? null : value * 100, label: point.valuation_date.slice(5) };
    }),
    [visiblePoints, companyDrawdown],
  );
  const latestAssetsYi = assetsChart[assetsChart.length - 1]?.assetsYi;
  const hasAssets = assetsChart.some((point) => point.assetsYi !== null);
  // The axis follows the window's own depth so a flat window cannot scale into
  // positive percentages.
  const drawdownFloor = useMemo(
    () => drawdownAxisFloor(drawdownChart.map((point) => point.drawdownPct)),
    [drawdownChart],
  );

  // Benchmark comparison chart: normalize company index and CSI 300 to 100.
  const benchmarkPoints = data?.series.data.benchmark?.points ?? [];
  const benchmarkChart = useMemo(() => {
    if (!visiblePoints.length || !benchmarkPoints.length) return [];
    // Build a map of benchmark close by date for fast lookup.
    const bmByDate = new Map(benchmarkPoints.map((p) => [p.trade_date, Number(p.close)]));
    const bmDates = benchmarkPoints.map((p) => p.trade_date);
    // For each visible point, find the benchmark close on or before that date.
    const aligned: { date: string; company: number | null; benchmark: number | null }[] = [];
    let bmBase: number | null = null;
    let companyBase: number | null = null;
    for (const point of visiblePoints) {
      const companyIdx = point.company_index != null ? Number(point.company_index) : null;
      // Find benchmark close on or before this date.
      let bmClose: number | null = null;
      const directMatch = bmByDate.get(point.valuation_date);
      if (directMatch != null) {
        bmClose = directMatch;
      } else {
        // Binary search for the nearest prior date.
        let lo = 0, hi = bmDates.length - 1, idx = -1;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (bmDates[mid] <= point.valuation_date) { idx = mid; lo = mid + 1; }
          else hi = mid - 1;
        }
        if (idx >= 0) bmClose = bmByDate.get(bmDates[idx]) ?? null;
      }
      // Set base values from the first point that has both.
      if (bmBase === null && bmClose != null && companyIdx != null) {
        bmBase = bmClose;
        companyBase = companyIdx;
      }
      aligned.push({
        date: point.valuation_date.slice(5),
        company: companyIdx != null && companyBase != null && companyBase !== 0
          ? (companyIdx / companyBase) * 100 : null,
        benchmark: bmClose != null && bmBase != null && bmBase !== 0
          ? (bmClose / bmBase) * 100 : null,
      });
    }
    return aligned.filter((p) => p.company !== null || p.benchmark !== null);
  }, [visiblePoints, benchmarkPoints]);
  const hasBenchmark = benchmarkChart.some((p) => p.benchmark !== null);

  const drawdownStats = {
    max: drawdownText(companyDrawdown.maxDrawdown),
    current: drawdownText(companyDrawdown.currentDrawdown),
  };
  const drawdownUnavailable = companyDrawdown.status === "insufficient" || companyDrawdown.status === "invalid";
  const drawdownNote = drawdownUnavailable
    ? companyDrawdown.reason
    : companyDrawdown.status === "incomplete"
      ? "区间内存在缺失净值，回撤仅基于已观测数据"
      : companyDrawdown.start && companyDrawdown.end
        ? `${RANGE_LABEL[range]} · ${companyDrawdown.start} 至 ${companyDrawdown.end}`
        : RANGE_LABEL[range];
  // One batch request per (range, valuation day) for every product row.
  const fundDrawdowns = useAsyncData(
    () => dashboardApi.getFundDrawdowns(range, asOfValue),
    [range, asOfValue],
    "产品区间回撤加载失败",
  );
  const drawdownByFund = useMemo(
    () => new Map((fundDrawdowns.data?.data.funds ?? []).map((fund) => [fund.fund_id, fund])),
    [fundDrawdowns.data],
  );
  const windowNote = fundDrawdowns.data?.data.window_note ?? null;
  const latestIndex = noDataForDate ? null : overview?.company_index ?? null;
  const money = splitMoney(overview?.total_net_assets);
  const riskCount = overview?.risk_event_count ?? 0;
  const scoped = isProductScoped(session);
  const canSeeRisk = can(session?.role, "risk", session?.fund_access_mode);
  const shownAsOf = asOfValue ?? latestAsOf;
  const chartEmptyText = loading ? "加载中…" : asOfValue ? "该日期没有已发布的估值" : null;

  async function exportOverview() {
    try {
      // Keep the CSV on the same window and caliber as the page.
      const query = new URLSearchParams({ range });
      if (asOfValue) query.set("as_of", asOfValue);
      await downloads.downloadBlob(`/exports/overview?${query.toString()}`, "company-overview.csv");
      toast.success("总览已导出");
    } catch {
      toast.error("导出失败，请稍后重试");
    }
  }

  return (
    <div className="fd-page">
      <PageHeader
        title={scoped ? "选定产品总览" : "公司总览"}
        status={
          <>
            <span>截至 <b className="num">{dateStr(shownAsOf)}</b></span>
            <span><b className="num">{overview?.fund_count ?? "—"}</b> 只产品</span>
            {coverage.total > 0 && coverage.available < coverage.total && !noDataForDate && (
              <span><b className="num">{coverage.available}/{coverage.total}</b> {asOfValue ? "有该日估值" : "已更新到最新估值日"}</span>
            )}
            {!noDataForDate && <QualityBadge status={overview?.quality_status ?? "pending"} />}
            {analysisStatus !== "ready" && !noDataForDate && <AnalysisBadge status={meta?.analysis_status} />}
          </>
        }
        extra={
          <>
            <DatePicker value={asOf} onChange={setAsOf} placeholder="指定估值日" allowClear aria-label="估值日" />
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading || refreshing}>刷新</Button>
            <Button icon={<DownloadOutlined />} onClick={() => void exportOverview()}>导出</Button>
          </>
        }
      >
        {scoped && session?.fund_ids.length === 0 && <EmptyState compact title="尚未分配产品" hint="请联系管理员配置可见产品，授权后刷新页面即可查看。" />}
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <div className="fd-stats" role="group" aria-label="核心指标">
          <Stat
            label="管理规模"
            value={<>{money.figure}<small>{money.unit}</small></>}
            loading={loading}
          />
          <Stat
            label="组合日变动"
            value={<span style={{ color: returnColor(overview?.company_daily_return) }}>{pct(overview?.company_daily_return)}</span>}
            sub={
              noDataForDate
                ? "该日期没有已发布的估值"
                : overview?.company_daily_return === null || overview?.company_daily_return === undefined
                  ? analysisStatus === "ready" ? "暂无可比的前一估值日" : "等待分析完成后显示"
                  : `${dateStr(shownAsOf)} 估值日`
            }
            loading={loading}
          />
          <Stat label="组合净值" value={dec(latestIndex, 4)} sub={<>按各产品净资产加权，<span className="fd-nowrap">起点 1.0000</span></>} loading={loading} />
          <Stat
            label="待处理风险"
            value={<span style={{ color: riskCount > 0 ? "var(--accent)" : undefined }}>{overview ? riskCount : "—"}</span>}
            sub={
              riskCount > 0
                ? canSeeRisk ? <Link to="/risk">查看风险事件 →</Link> : "包含待处理与已确认的风险事件"
                : "当前没有待处理的风险事件"
            }
            loading={loading}
          />
        </div>
      </PageHeader>

      <section className="fd-section" aria-label="走势">
        <div className="fd-split fd-split--charts">
          <div>
            <div className="fd-section__head">
              <div className="fd-section__title">
                资产净值走势
                <small>单位：亿{latestAssetsYi != null && <> · 最新 <span className="num">{latestAssetsYi.toFixed(2)}</span> 亿</>}</small>
              </div>
              <PeriodTabs value={range} onChange={setRange} ariaLabel="资产净值走势时间范围" />
            </div>
            {!hasAssets ? (
              <div className="fd-chart-empty">{loading ? "加载中…" : "暂无可用的资产净值历史"}</div>
            ) : (
              <div className="fd-chart" role="img" aria-label="资产净值走势，单位：亿">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={assetsChart} margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
                    <defs>
                      <linearGradient id="fd-assets-gradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--chart)" stopOpacity={0.1} />
                        <stop offset="100%" stopColor="var(--chart)" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="var(--border)" strokeOpacity={0.55} vertical={false} />
                    <XAxis dataKey="label" minTickGap={40} tick={axisTick} axisLine={false} tickLine={false} dy={6} />
                    <YAxis domain={["auto", "auto"]} tickFormatter={(value: number) => `${value.toFixed(2)} 亿`} tick={axisTick} axisLine={false} tickLine={false} width={76} />
                    <Tooltip content={<ChartTooltip format={(value) => [typeof value === "number" ? `${value.toFixed(2)} 亿` : "—", "资产净值"]} />} cursor={{ stroke: "var(--border-strong)" }} />
                    <Area type="monotone" dataKey="assetsYi" name="资产净值" stroke="var(--chart)" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" fill="url(#fd-assets-gradient)" dot={assetsChart.filter((point) => point.assetsYi !== null).length === 1 ? { r: 3 } : false} activeDot={{ r: 4, fill: "var(--accent)", stroke: "var(--panel)", strokeWidth: 2 }} connectNulls={false} isAnimationActive={false} />
                  </AreaChart>
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
            <div className="fd-chart-stats">
              <div>
                <span>区间最大回撤</span>
                <AntdTooltip title="按所选区间计算：区间内最深的一次回落">
                  <strong className="num" tabIndex={0} style={{ color: returnColor(drawdownStats.max) }}>{pctPlain(drawdownStats.max)}</strong>
                </AntdTooltip>
              </div>
              <div>
                <span>区间当前回撤</span>
                <AntdTooltip title={`按所选区间计算：截止估值日净值距离区间最高净值的跌幅${windowNote ? ` · ${windowNote}` : ""}`}>
                  <strong className="num" tabIndex={0} style={{ color: returnColor(drawdownStats.current) }}>{pctPlain(drawdownStats.current)}</strong>
                </AntdTooltip>
              </div>
            </div>
            {drawdownChart.length === 0 || drawdownUnavailable ? (
              <div className="fd-chart-empty">
                {drawdownUnavailable ? companyDrawdown.reason : chartEmptyText ?? "暂无可用的回撤历史"}
              </div>
            ) : (
              <div className="fd-chart fd-chart--short" role="img" aria-label={`组合净值区间回撤走势，${RANGE_LABEL[range]}`}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={drawdownChart} margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
                    <defs>
                      <linearGradient id="fd-drawdown-gradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--down)" stopOpacity={0} />
                        <stop offset="100%" stopColor="var(--down)" stopOpacity={0.18} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="var(--border)" strokeOpacity={0.55} vertical={false} />
                    <XAxis dataKey="label" minTickGap={40} tick={axisTick} axisLine={false} tickLine={false} dy={6} />
                    <YAxis domain={[drawdownFloor, 0]} tickFormatter={(value: number) => `${value.toFixed(1)}%`} tick={axisTick} axisLine={false} tickLine={false} width={56} />
                    <ReferenceLine y={0} stroke="var(--border-strong)" />
                    <Tooltip content={<ChartTooltip format={(value) => [typeof value === "number" ? `${value.toFixed(2)}%` : "—", "区间回撤"]} />} cursor={{ stroke: "var(--border-strong)" }} />
                    <Area type="monotone" dataKey="drawdownPct" name="区间回撤" stroke="var(--down)" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" fill="url(#fd-drawdown-gradient)" dot={drawdownChart.length === 1 ? { r: 3 } : false} activeDot={{ r: 4, fill: "var(--down)", stroke: "var(--panel)", strokeWidth: 2 }} connectNulls={false} isAnimationActive={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        </div>
      </section>

      {hasBenchmark && (
        <section className="fd-section" aria-label="对标走势">
          <div className="fd-section__head">
            <div className="fd-section__title">
              对标走势
              <small>组合 vs 沪深300，以区间首日为基准，归一化至 100</small>
            </div>
          </div>
          <div className="fd-chart fd-chart--short" role="img" aria-label="组合指数与沪深300对标走势">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={benchmarkChart} margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
                <CartesianGrid stroke="var(--border)" strokeOpacity={0.55} vertical={false} />
                <XAxis dataKey="date" minTickGap={40} tick={axisTick} axisLine={false} tickLine={false} dy={6} />
                <YAxis domain={["auto", "auto"]} tick={axisTick} axisLine={false} tickLine={false} width={56} />
                <Tooltip content={<ChartTooltip format={(value, name) => [typeof value === "number" ? value.toFixed(2) : "—", name === "company" ? (scoped ? "授权产品组合" : "组合") : "沪深300"]} />} cursor={{ stroke: "var(--border-strong)" }} />
                <Legend iconType="plainline" iconSize={20} height={42} wrapperStyle={{ fontSize: 14, paddingTop: 18 }} formatter={(value: string) => value === "company" ? (scoped ? "授权产品组合" : "组合") : "沪深300"} />
                <Line type="monotone" dataKey="company" name="company" stroke="var(--chart)" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" dot={false} connectNulls isAnimationActive={false} />
                <Line type="monotone" dataKey="benchmark" name="benchmark" stroke="var(--chart-benchmark)" strokeWidth={2.25} strokeDasharray="6 3" strokeLinecap="round" strokeLinejoin="round" dot={false} connectNulls isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </section>
      )}

      <section className="fd-section" aria-label="产品">
        <div className="fd-section__head">
          <div className="fd-section__title">
            产品
            {overview && <small className="num">{fundRows.length}</small>}
          </div>
          <Link to="/funds">全部产品 →</Link>
        </div>
        <Table<DashboardFund>
          className="fd-table--read"
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={fundRows}
          pagination={false}
          scroll={{ x: 1080 }}
          locale={{
            emptyText: asOfValue ? (
              <EmptyState compact variant="filtered" title="该日期没有已发布的估值" hint="换一个估值日，或清除日期查看最新数据。" action={<Button size="small" onClick={() => setAsOf(null)}>清除日期</Button>} />
            ) : (
              <EmptyState compact hint="尚无已发布的估值版本；导入并发布估值表后，产品会出现在这里。" />
            ),
          }}
          columns={[
            { title: "产品", dataIndex: "name", ellipsis: true, render: (value: string, row) => <Link className="fd-table-name" to={`/funds/${row.id}`}>{value}</Link> },
            { title: "估值日", dataIndex: "valuation_date", width: 120, render: (value: string) => <Num className="fd-table-meta">{dateStr(value)}</Num> },
            { title: "资产净值", dataIndex: "net_asset_value", align: "right", width: 130, render: (value: string | null) => <Num>{compactMoneyText(value)}</Num> },
            { title: "单位净值", dataIndex: "unit_nav", align: "right", width: 110, render: (value: string | null) => <Num>{dec(value, 4)}</Num> },
            { title: "日收益", dataIndex: "daily_return", align: "right", width: 100, render: (value: string | null) => <Num style={{ color: returnColor(value) }}>{pct(value)}</Num> },
            {
              title: "区间当前回撤",
              key: "range_current_drawdown",
              align: "right",
              width: 130,
              render: (_, row) => (
                <DrawdownCell
                  detail={drawdownByFund.get(row.id)}
                  value={drawdownByFund.get(row.id)?.current_drawdown ?? null}
                  rangeLabel={RANGE_LABEL[range]}
                  loading={fundDrawdowns.loading && !fundDrawdowns.data}
                />
              ),
            },
            {
              title: "区间最大回撤",
              key: "range_max_drawdown",
              align: "right",
              width: 130,
              render: (_, row) => (
                <DrawdownCell
                  detail={drawdownByFund.get(row.id)}
                  value={drawdownByFund.get(row.id)?.max_drawdown ?? null}
                  rangeLabel={RANGE_LABEL[range]}
                  loading={fundDrawdowns.loading && !fundDrawdowns.data}
                />
              ),
            },
            { title: "质量", width: 110, render: (_, row) => <QualityBadge status={data?.qualityByFund.get(row.id) ?? "pending"} /> },
            ...(analysisStatus !== "ready" && !noDataForDate
              ? [{ title: "分析", dataIndex: "analysis_status", width: 110, render: (value: string) => <AnalysisBadge status={value} /> }]
              : []),
          ]}
        />
      </section>
    </div>
  );
}

function compactMoneyText(value: string | null | undefined): string {
  const { figure, unit } = splitMoney(value);
  return unit ? `${figure} ${unit}` : figure;
}

/** One product row's interval drawdown: value, real window and why it is missing. */
function DrawdownCell({ detail, value, rangeLabel, loading }: {
  detail: DashboardFundDrawdown | undefined;
  value: string | null;
  rangeLabel: string;
  loading: boolean;
}) {
  if (loading) return <Num style={{ color: "var(--muted)" }}>…</Num>;
  if (!detail) return <Num style={{ color: "var(--muted)" }}>—</Num>;
  const text = pctPlain(value);
  if (value === null) {
    const reason = detail.reason ?? "该区间没有可用的回撤结果";
    return (
      <AntdTooltip title={reason}>
        <span tabIndex={0} aria-label={`${rangeLabel}回撤不可用，${reason}`}>
          <Num style={{ color: "var(--muted)" }}>—</Num>
        </span>
      </AntdTooltip>
    );
  }
  const parts = [`${detail.start ?? "—"} 至 ${detail.end ?? "—"}`, `按所选区间（${rangeLabel}）重算`];
  if (detail.status === "incomplete") parts.push(detail.reason ?? "区间数据不完整，仅基于已观测净值");
  if (detail.note) parts.push(detail.note);
  const tip = parts.join(" · ");
  return (
    <AntdTooltip title={tip}>
      <span tabIndex={0} aria-label={`${rangeLabel}回撤 ${text}，${tip}`}>
        <Num style={{ color: returnColor(value) }}>{text}</Num>
      </span>
    </AntdTooltip>
  );
}
