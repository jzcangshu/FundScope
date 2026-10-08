import { useState } from "react";
import { Button, Form, Input, InputNumber, Modal, Popconfirm, Select, Switch, Table, Tooltip } from "antd";
import { PlayCircleOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import * as riskApi from "@/api/risk";
import type { RiskRule } from "@/api/types";
import { Chip, EmptyState, PageError, PageHeader, Section, StatusText, describeError, useAsyncData, useToast, type Tone } from "@/components";
import { useAuth } from "@/app/auth";
import { can } from "@/utils/permissions";
import { RISK_RULE_TYPES, RISK_RULE_TYPE_LABEL, RISK_SEVERITY_LABEL, type RiskSeverity } from "@/utils/constants";
import { dateStr, pctPlain } from "@/utils/format";

const PAGE_SIZE = 20;
const SEVERITY_TONE: Record<RiskSeverity, Tone> = { critical: "danger", warning: "warning", info: "neutral" };

interface RuleFormValues {
  rule_code: string;
  rule_type: string;
  scope: string;
  threshold: number;
  severity: RiskSeverity;
  enabled: boolean;
}

export default function AdminRiskRules() {
  const { session } = useAuth();
  const canWrite = can(session?.role, "manageRiskRules", session?.fund_access_mode);
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [includeHistory, setIncludeHistory] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<RiskRule | null>(null);
  const [saving, setSaving] = useState(false);
  const [recalculating, setRecalculating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [form] = Form.useForm<RuleFormValues>();

  const state = useAsyncData(() => riskApi.listRules({ include_history: includeHistory, page, page_size: PAGE_SIZE }), [page, includeHistory], "风险规则加载失败");
  const { data, loading, error, reload } = state;

  function openCreate() {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ rule_code: `CUSTOM-${Date.now()}`, scope: "all", severity: "warning", enabled: true });
    setOpen(true);
  }

  function openEdit(row: RiskRule) {
    setEditing(row);
    form.setFieldsValue({ rule_code: row.rule_code, rule_type: row.rule_type, scope: row.scope, threshold: Number(row.threshold) * 100, severity: row.severity, enabled: row.enabled });
    setOpen(true);
  }

  async function save() {
    let values: RuleFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    setActionError(null);
    try {
      const payload = { ...values, threshold: Number(values.threshold) / 100 };
      if (editing) {
        const { rule_code: _ruleCode, ...changes } = payload;
        await riskApi.updateRule(editing.id, changes);
      } else {
        await riskApi.createRule(payload);
      }
      toast.success("风险规则已保存并生成新版本");
      setOpen(false);
      setEditing(null);
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "风险规则保存失败，请检查输入或版本冲突"));
    } finally {
      setSaving(false);
    }
  }

  async function recalculate() {
    setRecalculating(true);
    setActionError(null);
    try {
      const result = await riskApi.recalculateRules();
      toast.success(`已排队 ${result.data.queued_count} 只产品的全历史重算，最新规则将在后台逐日应用，不再命中的历史预警会自动关闭`);
    } catch (caught) {
      setActionError(describeError(caught, "规则重算排队失败，请稍后重试"));
    } finally {
      setRecalculating(false);
    }
  }

  async function disable(row: RiskRule) {
    setActionError(null);
    try {
      await riskApi.disableRule(row);
      toast.success("风险规则已停用，并生成新的停用版本");
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "风险规则停用失败，请稍后重试"));
    }
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="风险规则"
        status={
          <>
            <span>共 <b className="num">{data ? data.meta.total : "—"}</b> 条</span>
            {includeHistory && <span>含历史版本</span>}
          </>
        }
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading}>刷新</Button>
            {canWrite && (
              <Popconfirm
                title="应用最新规则到历史数据"
                description="将对所有启用产品排队全历史重算：按最新规则重新评估每个历史交易日，不再命中的历史预警自动关闭。重算在后台执行，期间可正常使用。"
                okText="开始重算"
                cancelText="取消"
                onConfirm={() => void recalculate()}
              >
                <Button icon={<PlayCircleOutlined />} loading={recalculating}>应用到历史数据</Button>
              </Popconfirm>
            )}
            {canWrite && <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建规则</Button>}
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <Section
        title="规则"
        tools={
          <label className="fd-inline-actions">
            <Switch size="small" checked={includeHistory} onChange={(value) => { setPage(1); setIncludeHistory(value); }} />
            <span>显示历史版本</span>
          </label>
        }
      >
        <Table<RiskRule>
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 820 }}
          locale={{ emptyText: <EmptyState compact title="暂无风险规则" hint={canWrite ? "点击“新建规则”设置第一条触发阈值。" : "尚未配置任何风险规则。"} /> }}
          columns={[
            { title: "风险指标", dataIndex: "rule_type", render: (value: string, row) => <Tooltip title={`规则编码 ${row.rule_code}`}><span className="fd-table-name">{RISK_RULE_TYPE_LABEL[value as keyof typeof RISK_RULE_TYPE_LABEL] ?? value}</span></Tooltip> },
            { title: "触发阈值", dataIndex: "threshold", width: 120, align: "right", render: (value: string) => <span className="num">{pctPlain(value)}</span> },
            { title: "风险级别", dataIndex: "severity", width: 100, render: (value: RiskSeverity) => <StatusText tone={SEVERITY_TONE[value]}>{RISK_SEVERITY_LABEL[value]}</StatusText> },
            { title: "版本", dataIndex: "version", width: 90, render: (value: string) => <span className="mono">{value}</span> },
            { title: "生效自", dataIndex: "valid_from", width: 120, render: (value: string) => <span className="num fd-table-meta">{dateStr(value)}</span> },
            { title: "失效于", dataIndex: "valid_to", width: 120, render: (value: string | null) => value ? <span className="num fd-table-meta">{dateStr(value)}</span> : <Chip>当前</Chip> },
            { title: "状态", dataIndex: "enabled", width: 80, render: (value: boolean) => value ? <StatusText tone="success">启用</StatusText> : <StatusText tone="neutral" muted>停用</StatusText> },
            ...(canWrite
              ? [{
                  title: "操作",
                  width: 150,
                  render: (_: unknown, row: RiskRule) => (
                    <span className="fd-row-actions">
                      <Button type="link" size="small" disabled={Boolean(row.valid_to)} onClick={() => openEdit(row)}>修改</Button>
                      <Popconfirm
                        title="停用这条风险规则？"
                        description="停用会生成新的停用版本，保留历史版本和已产生的风险事件。"
                        okText="停用"
                        cancelText="取消"
                        onConfirm={() => void disable(row)}
                      >
                        <Button type="link" danger size="small" disabled={Boolean(row.valid_to) || !row.enabled}>停用</Button>
                      </Popconfirm>
                    </span>
                  ),
                }]
              : []),
          ]}
        />
        {canWrite && <p className="fd-form-note">每次修改都会生成新版本；编辑完成后点击「应用到历史数据」，即可按最新规则统一重算全部历史。</p>}
      </Section>

      {canWrite && (
        <Modal open={open} title={editing ? "修改规则" : "新建风险规则"} onCancel={() => setOpen(false)} onOk={() => void save()} okText="保存" okButtonProps={{ loading: saving }} destroyOnClose>
          <Form form={form} layout="vertical">
            <Form.Item hidden name="rule_code"><Input /></Form.Item>
            <Form.Item hidden name="scope"><Input /></Form.Item>
            <Form.Item label="风险指标" name="rule_type" rules={[{ required: true, message: "请选择风险指标" }]}>
              <Select disabled={Boolean(editing)} options={RISK_RULE_TYPES.map((value) => ({ value, label: RISK_RULE_TYPE_LABEL[value] }))} />
            </Form.Item>
            <Form.Item label="触发阈值" name="threshold" rules={[{ required: true, message: "请输入阈值" }]} extra="以百分比填写，例如 -3 表示日收益低于 -3% 触发；权重类指标填写正数。">
              <InputNumber min={-100} max={100} precision={2} addonAfter="%" style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item label="风险级别" name="severity" rules={[{ required: true, message: "请选择风险级别" }]}>
              <Select options={Object.entries(RISK_SEVERITY_LABEL).map(([value, label]) => ({ value, label }))} />
            </Form.Item>
            <Form.Item label="启用" name="enabled" valuePropName="checked"><Switch /></Form.Item>
          </Form>
        </Modal>
      )}
    </div>
  );
}
