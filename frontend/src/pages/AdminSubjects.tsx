import { useState } from "react";
import { Button, Form, Input, Modal, Segmented, Switch, Table } from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import * as catalogApi from "@/api/catalog";
import type { SubjectMapping } from "@/api/types";
import {
  EmptyState,
  PageError,
  PageHeader,
  Section,
  StatusText,
  describeError,
  useAsyncData,
  useConfirm,
  useToast,
  type Tone,
} from "@/components";
import { MAPPING_STATUS_LABEL, type MappingStatus } from "@/utils/constants";

const PAGE_SIZE = 20;

/** Mapping status reads as text, not as a coloured tag. */
const MAPPING_TONE: Record<MappingStatus, Tone> = { active: "success", inactive: "neutral" };

export default function AdminSubjects() {
  const toast = useToast();
  const confirm = useConfirm();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<MappingStatus | "all">("active");
  const [editing, setEditing] = useState<SubjectMapping | null>(null);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [form] = Form.useForm<catalogApi.SubjectMappingInput>();

  const state = useAsyncData(
    () => catalogApi.listMappings({ status: status === "all" ? undefined : status, page, page_size: PAGE_SIZE }),
    [page, status],
    "科目映射加载失败",
  );
  const { data, loading, error, reload } = state;

  function openCreate() {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ is_leaf: true, include_in_holdings: false, rule_version: "1" });
    setOpen(true);
  }

  function openEdit(row: SubjectMapping) {
    setEditing(row);
    form.setFieldsValue({
      subject_code_or_prefix: row.subject_code_or_prefix,
      raw_name_pattern: row.raw_name_pattern,
      standard_category: row.standard_category,
      is_leaf: row.is_leaf,
      include_in_holdings: row.include_in_holdings,
      rule_version: row.rule_version,
    });
    setOpen(true);
  }

  async function save() {
    let values: catalogApi.SubjectMappingInput;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    if (!values.subject_code_or_prefix?.trim() && !values.raw_name_pattern?.trim()) {
      setActionError("科目代码/前缀与原始名称匹配至少填写一项");
      return;
    }
    setSaving(true);
    setActionError(null);
    try {
      const payload: catalogApi.SubjectMappingInput = {
        ...values,
        subject_code_or_prefix: values.subject_code_or_prefix?.trim() || null,
        raw_name_pattern: values.raw_name_pattern?.trim() || null,
        standard_category: values.standard_category.trim(),
        rule_version: values.rule_version.trim(),
      };
      if (editing) await catalogApi.updateMapping(editing.id, payload);
      else await catalogApi.createMapping(payload);
      toast.success("科目映射已保存");
      setOpen(false);
      setEditing(null);
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "科目映射保存失败，请检查输入"));
    } finally {
      setSaving(false);
    }
  }

  async function disable(row: SubjectMapping) {
    const reason = await confirm({
      title: "停用科目映射",
      description: "停用只影响后续解析，不修改历史数据。",
      reasonRequired: true,
      danger: true,
      okText: "确认停用",
    });
    if (reason === null) return;
    setActionError(null);
    try {
      await catalogApi.disableMapping(row.id, reason);
      toast.success("科目映射已停用");
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "科目映射停用失败"));
    }
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="科目映射"
        status={<span>共 <b className="num">{data ? data.meta.total : "—"}</b> 条</span>}
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增映射</Button>
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <Section
        title="映射"
        tools={
          <Segmented
            value={status}
            onChange={(value) => { setPage(1); setStatus(value as MappingStatus | "all"); }}
            options={[
              { value: "active", label: "启用" },
              { value: "inactive", label: "停用" },
              { value: "all", label: "全部" },
            ]}
            aria-label="映射状态"
          />
        }
      >
        <Table<SubjectMapping>
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 900 }}
          locale={{ emptyText: <EmptyState compact title="暂无科目映射" hint="没有映射时，解析器只保留原始科目层级，不做标准类别聚合。" /> }}
          columns={[
            { title: "代码 / 前缀", dataIndex: "subject_code_or_prefix", width: 130, render: (value: string | null) => value ? <span className="mono">{value}</span> : "—" },
            { title: "名称匹配", dataIndex: "raw_name_pattern", render: (value: string | null) => value ?? "—" },
            { title: "标准类别", dataIndex: "standard_category", width: 160 },
            { title: "叶子", dataIndex: "is_leaf", width: 70, render: (value: boolean) => value ? "是" : "否" },
            { title: "计入持仓", dataIndex: "include_in_holdings", width: 90, render: (value: boolean) => value ? "是" : "否" },
            { title: "规则版本", dataIndex: "rule_version", width: 100, render: (value: string) => <span className="mono">{value}</span> },
            {
              title: "状态",
              dataIndex: "status",
              width: 80,
              render: (value: MappingStatus) => (
                <StatusText tone={MAPPING_TONE[value]} muted={value !== "active"}>{MAPPING_STATUS_LABEL[value]}</StatusText>
              ),
            },
            {
              title: "操作",
              width: 130,
              render: (_, row) => (
                <span className="fd-row-actions">
                  <Button type="link" size="small" onClick={() => openEdit(row)}>编辑</Button>
                  {row.status === "active" && <Button type="link" danger size="small" onClick={() => void disable(row)}>停用</Button>}
                </span>
              ),
            },
          ]}
        />
        <p className="fd-form-note">映射决定资产配置聚合与持仓纳入。</p>
      </Section>

      <Modal
        open={open}
        title={editing ? "编辑科目映射" : "新增科目映射"}
        onCancel={() => { setOpen(false); setEditing(null); }}
        onOk={() => void save()}
        okText="保存"
        okButtonProps={{ loading: saving }}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item label="科目代码 / 前缀" name="subject_code_or_prefix" extra="与原始名称匹配至少填写一项。"><Input placeholder="例如 1102" /></Form.Item>
          <Form.Item label="原始名称匹配" name="raw_name_pattern"><Input placeholder="例如 股票投资" /></Form.Item>
          <Form.Item label="标准类别" name="standard_category" rules={[{ required: true, whitespace: true, message: "请输入标准类别" }]}><Input placeholder="例如 equity" /></Form.Item>
          <div className="fd-form-grid">
            <Form.Item label="是否叶子科目" name="is_leaf" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item label="计入持仓" name="include_in_holdings" valuePropName="checked"><Switch /></Form.Item>
          </div>
          <Form.Item label="规则版本" name="rule_version" rules={[{ required: true, whitespace: true, message: "请输入规则版本" }]}><Input style={{ width: 160 }} /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
