import { useState } from "react";
import { Button, Dropdown, Form, Input, Modal, Select, Table } from "antd";
import { DownOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import * as authApi from "@/api/auth";
import type { AdminUser } from "@/api/types";
import {
  Chip,
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
import { FundAccessFields, normalizeFundAccess, type FundAccessValues } from "@/components/FundAccessFields";
import { useAuth } from "@/app/auth";
import { ROLE_LABEL, USER_STATUS_LABEL, type UserRole, type UserStatus } from "@/utils/constants";
import { timeStr } from "@/utils/format";

const PAGE_SIZE = 20;

type CreateUserValues = FundAccessValues & { username: string; display_name?: string; role: UserRole; password: string };

/** Account status reads as dot + text; no tag backgrounds. */
const STATUS_TONE: Record<UserStatus, Tone> = { active: "success", disabled: "danger" };

export default function AdminUsers() {
  const toast = useToast();
  const confirm = useConfirm();
  const { session } = useAuth();
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<{ q?: string; role?: UserRole; status?: UserStatus }>({});
  const [createOpen, setCreateOpen] = useState(false);
  const [accessUser, setAccessUser] = useState<AdminUser | null>(null);
  const [accessForm] = Form.useForm<FundAccessValues & { reason: string }>();
  const [resetUser, setResetUser] = useState<AdminUser | null>(null);
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [form] = Form.useForm<CreateUserValues>();
  const createRole = Form.useWatch("role", form) ?? "viewer";
  const [resetForm] = Form.useForm<{ password: string }>();

  const state = useAsyncData(() => authApi.listUsers({ ...filters, page, page_size: PAGE_SIZE }), [page, filters], "账号列表加载失败");
  const { data, loading, error, reload } = state;
  const filtered = Boolean(filters.q || filters.role || filters.status);

  async function run(action: () => Promise<void>, success: string, fallback: string) {
    setSaving(true);
    setActionError(null);
    try {
      await action();
      toast.success(success);
      await reload({ silent: true });
      return true;
    } catch (caught) {
      setActionError(describeError(caught, fallback));
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function create() {
    let values: CreateUserValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const ok = await run(() => authApi.createUser({ ...values, ...normalizeFundAccess(values.role, values), username: values.username.trim(), display_name: values.display_name?.trim() || undefined }).then(() => undefined), "账号已创建", "账号创建失败，请检查输入或账号是否重复");
    if (ok) { setCreateOpen(false); form.resetFields(); }
  }

  async function saveFundAccess() {
    if (!accessUser) return;
    let values: FundAccessValues & { reason: string };
    try { values = await accessForm.validateFields(); } catch { return; }
    const ok = await run(() => authApi.changeUserFundAccess(accessUser.id, {
      ...normalizeFundAccess(accessUser.role, values), reason: values.reason.trim(),
    }).then(() => undefined), "产品范围已更新，立即生效", "产品范围更新失败");
    if (ok) { setAccessUser(null); accessForm.resetFields(); }
  }

  async function changeRole(user: AdminUser, role: UserRole) {
    if (role === user.role) return;
    const reason = await confirm({ title: `修改 ${user.username} 的角色`, description: `将角色由“${ROLE_LABEL[user.role]}”改为“${ROLE_LABEL[role]}”。`, reasonRequired: true, okText: "确认修改" });
    if (reason === null) return;
    await run(() => authApi.changeUserRole(user.id, role, reason).then(() => undefined), "角色已修改", "角色修改失败");
  }

  async function toggle(user: AdminUser) {
    const disabling = user.status === "active";
    const reason = await confirm({
      title: `${disabling ? "禁用" : "启用"}账号 ${user.username}`,
      description: disabling ? "禁用后该账号无法登录，现有会话将失效。" : undefined,
      reasonRequired: disabling,
      danger: disabling,
      okText: disabling ? "确认禁用" : "确认启用",
    });
    if (reason === null) return;
    await run(() => (disabling ? authApi.disableUser(user.id, reason) : authApi.enableUser(user.id)).then(() => undefined), `账号已${disabling ? "禁用" : "启用"}`, "账号状态更新失败");
  }

  async function resetPassword() {
    if (!resetUser) return;
    let values: { password: string };
    try {
      values = await resetForm.validateFields();
    } catch {
      return;
    }
    const reason = await confirm({ title: `重置 ${resetUser.username} 的密码`, description: "重置后该账号的现有会话会被撤销。", reasonRequired: true, danger: true, okText: "确认重置" });
    if (reason === null) return;
    const ok = await run(() => authApi.resetUserPassword(resetUser.id, values.password, reason).then(() => undefined), "密码已重置，原有会话已撤销", "密码重置失败");
    if (ok) { setResetUser(null); resetForm.resetFields(); }
  }

  async function revoke(user: AdminUser) {
    const reason = await confirm({ title: `撤销 ${user.username} 的全部会话`, description: "该账号所有已登录设备都需要重新登录。", reasonRequired: true, danger: true, okText: "确认撤销" });
    if (reason === null) return;
    await run(() => authApi.revokeUserSessions(user.id, reason).then(() => undefined), "会话已撤销", "会话撤销失败");
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="账号管理"
        status={
          <>
            <span>共 <b className="num">{data?.meta.total ?? "—"}</b> 个账号</span>
            {filtered && <span>已筛选</span>}
          </>
        }
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setActionError(null); setCreateOpen(true); }}>新增账号</Button>
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <Section
        title="账号"
        tools={
          <>
            <Input.Search allowClear placeholder="账号或显示名称" style={{ width: 220 }} onSearch={(value) => { setPage(1); setFilters((current) => ({ ...current, q: value.trim() || undefined })); }} aria-label="搜索账号" />
            <Select allowClear placeholder="角色" style={{ width: 140 }} value={filters.role} onChange={(role) => { setPage(1); setFilters((current) => ({ ...current, role })); }} options={Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))} aria-label="角色" />
            <Select allowClear placeholder="状态" style={{ width: 120 }} value={filters.status} onChange={(status) => { setPage(1); setFilters((current) => ({ ...current, status })); }} options={Object.entries(USER_STATUS_LABEL).map(([value, label]) => ({ value, label }))} aria-label="状态" />
            {filtered && <Button type="link" onClick={() => { setPage(1); setFilters({}); }}>清除筛选</Button>}
          </>
        }
      >
        <Table<AdminUser>
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 1060 }}
          locale={{ emptyText: filtered ? <EmptyState compact variant="filtered" /> : <EmptyState compact title="暂无账号" /> }}
          columns={[
            { title: "账号", dataIndex: "username", width: 160, render: (value: string, user) => <span className="fd-inline-actions"><span className="mono fd-table-name">{value}</span>{user.id === session?.id && <Chip>当前</Chip>}</span> },
            { title: "显示名称", dataIndex: "display_name", ellipsis: true },
            {
              title: "角色",
              dataIndex: "role",
              width: 150,
              render: (value: UserRole, user) => (
                <Dropdown
                  trigger={["click"]}
                  disabled={user.id === session?.id}
                  menu={{
                    selectedKeys: [value],
                    items: Object.entries(ROLE_LABEL).map(([key, label]) => ({ key, label })),
                    onClick: ({ key }) => void changeRole(user, key as UserRole),
                  }}
                >
                  <Button className="fd-table-role-button" size="small" type="text" disabled={user.id === session?.id}>{ROLE_LABEL[value]} <DownOutlined style={{ fontSize: 10 }} /></Button>
                </Dropdown>
              ),
            },
            {
              title: "可见产品", width: 180,
              render: (_, user) => user.role === "admin" ? <span className="fd-table-meta">全部产品 · 无需配置</span> : (
                <Button type="link" size="small" onClick={() => {
                  setActionError(null);
                  accessForm.setFieldsValue({ ...normalizeFundAccess(user.role, user), reason: "" });
                  setAccessUser(user);
                }}>{user.fund_access_mode === "selected" ? `指定 ${user.fund_ids.length} 只产品` : "全部产品"} · 修改</Button>
              ),
            },
            { title: "状态", dataIndex: "status", width: 90, render: (value: UserStatus) => <StatusText tone={STATUS_TONE[value]}>{USER_STATUS_LABEL[value]}</StatusText> },
            { title: "最后登录", dataIndex: "last_login_at", width: 150, render: (value: string | null) => <span className="num fd-table-meta">{timeStr(value)}</span> },
            {
              title: "操作",
              width: 220,
              render: (_, user) => (
                <span className="fd-row-actions">
                  <Button type="link" size="small" onClick={() => { resetForm.resetFields(); setResetUser(user); }}>重置密码</Button>
                  <Button type="link" size="small" danger={user.status === "active"} disabled={user.id === session?.id} onClick={() => void toggle(user)}>{user.status === "active" ? "禁用" : "启用"}</Button>
                  <Button type="link" size="small" danger onClick={() => void revoke(user)}>撤销会话</Button>
                </span>
              ),
            },
          ]}
        />
        <p className="fd-form-note">所有变更都会要求原因并写入审计日志。</p>
      </Section>

      <Modal open={createOpen} title="新增账号" onCancel={() => setCreateOpen(false)} onOk={() => void create()} okText="创建" okButtonProps={{ loading: saving }} destroyOnClose>
        <PageError message={actionError} onClose={() => setActionError(null)} />
        <Form form={form} layout="vertical" initialValues={{ role: "viewer", fund_access_mode: "all", fund_ids: [] }}>
          <Form.Item label="账号" name="username" rules={[{ required: true, whitespace: true, message: "请输入账号" }]}><Input autoComplete="off" /></Form.Item>
          <Form.Item label="显示名称" name="display_name"><Input /></Form.Item>
          <Form.Item label="角色" name="role" rules={[{ required: true, message: "请选择角色" }]}>
            <Select options={Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))} />
          </Form.Item>
          <FundAccessFields role={createRole} />
          <Form.Item label="初始密码" name="password" rules={[{ required: true, message: "请输入初始密码" }, { min: 8, message: "至少 8 位" }]} extra="请通过安全渠道告知用户，并建议首次登录后修改。">
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal open={accessUser !== null} title={`可见产品：${accessUser?.username ?? ""}`} onCancel={() => setAccessUser(null)} onOk={() => void saveFundAccess()} okText="保存产品范围" okButtonProps={{ loading: saving }} destroyOnClose>
        <PageError message={actionError} onClose={() => setActionError(null)} />
        <p className="fd-form-note">保存后立即生效，用户页面会自动同步新的产品范围。</p>
        <Form form={accessForm} layout="vertical">
          <FundAccessFields role={accessUser?.role ?? "viewer"} />
          <Form.Item label="修改原因" name="reason" rules={[{ required: true, whitespace: true, message: "请填写修改原因" }]}>
            <Input.TextArea rows={2} placeholder="例如：调整负责的产品" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal open={resetUser !== null} title={`重置密码：${resetUser?.username ?? ""}`} onCancel={() => setResetUser(null)} onOk={() => void resetPassword()} okText="下一步" okButtonProps={{ loading: saving }} destroyOnClose>
        <Form form={resetForm} layout="vertical">
          <Form.Item label="新密码" name="password" rules={[{ required: true, message: "请输入新密码" }, { min: 8, message: "至少 8 位" }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
