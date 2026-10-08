import { useState } from "react";
import { Button, DatePicker, Skeleton } from "antd";
import { Link, Navigate, useParams } from "react-router-dom";
import type { Dayjs } from "dayjs";
import * as fundsApi from "@/api/funds";
import type { FundDetail as FundDetailData } from "@/api/types";
import {
  AnalysisBadge,
  ErrorState,
  Num,
  PageError,
  PageHeader,
  QualityBadge,
  Stat,
  StatusText,
  useAsyncData,
  useInterval,
} from "@/components";
import { useAuth } from "@/app/auth";
import { can } from "@/utils/permissions";
import { compactMoney, dateStr, dec, pct, returnColor } from "@/utils/format";
import { FUND_STATUS_LABEL, toAnalysisStatus } from "@/utils/constants";
import NavSeriesSection from "./tabs/NavSeries";
import { QualitySection } from "./tabs/Quality";

const BACK = { to: "/funds", label: "产品列表" };

export default function FundDetail() {
  const { id } = useParams();
  const { session } = useAuth();
  const rawId = Number(id);
  const fundId = Number.isInteger(rawId) && rawId > 0 ? rawId : NaN;
  // Pins the whole page (headline figures, positions, quality) to one exact valuation date.
  const [asOf, setAsOf] = useState<Dayjs | null>(null);
  const asOfValue = asOf ? asOf.format("YYYY-MM-DD") : undefined;
  const state = useAsyncData(() => fundsApi.detail(fundId, asOfValue).then((result) => result.data), [fundId, asOfValue], "产品详情加载失败");
  const { data: fund, loading, error, reload } = state;
  const analysis = toAnalysisStatus(fund?.analysis_status);
  useInterval(() => void reload({ silent: true }), analysis === "ready" || asOfValue ? 60_000 : 15_000, Boolean(fund));

  if (!Number.isInteger(fundId)) return <Navigate to="/funds" replace />;

  if (error && !fund) {
    return (
      <div className="fd-page">
        <PageHeader title="产品详情" back={BACK} />
        <section className="fd-section"><ErrorState message={error} onRetry={() => void reload()} retrying={loading} /></section>
      </div>
    );
  }

  if (!fund) {
    return (
      <div className="fd-page" aria-busy="true">
        <PageHeader title={<Skeleton.Input active style={{ width: 240 }} />} back={BACK}>
          <div className="fd-stats">
            {[0, 1, 2, 3].map((index) => <Stat key={index} label={<span>&nbsp;</span>} value="" loading />)}
          </div>
        </PageHeader>
        <section className="fd-section"><Skeleton active paragraph={{ rows: 6 }} /></section>
      </div>
    );
  }

  /** The chosen date has no published version for this fund; the basics still come back, figures are null. */
  const noVersionForDate = Boolean(asOfValue) && !fund.valuation_date;
  const valuationDate = fund.id === fundId && (!asOfValue || fund.valuation_date === asOfValue) ? fund.valuation_date : null;
  const sections = [
    { id: "nav", label: "净值走势" },
    { id: "performance", label: "收益与回撤" },
    { id: "nav-table", label: "净值明细" },
    { id: "quality", label: "数据质量" },
  ];
  const datePicker = <DatePicker value={asOf} onChange={setAsOf} placeholder="指定估值日" allowClear aria-label="估值日" />;

  return (
    <div className="fd-page">
      <PageHeader
        back={BACK}
        title={
          <span className="fd-detail-title">
            {fund.name}
            {fund.status !== "active" && <StatusText tone="neutral" muted>{FUND_STATUS_LABEL[fund.status]}</StatusText>}
          </span>
        }
        status={
          <>
            <span>估值日 <b className="num">{dateStr(fund.valuation_date)}</b></span>
            {!noVersionForDate && <QualityBadge status={fund.quality_status} />}
            {analysis !== "ready" && !noVersionForDate && <AnalysisBadge status={fund.analysis_status} />}
          </>
        }
        extra={
          <>
            {datePicker}
            {can(session?.role, "risk", session?.fund_access_mode) && <Link to={`/risk?fund=${fund.id}`}>该产品的风险事件 →</Link>}
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        {noVersionForDate && (
          <p className="fd-note">
            该日期没有已发布的估值。
            <Button type="link" size="small" onClick={() => setAsOf(null)}>清除日期</Button>
          </p>
        )}
        <div className="fd-stats" role="group" aria-label="核心指标">
          <Stat label="资产净值" value={<Money value={fund.net_asset_value} />} sub={fund.total_assets ? <>总资产 <span className="num">{compactMoney(fund.total_assets)}</span></> : undefined} />
          <Stat
            label="单位净值"
            value={dec(fund.unit_nav, 4)}
            sub={fund.cumulative_unit_nav ? <>累计单位净值 <span className="num">{dec(fund.cumulative_unit_nav, 4)}</span></> : undefined}
          />
          <Stat label="日收益" value={<span style={{ color: returnColor(fund.daily_return) }}>{pct(fund.daily_return)}</span>} sub={<PeriodReturns fund={fund} />} />
          <Stat
            label="累计收益"
            value={<span style={{ color: returnColor(fund.cumulative_return) }}>{pct(fund.cumulative_return)}</span>}
            sub={fund.establishment_date ? <>自 <span className="num">{dateStr(fund.establishment_date)}</span> 成立</> : "自成立以来"}
          />
        </div>

        <nav className="fd-anchor-nav" aria-label="页内导航">
          {sections.map((section) => <a key={section.id} href={`#${section.id}`}>{section.label}</a>)}
        </nav>
      </PageHeader>

      <NavSeriesSection
        key={`${fundId}:${asOfValue ?? "latest"}:${valuationDate ?? "none"}`}
        fundId={fundId}
        asOf={asOfValue}
        valuationDate={valuationDate}
        performance={{
          valuationDate,
          cumulativeReturn: fund.period_returns?.cumulative ?? null,
          establishmentDate: fund.establishment_date,
          benchmarkReturns: fund.period_returns?.benchmark_returns ?? null,
          inceptionDrawdown: fund.inception_drawdown ?? null,
        }}
      />
      <QualitySection fundId={fundId} asOf={asOfValue} />
    </div>
  );
}

/** "77.45 亿" with the unit set small, like the overview's headline figures. */
function Money({ value }: { value: string | null | undefined }) {
  const text = compactMoney(value);
  const match = /^(.*?)\s*([亿万元]+)$/.exec(text);
  if (!match) return <>{text}</>;
  return <>{match[1]}<small>{match[2]}</small></>;
}

function PeriodReturns({ fund }: { fund: FundDetailData }) {
  const items: Array<[string, string | null]> = [
    ["本周", fund.period_returns?.wtd ?? null],
    ["本月", fund.period_returns?.mtd ?? null],
    ["今年", fund.period_returns?.ytd ?? null],
  ];
  if (items.every(([, value]) => value === null)) return fund.valuation_date ? <>{dateStr(fund.valuation_date)} 估值日</> : null;
  return (
    <span className="fd-period-returns">
      {items.map(([label, value]) => (
        <span className="fd-period-returns__item" key={label}>
          <span className="fd-period-returns__label">{label}</span>
          <Num style={{ color: returnColor(value) }}>{pct(value)}</Num>
        </span>
      ))}
    </span>
  );
}
