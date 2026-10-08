import { useEffect, useState } from "react";
import { Button, Checkbox, Input, InputNumber, Radio, Skeleton, Table, TimePicker } from "antd";
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import * as mailApi from "@/api/mail";
import { apiErrorMessage } from "@/api/client";
import { downloadBlob } from "@/api/downloads";
import type { MailMessage, MailSettings, MailSyncResult, MailSyncSchedule, MailSyncScheduleTime } from "@/api/types";
import {
  Disclosure,
  EmptyState,
  Num,
  PageError,
  PageHeader,
  Section,
  StatusText,
  describeError,
  useAsyncData,
  useInterval,
  useModal,
  useToast,
  type Tone,
} from "@/components";
import { useAuth } from "@/app/auth";
import { MAIL_CREDENTIAL_SOURCE_LABEL, MAIL_SYNC_RUN_STATUS_LABEL, type MailSyncRunStatus } from "@/utils/constants";
import { formatBytes, timeStr } from "@/utils/format";

const DAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const RUN_TONE: Record<MailSyncRunStatus, Tone> = { queued: "neutral", running: "info", succeeded: "success", failed: "danger", cancelled: "neutral" };
const CONNECTION_ERRORS: Record<string, string> = {
  mail_not_configured: "邮箱服务器、账号或授权码未配置",
  mail_credential_unavailable: "授权码文件不可读，请检查文件权限和挂载",
  mail_connection_timeout: "连接邮箱服务器超时，请检查容器外网出口",
  mail_dns_failed: "无法解析邮箱服务器地址，请检查 DNS 设置",
  mail_tls_failed: "邮箱服务器安全连接失败，请检查 SSL 设置",
  mail_connection_failed: "邮箱服务器连接失败，请检查网络和服务器配置",
};

const isActiveRun = (run: MailSyncResult) => run.status === "queued" || run.status === "running";

/** A queued/running run older than this is stuck (e.g. IMAP hang): unlock the page instead of disabling every control forever. */
const STUCK_RUN_MINUTES = 15;
/** While a run is stuck we only watch for it to finish, so the poll can slow down. */
const STUCK_POLL_MS = 60_000;

/** Keep mail connection details out of the normal page view. */
function maskHost(host: string, port: number) {
  const value = host.trim();
  if (!value) return "—";
  const maskedPort = String(port || "").replace(/\d/g, "•") || "•••";
  if (value.length <= 4) return `${value.slice(0, 1)}•••:${maskedPort}`;
  const suffix = value.includes(".") ? value.slice(value.indexOf(".")) : "";
  const prefix = value.slice(0, 1);
  return `${prefix}•••${suffix}:${maskedPort}`;
}

function maskEmail(value: string) {
  const email = value.trim();
  if (!email) return "—";
  const at = email.indexOf("@");
  if (at <= 0) return `${email.slice(0, 1)}•••`;
  return `${email.slice(0, 1)}•••${email.slice(at)}`;
}

const DOWNLOAD_ERRORS: Record<string, string> = {
  no_messages_selected: "请先选择邮件",
  too_many_messages: "一次最多选择 500 封邮件",
  too_many_files: "所选附件总数超过上限（200 个）",
  archive_too_large: "所选附件总大小超过上限（200MB）",
  no_attachments: "所选邮件没有可下载的附件",
};

interface MailData { settings: MailSettings; runs: MailSyncResult[] }

export default function Mail() {
  const { session } = useAuth();
  const isAdmin = session?.role === "admin";
  const toast = useToast();
  const modal = useModal();
  const [username, setUsername] = useState("");
  const [editingUsername, setEditingUsername] = useState(false);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [scheduleMode, setScheduleMode] = useState<"interval" | "scheduled">("interval");
  const [intervalMinutes, setIntervalMinutes] = useState(30);
  const [scheduleTimes, setScheduleTimes] = useState<MailSyncScheduleTime[]>([]);
  const [scheduleDirty, setScheduleDirty] = useState(false);

  const state = useAsyncData<MailData>(
    async () => {
      const [settings, runs] = await Promise.all([mailApi.getSettings(), mailApi.listSyncRuns()]);
      return { settings: settings.data, runs: runs.data };
    },
    [],
    "邮件设置加载失败",
  );
  const { data, loading, error, reload, setData } = state;
  const settings = data?.settings ?? null;
  const runs = data?.runs ?? [];
  const activeRun = runs.find(isActiveRun) ?? null;
  const syncStuck =
    activeRun !== null && Boolean(activeRun.created_at) && dayjs().diff(dayjs(activeRun.created_at), "minute") >= STUCK_RUN_MINUTES;
  const syncRunning = activeRun !== null && !syncStuck;
  const showSkeleton = loading && !data;
  const autoSync = settings?.auto_sync_enabled ?? false;

  const [selectedMessageIds, setSelectedMessageIds] = useState<number[]>([]);
  const [downloading, setDownloading] = useState(false);

  const messagesState = useAsyncData<MailMessage[]>(
    async () => {
      const result = await mailApi.listMessages();
      return result.data;
    },
    [],
    "邮件列表加载失败",
  );
  const messages = messagesState.data ?? [];

  // Sync the editable form state from the server once per load (not on silent polls).
  useEffect(() => {
    if (!settings) return;
    setUsername(settings.username);
    setEditingUsername(false);
    const schedule = settings.schedule;
    if (schedule) {
      setScheduleMode(schedule.mode);
      if (schedule.mode === "interval") setIntervalMinutes(schedule.interval_minutes ?? 30);
      if (schedule.mode === "scheduled") setScheduleTimes(schedule.times ?? []);
    }
    setScheduleDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings?.username, settings?.schedule?.mode, loading]);

  useInterval(() => {
    void mailApi.listSyncRuns().then((result) => setData((current) => (current ? { ...current, runs: result.data } : current))).catch(() => undefined);
  }, syncStuck ? STUCK_POLL_MS : 2_000, activeRun !== null);

  async function act(key: string, action: () => Promise<void>, success: string | null, fallback: string) {
    setBusy(key);
    setActionError(null);
    try {
      await action();
      if (success) toast.success(success);
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, fallback));
    } finally {
      setBusy(null);
    }
  }

  const saveUsername = () => username.trim() && act("username", async () => {
    await mailApi.updateSettings(username.trim());
    setEditingUsername(false);
  }, "邮箱账号已保存", "邮箱账号保存失败，请检查账号格式");
  const saveCode = () => code.trim() && act("code", async () => { await mailApi.updateCredential(code.trim()); setCode(""); }, "授权码已保存（不会回显）", "授权码保存失败");
  const toggleAuto = () => act("toggle", () => (settings?.auto_sync_enabled ? mailApi.pause() : mailApi.resume()).then(() => undefined), settings?.auto_sync_enabled ? "自动同步已暂停" : "自动同步已恢复", "自动同步状态更新失败");
  const cancelSync = () => activeRun && act("cancel", () => mailApi.cancelSync(activeRun.run_id).then(() => undefined), "已发送中断请求，系统会在当前邮件处理完成后停止", "中断请求发送失败");

  async function testConnection() {
    setBusy("test");
    setActionError(null);
    try {
      await mailApi.testConnection();
      toast.success("邮箱连接测试成功");
    } catch (cause) {
      const detail = apiErrorMessage(cause, "");
      setActionError(CONNECTION_ERRORS[detail] ?? "邮件连接测试失败，请检查服务器配置和网络");
    } finally {
      setBusy(null);
    }
  }

  async function syncNow() {
    await act("sync", async () => {
      await mailApi.syncNow();
      modal.info({
        title: "邮件同步已开始",
        content: "同步在后台持续处理，可以离开当前页面，稍后在“同步记录”中查看结果。",
        okText: "知道了",
      });
    }, null, "邮件同步启动失败，请稍后重试");
  }

  async function downloadSelected() {
    if (selectedMessageIds.length === 0) return;
    setDownloading(true);
    try {
      const started = await downloadBlob("/mail/messages/archive", "邮件附件.zip", {
        method: "POST",
        body: { message_ids: selectedMessageIds },
      });
      if (started) toast.success("附件已开始下载");
    } catch (cause) {
      const detail = apiErrorMessage(cause, "");
      toast.error(DOWNLOAD_ERRORS[detail] ?? describeError(cause, "附件下载失败"));
    } finally {
      setDownloading(false);
    }
  }

  async function saveSchedule() {
    const schedule: MailSyncSchedule = scheduleMode === "interval"
      ? { mode: "interval", interval_minutes: intervalMinutes }
      : { mode: "scheduled", times: scheduleTimes };
    await act("schedule", async () => { await mailApi.updateSchedule(schedule); setScheduleDirty(false); }, "定时同步设置已保存", "定时同步设置保存失败");
  }

  function updateTimes(updater: (times: MailSyncScheduleTime[]) => MailSyncScheduleTime[]) {
    setScheduleTimes(updater);
    setScheduleDirty(true);
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="邮件接入"
        status={
          <>
            <StatusText tone={settings?.configured ? "success" : "neutral"} muted={!settings?.configured}>{settings?.configured ? "已配置" : "未配置"}</StatusText>
            <StatusText tone={autoSync ? "success" : "neutral"} muted={!autoSync}>自动同步{autoSync ? "运行中" : "已暂停"}</StatusText>
          </>
        }
        extra={<Button type="text" icon={<ReloadOutlined />} onClick={() => { void reload(); void messagesState.reload({ silent: true }); }} loading={loading}>刷新</Button>}
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      {isAdmin && (
        <Section title="邮箱设置">
          <Disclosure summary="管理账号与授权码">
          {showSkeleton ? (
            <Skeleton active paragraph={{ rows: 2 }} />
          ) : (
            <>
              <p className="fd-section__note">服务器和邮箱账号默认脱敏显示；授权码保存后不会回显。当前来源：{settings ? MAIL_CREDENTIAL_SOURCE_LABEL[settings.credential_source] ?? settings.credential_source : "—"}。</p>
              <div className="fd-mail-settings">
                <div className="fd-mail-setting fd-mail-setting--readonly">
                  <span className="fd-mail-setting__label">邮箱服务器</span>
                  <span className="fd-mail-setting__masked" aria-label="邮箱服务器已脱敏">{settings ? maskHost(settings.host, settings.port) : "—"}</span>
                </div>
                <div className="fd-mail-setting">
                  <label className="fd-mail-setting__label" htmlFor="mail-username">邮箱账号</label>
                  {editingUsername ? (
                    <div className="fd-mail-setting__input">
                      <Input id="mail-username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="输入新的邮箱账号" autoComplete="username" disabled={syncRunning} aria-label="邮箱账号" autoFocus />
                    </div>
                  ) : (
                    <span className="fd-mail-setting__masked" aria-label="邮箱账号已脱敏">{maskEmail(settings?.username ?? username)}</span>
                  )}
                  <div className="fd-inline-actions fd-mail-setting__actions">
                    {editingUsername ? (
                      <>
                        <Button type="primary" loading={busy === "username"} disabled={syncRunning || !username.trim() || username.trim() === settings?.username} onClick={() => void saveUsername()}>保存账号</Button>
                        <Button disabled={busy !== null || syncRunning} onClick={() => { setUsername(settings?.username ?? ""); setEditingUsername(false); }}>取消</Button>
                      </>
                    ) : (
                      <Button disabled={syncRunning || !settings?.username} onClick={() => { setUsername(""); setEditingUsername(true); }}>编辑账号</Button>
                    )}
                  </div>
                </div>
                <div className="fd-mail-setting">
                  <label className="fd-mail-setting__label" htmlFor="mail-credential">邮箱授权码</label>
                  <div className="fd-mail-setting__input">
                    <Input.Password id="mail-credential" value={code} onChange={(event) => setCode(event.target.value)} placeholder="邮箱授权码" autoComplete="new-password" disabled={syncRunning} aria-label="邮箱授权码" />
                  </div>
                  <div className="fd-inline-actions fd-mail-setting__actions">
                    <Button loading={busy === "code"} disabled={syncRunning || !code.trim()} onClick={() => void saveCode()}>保存授权码</Button>
                    <Button loading={busy === "test"} disabled={busy !== null || syncRunning} onClick={() => void testConnection()}>测试连接</Button>
                  </div>
                </div>
              </div>
            </>
          )}
          </Disclosure>
        </Section>
      )}

      <Section
        title="同步控制"
        tools={
          <>
            {activeRun && (syncRunning || syncStuck)
              ? <Button danger type="primary" size="small" loading={busy === "cancel"} onClick={() => void cancelSync()}>中断同步</Button>
              : <Button type="primary" size="small" loading={busy === "sync"} disabled={busy !== null || showSkeleton} onClick={() => void syncNow()}>立即同步</Button>}
            {isAdmin && <Button size="small" loading={busy === "toggle"} disabled={busy !== null || syncRunning || showSkeleton} onClick={() => void toggleAuto()}>{settings?.auto_sync_enabled ? "暂停自动同步" : "恢复自动同步"}</Button>}
          </>
        }
      >
        {showSkeleton ? (
          <Skeleton active title={false} paragraph={{ rows: 1 }} />
        ) : syncRunning ? (
          <span className="fd-sync-running"><span className="fd-sync-running__dot" />后台同步进行中（{MAIL_SYNC_RUN_STATUS_LABEL[activeRun.status as MailSyncRunStatus] ?? activeRun.status}），可安全离开此页面</span>
        ) : syncStuck ? (
          <p className="fd-note">后台同步已超过 {STUCK_RUN_MINUTES} 分钟未结束，已解除本页操作锁定；如需处理请先「中断同步」，持续无进展请联系管理员检查后端任务。</p>
        ) : (
          <p className="fd-note">同步会读取收件箱并处理可识别的估值表附件；产生的批次会出现在导入中心。</p>
        )}
      </Section>

      {isAdmin && (
        <Section
          title="定时同步"
          tools={<Button type="primary" size="small" loading={busy === "schedule"} disabled={syncRunning || !scheduleDirty} onClick={() => void saveSchedule()}>保存定时设置</Button>}
        >
          {showSkeleton ? (
            <Skeleton active paragraph={{ rows: 3 }} />
          ) : (
            <div className="fd-stack fd-stack--tight">
              <Radio.Group value={scheduleMode} onChange={(event) => { setScheduleMode(event.target.value); setScheduleDirty(true); }} disabled={syncRunning}>
                <Radio value="interval">按固定间隔</Radio>
                <Radio value="scheduled">按指定时间点</Radio>
              </Radio.Group>
              {scheduleMode === "interval" ? (
                <div className="fd-inline-actions">
                  <span>每隔</span>
                  <InputNumber min={1} max={1440} value={intervalMinutes} onChange={(value) => { if (value) { setIntervalMinutes(value); setScheduleDirty(true); } }} disabled={syncRunning} style={{ width: 100 }} aria-label="同步间隔（分钟）" />
                  <span>分钟同步一次</span>
                </div>
              ) : (
                <>
                  {scheduleTimes.length === 0 && <p className="fd-note">尚未添加时间点。</p>}
                  {scheduleTimes.map((entry, index) => (
                    <div key={index} className="fd-schedule-row">
                      <TimePicker
                        format="HH:mm"
                        value={dayjs(entry.time, "HH:mm")}
                        onChange={(value) => { if (value) updateTimes((times) => times.map((item, i) => (i === index ? { ...item, time: value.format("HH:mm") } : item))); }}
                        disabled={syncRunning}
                        allowClear={false}
                        minuteStep={5}
                        aria-label={`第 ${index + 1} 个时间点`}
                      />
                      <Checkbox.Group
                        options={DAY_LABELS.map((label, i) => ({ label, value: i + 1 }))}
                        value={entry.days}
                        onChange={(value) => updateTimes((times) => times.map((item, i) => (i === index ? { ...item, days: value as number[] } : item)))}
                        disabled={syncRunning}
                      />
                      <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label="删除时间点" onClick={() => updateTimes((times) => times.filter((_, i) => i !== index))} disabled={syncRunning} />
                    </div>
                  ))}
                  <div className="fd-inline-actions">
                    <Button size="small" icon={<PlusOutlined />} onClick={() => updateTimes((times) => [...times, { time: "09:00", days: [] }])} disabled={syncRunning || scheduleTimes.length >= 10}>添加时间点</Button>
                  </div>
                  <p className="fd-form-note">不勾选星期表示每天执行；时间按系统设置中的时区解释。</p>
                </>
              )}
            </div>
          )}
        </Section>
      )}

      <Section title="同步记录" count={data ? <>共 <span className="num">{runs.length}</span> 条</> : undefined}>
        <Table<MailSyncResult>
          rowKey="run_id"
          size="small"
          loading={loading && !data}
          dataSource={runs}
          pagination={{ pageSize: 10, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 760 }}
          locale={{ emptyText: <EmptyState compact title="还没有同步记录" hint="点击“立即同步”或等待定时任务执行后，记录会出现在这里。" /> }}
          columns={[
            { title: "运行编号", dataIndex: "run_id", width: 200, render: (value: string) => <span className="fd-code fd-code--quiet">{value}</span> },
            { title: "状态", dataIndex: "status", width: 100, render: (value: string) => <StatusText tone={RUN_TONE[value as MailSyncRunStatus] ?? "neutral"}>{MAIL_SYNC_RUN_STATUS_LABEL[value as MailSyncRunStatus] ?? value}</StatusText> },
            { title: "创建时间", dataIndex: "created_at", width: 150, render: (value: string) => <Num className="fd-table-meta">{timeStr(value)}</Num> },
            { title: "发现邮件", dataIndex: "messages_seen", align: "right", width: 100, render: (value: number) => <Num>{value}</Num> },
            { title: "导入附件", dataIndex: "attachments_imported", align: "right", width: 100, render: (value: number) => <Num>{value}</Num> },
            { title: "失败附件", dataIndex: "failed_attachments", align: "right", width: 100, render: (value: number) => <Num style={{ color: value ? "var(--danger)" : undefined }}>{value}</Num> },
            { title: "错误数", dataIndex: "error_count", align: "right", width: 90, render: (value: number) => <Num style={{ color: value ? "var(--danger)" : undefined }}>{value}</Num> },
          ]}
        />
      </Section>

      <Section
        title="邮件列表"
        count={messages.length ? <>共 <span className="num">{messages.length}</span> 条</> : undefined}
        tools={
          <Button
            type="primary"
            size="small"
            disabled={selectedMessageIds.length === 0}
            loading={downloading}
            onClick={() => void downloadSelected()}
          >
            下载所选附件
          </Button>
        }
      >
        <PageError message={messagesState.error} onRetry={() => void messagesState.reload()} retrying={messagesState.loading} />
        <Table<MailMessage>
          rowKey="id"
          size="small"
          loading={messagesState.loading && !messagesState.data}
          dataSource={messages}
          rowSelection={{
            selectedRowKeys: selectedMessageIds,
            onChange: (keys) => setSelectedMessageIds(keys.filter((key): key is number => typeof key === "number")),
          }}
          pagination={{ pageSize: 10, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 720 }}
          locale={{ emptyText: <EmptyState compact title="还没有同步到的邮件" hint="同步收件箱后，邮件会出现在这里，可多选后批量下载其中的估值表附件。" /> }}
          columns={[
            { title: "发件人", dataIndex: "sender", width: 200, ellipsis: true },
            { title: "主题", dataIndex: "subject", ellipsis: true, render: (value: string | null) => value || <span className="fd-table-meta">（无主题）</span> },
            { title: "接收时间", dataIndex: "received_at", width: 160, render: (value: string) => <Num className="fd-table-meta">{timeStr(value)}</Num> },
            { title: "附件数", dataIndex: "attachment_count", align: "right", width: 90, render: (value: number) => <Num>{value}</Num> },
            { title: "附件大小", dataIndex: "total_size", align: "right", width: 110, render: (value: number) => <Num>{formatBytes(value)}</Num> },
          ]}
        />
      </Section>
    </div>
  );
}
