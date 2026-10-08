import { useState } from "react";
import { Button, Input, Select, Table, Tooltip } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import * as systemApi from "@/api/system";
import type { AuditLog } from "@/api/types";
import { EmptyState, PageError, PageHeader, Section, StatusText, Truncate, useAsyncData, type Tone } from "@/components";
import { AUDIT_RESOURCE_TYPE_LABEL, AUDIT_RESULT_LABEL, type AuditResult } from "@/utils/constants";
import { timeStr } from "@/utils/format";

const PAGE_SIZE = 20;

/** Audit outcome reads as text, not a coloured tag. */
const RESULT_TONE: Record<AuditResult, Tone> = { success: "success", failure: "danger" };

export default function AdminAudit() {
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<systemApi.AuditParams>({});
  const state = useAsyncData(() => systemApi.listAuditLogs({ ...filters, page, page_size: PAGE_SIZE }), [page, filters], "审计日志加载失败");
  const { data, loading, error, reload } = state;
  const filtered = Boolean(filters.action || filters.resource_type || filters.result);

  function updateFilter(key: "action" | "resource_type" | "result", value: string | undefined) {
    setPage(1);
    setFilters((current) => ({ ...current, [key]: value || undefined }));
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="审计日志"
        status={
          <>
            <span>共 <b className="num">{data ? data.meta.total : "—"}</b> 条</span>
            {filtered && <span>已筛选</span>}
          </>
        }
        extra={<Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading}>刷新</Button>}
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
      </PageHeader>

      <Section
        title="记录"
        tools={
          <>
            <Input.Search allowClear placeholder="动作，例如 publish" style={{ width: 220 }} onSearch={(value) => updateFilter("action", value.trim())} aria-label="动作" />
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="资源类型"
              style={{ width: 180 }}
              value={filters.resource_type}
              onChange={(value) => updateFilter("resource_type", value)}
              options={Object.entries(AUDIT_RESOURCE_TYPE_LABEL).map(([value, label]) => ({ value, label }))}
              aria-label="资源类型"
            />
            <Select allowClear placeholder="结果" style={{ width: 120 }} value={filters.result} onChange={(value) => updateFilter("result", value)} options={Object.entries(AUDIT_RESULT_LABEL).map(([value, label]) => ({ value, label }))} aria-label="结果" />
            {filtered && <Button type="link" onClick={() => { setPage(1); setFilters({}); }}>清除筛选</Button>}
          </>
        }
      >
        <Table<AuditLog>
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 1360 }}
          locale={{ emptyText: filtered ? <EmptyState compact variant="filtered" /> : <EmptyState compact title="暂无审计记录" /> }}
          columns={[
            { title: "时间", dataIndex: "created_at", width: 150, render: (value: string) => <span className="num fd-table-meta">{timeStr(value)}</span> },
            { title: "操作人", dataIndex: "actor_user_id", width: 90, render: (value: number | null) => value ? <span className="mono">#{value}</span> : <span className="fd-muted">系统</span> },
            { title: "动作", dataIndex: "action", width: 200, render: (value: string) => <span className="fd-code fd-code--quiet">{value}</span> },
            { title: "资源", dataIndex: "resource_type", width: 120, render: (value: string) => <Tooltip title={value}><span>{AUDIT_RESOURCE_TYPE_LABEL[value] ?? value}</span></Tooltip> },
            { title: "编号", dataIndex: "resource_id", width: 100, render: (value: string | null) => value ? <span className="mono">{value}</span> : "—" },
            { title: "结果", dataIndex: "result", width: 80, render: (value: AuditResult) => <StatusText tone={RESULT_TONE[value] ?? "neutral"}>{AUDIT_RESULT_LABEL[value] ?? value}</StatusText> },
            { title: "原因", dataIndex: "reason", width: 220, render: (value: string | null) => <Truncate value={value ?? ""} maxChars={60} /> },
            // Fixed width: without it the flexible column collapses and `overflow-wrap: anywhere` wraps per character.
            { title: "摘要", dataIndex: "summary", width: 400, render: (value: unknown) => <Truncate className="fd-truncate--code" value={typeof value === "object" && value !== null ? value : String(value ?? "")} maxChars={120} /> },
          ]}
        />
        <p className="fd-form-note">接口仅返回脱敏摘要；密码、令牌、授权码和数据库连接信息不会出现在这里，日志也不可通过页面删除。</p>
      </Section>
    </div>
  );
}
