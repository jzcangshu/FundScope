import { useAuth } from "@/app/auth";
import { isProductScoped } from "@/utils/permissions";
import { useState } from "react";
import { Button, DatePicker, Input, Segmented, Table } from "antd";
import { Link } from "react-router-dom";
import { DownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import * as fundsApi from "@/api/funds";
import * as downloads from "@/api/downloads";
import {
  AnalysisBadge,
  EmptyState,
  Num,
  PageError,
  PageHeader,
  QualityBadge,
  Section,
  StatusText,
  useAsyncData,
  useInterval,
  useToast,
  type Tone,
} from "@/components";
import { dateStr, dec, pct, returnColor } from "@/utils/format";
import { FUND_STATUS_LABEL, toAnalysisStatus, type FundStatus } from "@/utils/constants";
import type { FundListItem } from "@/api/types";

const PAGE_SIZE = 20;

/** Fund status reads as dot + text, never as a tinted tag. */
const FUND_STATUS_TONE: Record<FundStatus, Tone> = { active: "success", inactive: "neutral" };

export default function Funds() {
  const { session } = useAuth();
  const scoped = isProductScoped(session);
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<FundStatus | "all">("all");
  const [asOf, setAsOf] = useState<Dayjs | null>(null);
  const asOfValue = asOf ? asOf.format("YYYY-MM-DD") : undefined;

  const state = useAsyncData(
    () => fundsApi.list({ q: q || undefined, status: status === "all" ? undefined : status, as_of: asOfValue, page, page_size: PAGE_SIZE }),
    [page, q, status, asOfValue],
    "产品列表加载失败",
  );
  const { data, loading, error, refreshing, reload } = state;
  useInterval(() => void reload({ silent: true }), 60_000, Boolean(data));

  const filtered = Boolean(q) || status !== "all" || Boolean(asOfValue);
  // The list endpoint has no page-level analysis status, so derive it per row; an empty page also hides the column.
  const allReady = (data?.data ?? []).every((row) => toAnalysisStatus(row.analysis_status) === "ready");

  async function exportFunds() {
    try {
      const query = asOfValue ? `?as_of=${encodeURIComponent(asOfValue)}` : "";
      await downloads.downloadBlob(`/exports/funds${query}`, "funds.csv");
      toast.success("产品列表已导出");
    } catch {
      toast.error("导出失败，请稍后重试");
    }
  }

  function resetFilters() {
    setPage(1);
    setQ("");
    setStatus("all");
    setAsOf(null);
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="产品列表"
        status={
          <>
            <span>共 <b className="num">{data?.meta.total ?? "—"}</b> 只产品</span>
            {asOfValue && <span>估值日 <b className="num">{asOfValue}</b></span>}
            {filtered && <span>已筛选</span>}
          </>
        }
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading || refreshing}>刷新</Button>
            <Button icon={<DownloadOutlined />} onClick={() => void exportFunds()}>导出产品列表</Button>
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
      </PageHeader>

      <Section
        title="产品"
        tools={
          <>
            <Input.Search
              allowClear
              placeholder="搜索产品名称"
              style={{ width: 240 }}
              onSearch={(value) => { setPage(1); setQ(value.trim()); }}
              aria-label="搜索产品名称"
            />
            <Segmented
              value={status}
              onChange={(value) => { setPage(1); setStatus(value as FundStatus | "all"); }}
              options={[
                { value: "all", label: "全部" },
                { value: "active", label: "启用" },
                { value: "inactive", label: "停用" },
              ]}
              aria-label="产品状态"
            />
            <DatePicker
              value={asOf}
              onChange={(value) => { setPage(1); setAsOf(value); }}
              placeholder="指定估值日"
              allowClear
              aria-label="估值日"
            />
            {filtered && <Button type="link" onClick={resetFilters}>清除筛选</Button>}
          </>
        }
      >
        <Table<FundListItem>
          className="fd-table--read"
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 740 }}
          locale={{
            emptyText: filtered ? (
              <EmptyState compact variant="filtered" hint="调整筛选条件或指定其他估值日。" action={<Button size="small" onClick={resetFilters}>清除筛选</Button>} />
            ) : (
              <EmptyState compact title={scoped && session?.fund_ids.length === 0 ? "尚未分配产品" : undefined} hint={scoped ? "暂无已授权产品或已发布估值；请联系管理员确认产品范围。" : "尚无产品或已发布估值；在产品管理中新增产品并导入估值表后会出现在这里。"} />
            ),
          }}
          columns={[
            { title: "产品名称", dataIndex: "name", ellipsis: true, render: (value: string, row) => <Link className="fd-table-name" to={`/funds/${row.id}`}>{value}</Link> },
            {
              title: "状态",
              dataIndex: "status",
              width: 90,
              render: (value: FundStatus) => (
                <StatusText tone={FUND_STATUS_TONE[value]} muted={value !== "active"}>{FUND_STATUS_LABEL[value]}</StatusText>
              ),
            },
            { title: "估值日", dataIndex: "valuation_date", width: 120, render: (value: string | null) => <Num className="fd-table-meta">{dateStr(value)}</Num> },
            { title: "单位净值", dataIndex: "unit_nav", align: "right", width: 110, render: (value: string | null) => <Num>{dec(value, 4)}</Num> },
            { title: "日收益", dataIndex: "daily_return", align: "right", width: 100, render: (value: string | null) => <Num style={{ color: returnColor(value) }}>{pct(value)}</Num> },
            { title: "质量", dataIndex: "quality_status", width: 120, render: (value: FundListItem["quality_status"]) => <QualityBadge status={value} /> },
            ...(allReady
              ? []
              : [{ title: "分析状态", dataIndex: "analysis_status", width: 120, render: (value: string) => <AnalysisBadge status={value} /> }]),
          ]}
        />
      </Section>
    </div>
  );
}
