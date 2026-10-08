import { useEffect, useState } from "react";
import { Button, Table } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import * as fundsApi from "@/api/funds";
import * as downloads from "@/api/downloads";
import type { Position } from "@/api/types";
import { EmptyState, ErrorState, Num, Section, useAsyncData, useToast } from "@/components";
import { dateStr, dec, weight } from "@/utils/format";

const PAGE_SIZE = 50;

/** `asOf` selects the published version of that exact date; the backend returns no rows when there is none. */
export function PositionsSection({ fundId, asOf }: { fundId: number; asOf?: string }) {
  const toast = useToast();
  const [page, setPage] = useState(1);
  useEffect(() => setPage(1), [fundId, asOf]);
  const state = useAsyncData(() => fundsApi.positions(fundId, { as_of: asOf, page, page_size: PAGE_SIZE }), [fundId, asOf, page], "持仓加载失败");
  const { data, loading, error, reload } = state;
  const valuationDate = typeof data?.meta.valuation_date === "string" ? data.meta.valuation_date : null;

  async function exportPositions() {
    try {
      const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
      await downloads.downloadBlob(`/exports/funds/${fundId}/positions${query}`, `fund-${fundId}-positions.csv`);
      toast.success("持仓已导出");
    } catch {
      toast.error("导出失败，请稍后重试");
    }
  }

  return (
    <Section
      id="positions"
      title="持仓"
      count={valuationDate ? <>估值日 <span className="num">{dateStr(valuationDate)}</span></> : undefined}
      tools={<Button size="small" type="text" icon={<DownloadOutlined />} onClick={() => void exportPositions()}>导出 CSV</Button>}
    >
      {error ? (
        <ErrorState message={error} onRetry={() => void reload()} retrying={loading} />
      ) : (
        <Table<Position>
          className="fd-table--read"
          rowKey={(row, index) => `${row.security_code ?? ""}-${row.account ?? ""}-${index ?? 0}`}
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 700 }}
          locale={{ emptyText: <EmptyState compact hint={asOf && !valuationDate ? "该日期没有已发布的估值。" : "该版本没有叶子持仓记录，或科目映射未将其纳入持仓。"} /> }}
          columns={[
            { title: "证券代码", dataIndex: "security_code", width: 120, render: (value: string | null) => <span className="mono fd-table-meta">{value ?? "—"}</span> },
            { title: "证券名称", dataIndex: "security_name", ellipsis: true, render: (value: string | null) => <span className="fd-table-name">{value ?? "—"}</span> },
            { title: "数量", dataIndex: "quantity", align: "right", width: 130, render: (value: string | null) => <Num>{dec(value, 0)}</Num> },
            { title: "市价", dataIndex: "market_price", align: "right", width: 110, render: (value: string | null) => <Num>{dec(value, 2)}</Num> },
            { title: "市值", dataIndex: "market_value", align: "right", width: 150, render: (value: string | null) => <Num>{dec(value, 2)}</Num> },
            { title: "净值权重", dataIndex: "nav_weight", align: "right", width: 110, render: (value: string | null) => <Num>{weight(value)}</Num> },
          ]}
        />
      )}
    </Section>
  );
}
