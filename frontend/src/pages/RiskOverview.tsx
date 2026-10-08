import { useEffect, useMemo, useState } from "react";
import { Button, Form, Input, Modal, Select, Table, Tooltip } from "antd";
import { Link, useSearchParams } from "react-router-dom";
import { ReloadOutlined } from "@ant-design/icons";
import * as riskApi from "@/api/risk";
import * as fundsApi from "@/api/funds";
import type { RiskEvent } from "@/api/types";
import {
  EmptyState,
  PageError,
  PageHeader,
  Section,
  StatusText,
  Truncate,
  describeError,
  useAsyncData,
  useInterval,
  useToast,
  type Tone,
} from "@/components";
import { dateStr, timeStr } from "@/utils/format";
import { useAuth } from "@/app/auth";
import { can } from "@/utils/permissions";
import {
  RISK_EVENT_STATUS_LABEL,
  RISK_RULE_TYPE_LABEL,
  RISK_SEVERITY_LABEL,
  type RiskEventStatus,
  type RiskSeverity,
} from "@/utils/constants";

const PAGE_SIZE = 20;

const SEVERITY_TONE: Record<RiskSeverity, Tone> = { critical: "danger", warning: "warning", info: "info" };
const STATUS_TONE: Record<RiskEventStatus, Tone> = { open: "danger", acknowledged: "warning", resolved: "success", ignored: "neutral" };

function typeLabel(type: string | undefined): string | null {
  return type && type in RISK_RULE_TYPE_LABEL ? RISK_RULE_TYPE_LABEL[type as keyof typeof RISK_RULE_TYPE_LABEL] : null;
}

/**
 * Events only carry `risk_rule_id` / `rule_code`; resolve the human label through
 * the rule catalogue, falling back to a keyword match on the code (e.g. "DEMO-DRAWDOWN").
 */
function ruleLabel(event: Pick<RiskEvent, "risk_rule_id" | "rule_code">, ruleTypeById: Map<number, string>): string {
  const byId = typeLabel(ruleTypeById.get(event.risk_rule_id));
  if (byId) return byId;
  const code = event.rule_code;
  if (!code) return "—";
  const lower = code.toLowerCase();
  const type = Object.keys(RISK_RULE_TYPE_LABEL).find((key) => lower.includes(key)) ?? (lower.includes("drawdown") ? "max_drawdown" : lower.includes("daily") ? "daily_return" : undefined);
  return typeLabel(type) ?? code;
}

export default function RiskOverview() {
  const toast = useToast();
  const { session } = useAuth();
  const canWrite = can(session?.role, "write", session?.fund_access_mode);
  const [params, setParams] = useSearchParams();
  const fundParam = params.get("fund");
  const fundId = fundParam && Number.isInteger(Number(fundParam)) && Number(fundParam) > 0 ? Number(fundParam) : undefined;
  const [page, setPage] = useState(1);
  const [severity, setSeverity] = useState<RiskSeverity | undefined>();
  const [status, setStatus] = useState<RiskEventStatus | undefined>();
  const [editing, setEditing] = useState<RiskEvent | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [form] = Form.useForm<{ status: RiskEventStatus; handling_note: string; evidence_reference?: string }>();

  useEffect(() => setPage(1), [fundId]);

  const state = useAsyncData(
    () => riskApi.listEvents({ fund_id: fundId, severity, status, page, page_size: PAGE_SIZE }),
    [fundId, severity, status, page],
    "风险事件加载失败",
  );
  const { data, loading, error, refreshing, reload } = state;
  useInterval(() => void reload({ silent: true }), 60_000, Boolean(data));

  const rulesState = useAsyncData(() => riskApi.listRules({ include_history: true, page: 1, page_size: 100 }), [], "风险规则加载失败");
  const ruleTypeById = useMemo(() => new Map((rulesState.data?.data ?? []).map((rule) => [rule.id, rule.rule_type])), [rulesState.data]);
  const fundsState = useAsyncData(() => fundsApi.list({ page: 1, page_size: 100 }), [], "产品列表加载失败");
  const fundOptions = (fundsState.data?.data ?? []).map((fund) => ({ value: fund.id, label: fund.name }));
  const filtered = Boolean(fundId) || Boolean(severity) || Boolean(status);

  function setFund(next?: number) {
    const nextParams = new URLSearchParams(params);
    if (next) nextParams.set("fund", String(next));
    else nextParams.delete("fund");
    setParams(nextParams, { replace: true });
  }

  function resetFilters() {
    setPage(1);
    setSeverity(undefined);
    setStatus(undefined);
    setFund(undefined);
  }

  async function submit() {
    if (!editing) return;
    let values: { status: RiskEventStatus; handling_note: string; evidence_reference?: string };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    setActionError(null);
    try {
      await riskApi.handleEvent(editing.id, {
        status: values.status,
        handling_note: values.handling_note.trim(),
        evidence_reference: values.evidence_reference?.trim() || undefined,
      });
      toast.success("处理结果已记录");
      setEditing(null);
      form.resetFields();
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "风险事件处理失败，请检查输入后重试"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="风险事件"
        status={
          <>
            <span>共 <b className="num">{data ? data.meta.total : "—"}</b> 条事件</span>
            {filtered && <span>已筛选</span>}
          </>
        }
        extra={<Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading || refreshing}>刷新</Button>}
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <Section
        title="事件"
        tools={
          <>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="全部产品"
              style={{ width: 240 }}
              value={fundId}
              loading={fundsState.loading}
              options={fundOptions}
              onChange={(value) => { setPage(1); setFund(value); }}
              aria-label="按产品筛选"
            />
            <Select
              allowClear
              placeholder="严重度"
              style={{ width: 130 }}
              value={severity}
              onChange={(value) => { setPage(1); setSeverity(value); }}
              options={Object.entries(RISK_SEVERITY_LABEL).map(([value, label]) => ({ value, label }))}
              aria-label="严重度"
            />
            <Select
              allowClear
              placeholder="处理状态"
              style={{ width: 130 }}
              value={status}
              onChange={(value) => { setPage(1); setStatus(value); }}
              options={Object.entries(RISK_EVENT_STATUS_LABEL).map(([value, label]) => ({ value, label }))}
              aria-label="处理状态"
            />
            {filtered && <Button type="link" onClick={resetFilters}>清除筛选</Button>}
          </>
        }
      >
        <Table<RiskEvent>
          className="fd-table--read"
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 820 }}
          locale={{
            emptyText: filtered
              ? <EmptyState compact variant="filtered" hint="没有符合当前筛选条件的风险事件。" action={<Button size="small" onClick={resetFilters}>清除筛选</Button>} />
              : <EmptyState compact title="暂无风险事件" hint="分析完成后若触发启用中的风险规则，事件会出现在这里。" />,
          }}
          columns={[
            { title: "产品", dataIndex: "fund_name", ellipsis: true, render: (value: string | null, row) => row.fund_id ? <Link className="fd-table-name" to={`/funds/${row.fund_id}`}>{value ?? `#${row.fund_id}`}</Link> : <span className="fd-table-name">{value ?? "—"}</span> },
            { title: "估值日", dataIndex: "valuation_date", width: 120, render: (value: string, row) => <Tooltip title={`首次触发 ${timeStr(row.first_triggered_at)}`}><span className="num fd-table-meta">{dateStr(value)}</span></Tooltip> },
            { title: "规则", dataIndex: "rule_code", width: 160, render: (value: string | null, row) => <Tooltip title={value ? `规则编码 ${value}` : undefined}><span>{ruleLabel(row, ruleTypeById)}</span></Tooltip> },
            { title: "严重度", dataIndex: "severity", width: 90, render: (value: RiskSeverity) => <StatusText tone={SEVERITY_TONE[value]}>{RISK_SEVERITY_LABEL[value]}</StatusText> },
            { title: "状态", dataIndex: "status", width: 100, render: (value: RiskEventStatus) => <StatusText tone={STATUS_TONE[value]} muted={value === "ignored"}>{RISK_EVENT_STATUS_LABEL[value]}</StatusText> },
            { title: "处理意见", dataIndex: "handling_note", render: (value: string | null) => <Truncate value={value ?? ""} maxChars={60} /> },
            ...(canWrite ? [{
              title: "操作",
              width: 90,
              render: (_: unknown, row: RiskEvent) => (
                <span className="fd-row-actions">
                  <Button type="link" size="small" disabled={row.status !== "open" && row.status !== "acknowledged"} onClick={() => { form.resetFields(); setEditing(row); }}>处理</Button>
                </span>
              ),
            }] : []),
          ]}
        />
        <p className="fd-form-note">处理结果、意见与证据引用会写入审计记录。</p>
      </Section>

      <Modal
        open={editing !== null}
        title="处理风险事件"
        onCancel={() => setEditing(null)}
        onOk={() => void submit()}
        okText="提交处理"
        okButtonProps={{ loading: submitting }}
        destroyOnClose
      >
        {editing && (
          <p className="fd-modal-note">
            {editing.fund_name ?? "—"} · {dateStr(editing.valuation_date)} · {ruleLabel(editing, ruleTypeById)} · {RISK_SEVERITY_LABEL[editing.severity]}
          </p>
        )}
        <Form form={form} layout="vertical" initialValues={{ status: "acknowledged" }}>
          <Form.Item name="status" label="处理结果" rules={[{ required: true, message: "请选择处理结果" }]}>
            <Select options={(["acknowledged", "resolved", "ignored"] as RiskEventStatus[]).map((value) => ({ value, label: RISK_EVENT_STATUS_LABEL[value] }))} />
          </Form.Item>
          <Form.Item name="handling_note" label="处理意见" rules={[{ required: true, whitespace: true, message: "请填写处理意见" }]}>
            <Input.TextArea rows={4} maxLength={4000} showCount placeholder="说明核实过程与结论，将记入审计日志" />
          </Form.Item>
          <Form.Item name="evidence_reference" label="证据引用（可选）">
            <Input maxLength={1000} placeholder="例如：原始估值表文件名、批次号或复核记录" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
