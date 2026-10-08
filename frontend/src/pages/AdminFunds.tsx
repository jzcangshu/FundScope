import { useAuth } from "@/app/auth";
import { isProductScoped } from "@/utils/permissions";
import { useEffect, useRef, useState } from "react";
import { Button, DatePicker, Form, Input, InputNumber, Modal, Skeleton, Table } from "antd";
import dayjs from "dayjs";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import * as catalogApi from "@/api/catalog";
import type { Alias, CatalogFund, ShareClass } from "@/api/types";
import {
  Chip,
  EmptyState,
  Facts,
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
import { FUND_STATUS_LABEL, SHARE_CLASS_STATUS_LABEL, type ShareClassStatus } from "@/utils/constants";
import { dateStr } from "@/utils/format";

const PAGE_SIZE = 20;

/** Share class status reads as text, not as a coloured tag. */
const SHARE_TONE: Record<ShareClassStatus, Tone> = { active: "success", inactive: "neutral" };

/** Backend reports deletion impact by table name; show operators plain Chinese. */
const DELETION_LABELS: Record<string, string> = {
  fund: "产品记录",
  fund_alias: "识别别名",
  share_class: "份额类别",
  valuation_version: "估值版本",
  fund_daily_snapshot: "产品每日快照",
  share_class_daily_snapshot: "份额每日快照",
  account_subject_daily: "科目每日明细",
  position_daily: "持仓明细",
  validation_result: "校验结果",
  field_provenance: "字段溯源",
  fund_metric_daily: "产品每日指标",
  risk_event: "风险事件",
};

interface SelectedFund extends CatalogFund { aliases: Alias[]; shares: ShareClass[] }

export default function AdminFunds() {
  const { session } = useAuth();
  const scoped = isProductScoped(session);
  const toast = useToast();
  const confirm = useConfirm();
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selected, setSelected] = useState<SelectedFund | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [fundModal, setFundModal] = useState<"create" | "edit" | null>(null);
  const [aliasOpen, setAliasOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [fundForm] = Form.useForm();
  const [aliasForm] = Form.useForm();
  const [shareForm] = Form.useForm();
  const selectionToken = useRef(0);

  const list = useAsyncData(() => catalogApi.listFunds({ q: q || undefined, page, page_size: PAGE_SIZE }), [page, q], "产品目录加载失败");
  const funds = list.data?.data ?? [];

  useEffect(() => {
    if (selectedId === null && funds[0]) setSelectedId(funds[0].id);
  }, [funds, selectedId]);

  useEffect(() => {
    if (selectedId === null) return;
    const base = funds.find((fund) => fund.id === selectedId);
    const token = ++selectionToken.current;
    setDetailLoading(true);
    void Promise.all([catalogApi.getFund(selectedId), catalogApi.listAliases(selectedId), catalogApi.listShareClasses(selectedId)])
      .then(([detail, aliases, shares]) => {
        if (token !== selectionToken.current) return;
        setSelected({
          id: selectedId,
          status: detail.data.status,
          product_code: detail.data.product_code,
          standard_name: detail.data.name,
          name: detail.data.name,
          establishment_date: detail.data.establishment_date,
          strategy: detail.data.strategy,
          manager: detail.data.manager,
          notes: detail.data.notes,
          ...(base ?? {}),
          aliases: aliases.data,
          shares: shares.data,
        });
      })
      .catch((caught) => { if (token === selectionToken.current) setActionError(describeError(caught, "产品详情加载失败")); })
      .finally(() => { if (token === selectionToken.current) setDetailLoading(false); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, list.data]);

  async function refreshAll() {
    await list.reload({ silent: true });
    // Re-trigger the detail effect by bumping the selection.
    setSelectedId((current) => current);
  }

  async function withSaving(action: () => Promise<void>, success: string, fallback: string) {
    setSaving(true);
    setActionError(null);
    try {
      await action();
      toast.success(success);
      await refreshAll();
      return true;
    } catch (caught) {
      setActionError(describeError(caught, fallback));
      return false;
    } finally {
      setSaving(false);
    }
  }

  function openCreate() {
    fundForm.resetFields();
    setFundModal("create");
  }

  function openEdit() {
    if (!selected) return;
    fundForm.setFieldsValue({
      standard_name: selected.standard_name ?? selected.name,
      product_code: selected.product_code,
      establishment_date: selected.establishment_date ? dayjs(selected.establishment_date) : undefined,
      notes: selected.notes ?? undefined,
    });
    setFundModal("edit");
  }

  async function saveFund() {
    let values: { standard_name: string; product_code: string; alias?: string; establishment_date?: dayjs.Dayjs; notes?: string };
    try {
      values = await fundForm.validateFields();
    } catch {
      return;
    }
    const payload = {
      standard_name: values.standard_name.trim(),
      product_code: values.product_code.trim(),
      establishment_date: values.establishment_date?.format("YYYY-MM-DD"),
      notes: values.notes?.trim() || null,
    };
    const ok = await withSaving(async () => {
      if (fundModal === "edit" && selected) await catalogApi.updateFund(selected.id, payload);
      else {
        const created = await catalogApi.createFund({ ...payload, aliases: [{ alias: values.alias?.trim() }] });
        setSelectedId(created.data.id);
      }
    }, "产品已保存", "产品保存失败，请检查名称、代码、别名和日期");
    if (ok) setFundModal(null);
  }

  async function toggleFund() {
    if (!selected) return;
    const disabling = selected.status === "active";
    const reason = await confirm({
      title: `${disabling ? "停用" : "启用"}产品`,
      description: disabling ? "停用后看板不再展示该产品，历史估值数据保留。" : "启用后该产品重新出现在看板与列表中。",
      reasonRequired: disabling,
      danger: disabling,
    });
    if (reason === null) return;
    await withSaving(() => (disabling ? catalogApi.disableFund(selected.id, reason) : catalogApi.enableFund(selected.id)).then(() => undefined), "产品状态已更新", "产品状态更新失败");
  }

  /** Deletion is irreversible, so spell out exactly how much history goes with it. */
  function impactLines(impact: catalogApi.FundDeletionImpact) {
    const parts = Object.entries(impact.removed)
      .filter(([, count]) => count > 0)
      .map(([table, count]) => `${DELETION_LABELS[table] ?? table} ${count.toLocaleString()}`);
    return (
      <>
        <span>将永久删除：{parts.length ? parts.join("、") : "无关联数据"}。</span>
        <br />
        <span>合计 {impact.total_removed.toLocaleString()} 行，删除后无法恢复。</span>
        <br />
        <span>
          公司级指标 {impact.preserved_company_metrics.toLocaleString()} 行完整保留（
          {impact.detached_analysis_runs.toLocaleString()} 条分析运行仅解除触发关联，不删除）。
        </span>
      </>
    );
  }

  async function removeFund() {
    if (!selected || selected.status === "active") return;
    const target = selected;
    setActionError(null);
    let impact: catalogApi.FundDeletionImpact | null = null;
    try {
      impact = (await catalogApi.fundDeletionImpact(target.id)).data;
    } catch {
      impact = null; // Preview is best-effort; never block the operator on it.
    }
    const reason = await confirm({
      title: `删除产品「${target.standard_name ?? target.name}」`,
      description: impact
        ? impactLines(impact)
        : "将永久删除该产品及其全部估值数据，删除后无法恢复。",
      reasonRequired: true,
      reasonPlaceholder: "请填写删除原因，例如：产品已清算并完成归档",
      danger: true,
      okText: "永久删除",
    });
    if (reason === null) return;
    await withSaving(async () => {
      await catalogApi.deleteFund(target.id, reason);
      setSelectedId(null);
      setSelected(null);
      if (funds.length === 1 && page > 1) setPage(1);
    }, "产品已删除", "产品删除失败");
  }

  async function saveAlias() {
    if (!selected) return;
    let values: { alias: string; match_priority?: number };
    try {
      values = await aliasForm.validateFields();
    } catch {
      return;
    }
    const ok = await withSaving(() => catalogApi.createAlias(selected.id, { alias: values.alias.trim(), match_priority: values.match_priority ?? 0 }).then(() => undefined), "识别别名已新增", "别名保存失败");
    if (ok) { setAliasOpen(false); aliasForm.resetFields(); }
  }

  async function deleteAlias(alias: Alias) {
    if (!selected) return;
    const reason = await confirm({ title: "删除识别别名", description: `删除“${alias.alias}”后，后续估值表将无法通过该名称识别到本产品。`, reasonRequired: true, danger: true, okText: "确认删除" });
    if (reason === null) return;
    await withSaving(() => catalogApi.deleteAlias(selected.id, alias.id, reason).then(() => undefined), "识别别名已删除", "别名删除失败");
  }

  async function saveShare() {
    if (!selected) return;
    let values: { share_code: string; share_name: string; enabled_from?: dayjs.Dayjs };
    try {
      values = await shareForm.validateFields();
    } catch {
      return;
    }
    const ok = await withSaving(() => catalogApi.createShareClass(selected.id, { share_code: values.share_code.trim(), share_name: values.share_name.trim(), enabled_from: values.enabled_from?.format("YYYY-MM-DD") }).then(() => undefined), "份额类别已新增", "份额类别保存失败");
    if (ok) { setShareOpen(false); shareForm.resetFields(); }
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="产品管理"
        status={<span>共 <b className="num">{list.data?.meta.total ?? "—"}</b> 只产品</span>}
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void refreshAll()} loading={list.loading}>刷新</Button>
            {!scoped && <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增产品</Button>}
          </>
        }
      >
        <PageError message={list.error} onRetry={() => void list.reload()} retrying={list.loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <section className="fd-section" aria-label="产品目录">
        <div className="fd-split fd-split--aside">
          <div>
            <div className="fd-section__head">
              <div className="fd-section__title">产品</div>
            </div>
            <div className="fd-stack fd-stack--tight">
              <Input.Search allowClear placeholder="搜索名称或代码" style={{ width: "100%" }} onSearch={(value) => { setPage(1); setQ(value.trim()); }} aria-label="搜索产品" />
              <Table<CatalogFund>
                className="fd-master-list"
                rowKey="id"
                size="small"
                showHeader={false}
                loading={list.loading && !list.data}
                dataSource={funds}
                pagination={{ current: page, pageSize: PAGE_SIZE, total: list.data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true, simple: true }}
                rowClassName={(row) => (row.id === selectedId ? "is-selected" : "")}
                onRow={(row) => ({ onClick: () => setSelectedId(row.id) })}
                locale={{ emptyText: <EmptyState compact title="暂无产品" hint={scoped ? "暂无已授权产品，请联系管理员分配产品范围。" : "点击“新增产品”创建第一只产品。"} /> }}
                columns={[
                  {
                    render: (_, fund) => (
                      <div className="fd-master-list__item">
                        <div className="fd-strong">{fund.standard_name ?? fund.name ?? "—"}</div>
                        <div className="fd-caption fd-inline-actions">
                          <span className="mono">{fund.product_code ?? "无代码"}</span>
                          {fund.status === "inactive" && <Chip>{FUND_STATUS_LABEL.inactive}</Chip>}
                        </div>
                      </div>
                    ),
                  },
                ]}
              />
            </div>
          </div>

          <div className="fd-catalog-detail">
            {!selected && !detailLoading && <EmptyState title="未选择产品" hint="在左侧选择一只产品查看并维护其别名与份额类别。" />}
            {detailLoading && !selected && <Skeleton active paragraph={{ rows: 4 }} />}
            {selected && (
              <>
                <div className="fd-section__head">
                  <div className="fd-section__title fd-catalog-detail__title">
                    {selected.standard_name ?? selected.name}
                    <StatusText tone={selected.status === "active" ? "success" : "neutral"} muted={selected.status !== "active"}>{FUND_STATUS_LABEL[selected.status]}</StatusText>
                  </div>
                  <div className="fd-section__tools">
                    <Button size="small" onClick={openEdit}>编辑</Button>
                    <Button size="small" danger={selected.status === "active"} onClick={() => void toggleFund()}>{selected.status === "active" ? "停用" : "启用"}</Button>
                    {session?.role === "admin" && <Button
                      size="small"
                      danger
                      disabled={selected.status === "active"}
                      title={selected.status === "active" ? "请先停用产品，再执行删除" : undefined}
                      onClick={() => void removeFund()}
                    >
                      删除
                    </Button>}
                  </div>
                </div>
                <Facts
                  items={[
                    { label: "产品代码", value: <span className="mono">{selected.product_code ?? "—"}</span> },
                    { label: "成立日期", value: dateStr(selected.establishment_date) },
                    { label: "策略", value: selected.strategy ?? "—" },
                    { label: "管理人", value: selected.manager ?? "—" },
                    { label: "备注", value: selected.notes ?? "—", wide: true },
                  ]}
                />
                <p className="fd-form-note">配置只影响后续解析，历史估值数据不会被改写。</p>

                <Section
                  title="识别别名"
                  tools={<Button size="small" icon={<PlusOutlined />} onClick={() => { aliasForm.resetFields(); setAliasOpen(true); }}>新增别名</Button>}
                >
                  <p className="fd-section__note">解析估值表时按别名识别产品；优先级数值越大越先匹配。</p>
                  <Table<Alias>
                    rowKey="id"
                    size="small"
                    pagination={false}
                    scroll={{ x: 420 }}
                    dataSource={selected.aliases}
                    locale={{ emptyText: <EmptyState compact title="暂无别名" hint="没有别名时，估值表无法自动识别到本产品。" /> }}
                    columns={[
                      { title: "别名", dataIndex: "alias", render: (value: string) => <span className="fd-table-name">{value}</span> },
                      { title: "优先级", dataIndex: "match_priority", width: 90, align: "right" },
                      { title: "来源位置", dataIndex: "source_location", width: 160, render: (value: string | null) => value ?? "—" },
                      {
                        title: "",
                        width: 80,
                        render: (_, alias) => (
                          <span className="fd-row-actions">
                            <Button danger type="link" size="small" onClick={() => void deleteAlias(alias)}>删除</Button>
                          </span>
                        ),
                      },
                    ]}
                  />
                </Section>

                <Section
                  title="份额类别"
                  tools={<Button size="small" icon={<PlusOutlined />} onClick={() => { shareForm.resetFields(); setShareOpen(true); }}>新增份额</Button>}
                >
                  <Table<ShareClass>
                    rowKey="id"
                    size="small"
                    pagination={false}
                    scroll={{ x: 480 }}
                    dataSource={selected.shares}
                    locale={{ emptyText: <EmptyState compact title="暂无份额类别" hint="单一份额的产品可以不配置份额类别。" /> }}
                    columns={[
                      { title: "代码", dataIndex: "share_code", width: 140, render: (value: string) => <span className="mono">{value}</span> },
                      { title: "名称", dataIndex: "share_name", render: (value: string) => <span className="fd-table-name">{value}</span> },
                      { title: "启用日期", dataIndex: "enabled_from", width: 120, render: (value: string | null) => <span className="num fd-table-meta">{dateStr(value)}</span> },
                      {
                        title: "状态",
                        dataIndex: "status",
                        width: 90,
                        render: (value: ShareClassStatus) => (
                          <StatusText tone={SHARE_TONE[value]} muted={value === "inactive"}>{SHARE_CLASS_STATUS_LABEL[value]}</StatusText>
                        ),
                      },
                    ]}
                  />
                </Section>
              </>
            )}
          </div>
        </div>
      </section>

      <Modal open={fundModal !== null} title={fundModal === "edit" ? "编辑产品" : "新增产品"} onCancel={() => setFundModal(null)} onOk={() => void saveFund()} okText="保存" okButtonProps={{ loading: saving }} destroyOnClose>
        <Form form={fundForm} layout="vertical">
          <Form.Item label="标准名称" name="standard_name" rules={[{ required: true, whitespace: true, message: "请输入标准名称" }]}><Input /></Form.Item>
          <Form.Item label="产品代码" name="product_code" rules={[{ required: true, whitespace: true, message: "请输入产品代码" }]}><Input /></Form.Item>
          {fundModal === "create" && (
            <Form.Item
              label="初始识别别名"
              name="alias"
              extra="估值表中出现的产品名称，须不同于标准名称。"
              rules={[
                { required: true, whitespace: true, message: "请填写至少一个识别别名" },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    return value && value.trim().toLowerCase() !== String(getFieldValue("standard_name") ?? "").trim().toLowerCase()
                      ? Promise.resolve()
                      : Promise.reject(new Error("别名必须不同于标准名称"));
                  },
                }),
              ]}
            >
              <Input />
            </Form.Item>
          )}
          <Form.Item label="成立日期" name="establishment_date" rules={[{ required: true, message: "请选择成立日期" }]}><DatePicker style={{ width: "100%" }} /></Form.Item>
          <Form.Item label="备注" name="notes"><Input.TextArea rows={3} maxLength={500} showCount /></Form.Item>
        </Form>
      </Modal>

      <Modal open={aliasOpen} title="新增识别别名" onCancel={() => setAliasOpen(false)} onOk={() => void saveAlias()} okText="保存" okButtonProps={{ loading: saving }} destroyOnClose>
        <Form form={aliasForm} layout="vertical" initialValues={{ match_priority: 0 }}>
          <Form.Item label="别名" name="alias" rules={[{ required: true, whitespace: true, message: "请输入别名" }]}><Input /></Form.Item>
          <Form.Item label="匹配优先级" name="match_priority" extra="数值越大越先匹配，默认 0。"><InputNumber min={0} max={1000} style={{ width: 160 }} /></Form.Item>
        </Form>
      </Modal>

      <Modal open={shareOpen} title="新增份额类别" onCancel={() => setShareOpen(false)} onOk={() => void saveShare()} okText="保存" okButtonProps={{ loading: saving }} destroyOnClose>
        <Form form={shareForm} layout="vertical">
          <Form.Item label="份额代码" name="share_code" rules={[{ required: true, whitespace: true, message: "请输入份额代码" }]}><Input /></Form.Item>
          <Form.Item label="份额名称" name="share_name" rules={[{ required: true, whitespace: true, message: "请输入份额名称" }]}><Input /></Form.Item>
          <Form.Item label="启用日期" name="enabled_from"><DatePicker style={{ width: "100%" }} /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
