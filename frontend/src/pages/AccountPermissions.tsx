import { useState } from "react";
import { Button, Form, Input, Modal, Select, Table } from "antd";
import { EditOutlined, ReloadOutlined } from "@ant-design/icons";
import * as authApi from "@/api/auth";
import type { AccountPermissions as Account } from "@/api/types";
import { Chip, EmptyState, PageError, PageHeader, Section, StatusText, describeError, useAsyncData, useToast } from "@/components";
import { FundAccessFields, normalizeFundAccess, type FundAccessValues } from "@/components/FundAccessFields";
import { useAuth } from "@/app/auth";
import { ROLE_LABEL, USER_STATUS_LABEL, type UserRole, type UserStatus } from "@/utils/constants";
import { canEditFundAccess } from "@/utils/permissions";
import { timeStr } from "@/utils/format";

const PAGE_SIZE = 20;

export default function AccountPermissions() {
  const { session } = useAuth();
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<{ q?: string; role?: UserRole; status?: UserStatus }>({});
  const [accessUser, setAccessUser] = useState<Account | null>(null);
  const [accessForm] = Form.useForm<FundAccessValues & { reason: string }>();
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const { data, loading, error, reload } = useAsyncData(
    () => authApi.listAccountPermissions({ ...filters, page, page_size: PAGE_SIZE }),
    [page, filters],
    "账号权限加载失败",
  );
  const filtered = Boolean(filters.q || filters.role || filters.status);

  async function saveFundAccess() {
    if (!accessUser || !canEditFundAccess(session, accessUser) || saving) return;
    let values: FundAccessValues & { reason: string };
    try { values = await accessForm.validateFields(); } catch { return; }
    setSaving(true);
    setActionError(null);
    try {
      await authApi.changeUserFundAccess(accessUser.id, {
        ...normalizeFundAccess(accessUser.role, values), reason: values.reason.trim(),
      });
      toast.success("可见产品已更新，立即生效");
      setAccessUser(null);
      accessForm.resetFields();
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "可见产品更新失败"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="账号权限一览"
        status={<><span>共 <b className="num">{data?.meta.total ?? "—"}</b> 个账号</span>{filtered && <span>已筛选</span>}</>}
        extra={<Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading}>刷新</Button>}
      >
        <p className="fd-caption">查看各账号的可见产品，点击“修改可见产品”调整该账号的观看范围。</p>
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
      </PageHeader>
      <Section
        title="账号"
        tools={<>
          <Input.Search allowClear placeholder="账号或显示名称" value={query} onChange={(event) => setQuery(event.target.value)} style={{ width: 220 }} onSearch={(value) => { setPage(1); setFilters((current) => ({ ...current, q: value.trim() || undefined })); }} aria-label="搜索账号" />
          <Select allowClear placeholder="角色" style={{ width: 140 }} value={filters.role} onChange={(role) => { setPage(1); setFilters((current) => ({ ...current, role })); }} options={Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))} aria-label="角色" />
          {session?.role === "admin" && <Select allowClear placeholder="状态" style={{ width: 120 }} value={filters.status} onChange={(status) => { setPage(1); setFilters((current) => ({ ...current, status })); }} options={Object.entries(USER_STATUS_LABEL).map(([value, label]) => ({ value, label }))} aria-label="状态" />}
          {filtered && <Button type="link" onClick={() => { setPage(1); setQuery(""); setFilters({}); }}>清除筛选</Button>}
        </>}
      >
        <Table<Account>
          rowKey="id"
          size="small"
          loading={loading}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 920 }}
          locale={{ emptyText: filtered ? <EmptyState compact variant="filtered" /> : <EmptyState compact title="暂无账号" /> }}
          columns={[
            { title: "账号", dataIndex: "username", width: 150, render: (value: string, account) => <span className="fd-inline-actions"><span className="mono fd-table-name">{value}</span>{account.id === session?.id && <Chip>当前</Chip>}</span> },
            { title: "显示名称", dataIndex: "display_name", width: 120, render: (value: string | null) => value || "—" },
            { title: "角色", dataIndex: "role", width: 110, render: (value: UserRole) => ROLE_LABEL[value] },
            { title: "状态", dataIndex: "status", width: 90, render: (value: UserStatus) => <StatusText tone={value === "active" ? "success" : "danger"}>{USER_STATUS_LABEL[value]}</StatusText> },
            { title: "可见产品", width: 260, render: (_, account) => <div className="fd-account-products">{account.fund_access_mode === "all" || account.role === "admin"
              ? <div className="fd-account-products__all">全部产品</div>
              : account.funds.length === 0 ? <div className="fd-table-meta">未分配产品</div>
                : <div><div className="fd-table-meta">指定 {account.funds.length} 只产品</div>{account.funds.map((fund) => <div key={fund.id}>{fund.name}{fund.product_code && <span className="mono fd-table-meta"> · {fund.product_code}</span>}{fund.status === "inactive" && <span className="fd-table-meta">（已停用）</span>}</div>)}</div>}
              {canEditFundAccess(session, account) && <Button className="fd-account-products__edit" icon={<EditOutlined />} onClick={() => {
                setActionError(null);
                accessForm.setFieldsValue({ ...normalizeFundAccess(account.role, account), reason: "" });
                setAccessUser(account);
              }}>修改可见产品</Button>}
              {account.role === "admin" && <div className="fd-table-meta">管理员固定拥有全部产品权限</div>}
              {session?.role === "boss" && account.id === session.id && <div className="fd-table-meta">自己的范围由管理员调整</div>}
            </div> },
            { title: "最后登录", dataIndex: "last_login_at", width: 190, render: (value: string | null) => <span className="num fd-table-meta">{timeStr(value)}</span> },
          ]}
        />
      </Section>
      <Modal open={accessUser !== null} title={`可见产品：${accessUser?.username ?? ""}`} onCancel={() => { if (!saving) setAccessUser(null); }} onOk={() => void saveFundAccess()} okText="保存观看权限" okButtonProps={{ loading: saving }} cancelButtonProps={{ disabled: saving }} closable={!saving} maskClosable={!saving} destroyOnClose>
        <PageError message={actionError} onClose={() => setActionError(null)} />
        <p className="fd-form-note">保存后立即生效，并记录修改人、修改原因及前后产品范围。</p>
        <Form form={accessForm} layout="vertical" disabled={saving}>
          <FundAccessFields role={accessUser?.role ?? "viewer"} source="permissions" />
          <Form.Item label="修改原因" name="reason" rules={[{ required: true, whitespace: true, message: "请填写修改原因" }]}>
            <Input.TextArea rows={2} placeholder="例如：调整负责或可查看的产品" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
