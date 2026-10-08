import { useEffect, useState } from "react";
import { Button, Form, InputNumber, Segmented, Select, Skeleton } from "antd";
import { Link } from "react-router-dom";
import { ReloadOutlined } from "@ant-design/icons";
import * as systemApi from "@/api/system";
import type { MailSyncSchedule, SystemSetting } from "@/api/types";
import { PageError, PageHeader, Section, describeError, useAsyncData, useToast } from "@/components";
import { SETTING_DEFINITIONS, type SettingKey } from "@/utils/constants";

const SETTING_LABELS: Record<string, string> = {
  ...Object.fromEntries(Object.entries(SETTING_DEFINITIONS).map(([key, definition]) => [key, definition.label])),
  mail_sync_enabled: "自动邮件同步",
  mail_sync_schedule: "邮件同步计划",
};

const SOURCE_LABELS: Record<string, string> = { default: "默认值", environment: "环境变量", database: "系统设置", persisted: "系统设置", runtime: "运行时配置" };

const TIMEZONES = [
  { value: "Asia/Shanghai", label: "Asia/Shanghai（UTC+8）" },
  { value: "Asia/Hong_Kong", label: "Asia/Hong_Kong（UTC+8）" },
  { value: "UTC", label: "UTC" },
  { value: "America/New_York", label: "America/New_York" },
];

function describeSetting(key: string, value: SystemSetting["value"] | MailSyncSchedule | unknown): string {
  if (key === "mail_sync_schedule" && value && typeof value === "object") {
    const schedule = value as MailSyncSchedule;
    if (schedule.mode === "interval") return `每 ${schedule.interval_minutes ?? "—"} 分钟`;
    const times = schedule.times ?? [];
    return times.length ? `每日 ${times.map((item) => item.time).join("、")}` : "未设置时间点";
  }
  if (typeof value === "boolean") return value ? "启用" : "暂停";
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function AdminSettings() {
  const toast = useToast();
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [form] = Form.useForm();
  const state = useAsyncData(() => systemApi.getSettings(), [], "系统设置加载失败");
  const { data, loading, error, reload } = state;
  const settings = data?.data ?? {};
  const showSkeleton = loading && !data;
  // The backend ships this note in English; present the same meaning in the UI language.
  const note = typeof data?.meta?.runtime_note === "string" && data.meta.runtime_note
    ? "自动邮件同步开关在每次定时运行前读取，保存后立即生效；其余取值会保存到数据库，但需要后台 worker 重启后才会生效。"
    : "";

  useEffect(() => {
    if (!data) return;
    form.setFieldsValue(Object.fromEntries(Object.entries(data.data).filter(([key]) => key !== "mail_sync_schedule").map(([key, value]) => [key, value.value])));
  }, [data, form]);

  async function save() {
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    setActionError(null);
    try {
      await systemApi.updateSettings(values);
      toast.success("系统设置已更新");
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "系统设置保存失败，请检查取值范围"));
    } finally {
      setSaving(false);
    }
  }

  const numericKeys = (Object.keys(SETTING_DEFINITIONS) as SettingKey[]).filter((key) => key !== "timezone");

  return (
    <div className="fd-page fd-page--narrow">
      <PageHeader
        title="系统设置"
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading}>刷新</Button>
            <Button type="primary" loading={saving} disabled={loading || !data} onClick={() => void save()}>保存设置</Button>
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <Section title="保留与时区">
        {showSkeleton ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : (
          <Form form={form} layout="horizontal" className="fd-settings-form" colon={false} labelAlign="left" labelWrap>
            {numericKeys.map((key) => {
              const definition = SETTING_DEFINITIONS[key];
              return (
                <Form.Item
                  className="fd-setting-item"
                  key={key}
                  label={<span className="fd-setting-copy"><span>{definition.label}</span><small>允许范围 {"min" in definition ? definition.min : 0} – {"max" in definition ? definition.max : 9999} 天</small></span>}
                  name={key}
                  rules={[{ required: true, message: "请输入天数" }]}
                >
                  <InputNumber min={"min" in definition ? definition.min : 0} max={"max" in definition ? definition.max : 9999} addonAfter="天" style={{ width: "100%" }} />
                </Form.Item>
              );
            })}
            <Form.Item className="fd-setting-item" label={<span className="fd-setting-copy"><span>{SETTING_DEFINITIONS.timezone.label}</span><small>用于解释定时同步时间点与运维任务时间。</small></span>} name="timezone" rules={[{ required: true, message: "请选择时区" }]}>
              <Select showSearch options={TIMEZONES} />
            </Form.Item>
            <Form.Item className="fd-setting-item" label={<span className="fd-setting-copy"><span>自动邮件同步</span><small>同步计划（间隔或时间点）在 <Link to="/mail">邮件接入</Link> 页维护。</small></span>} name="mail_sync_enabled">
              <Segmented options={[{ value: true, label: "启用" }, { value: false, label: "暂停" }]} />
            </Form.Item>
          </Form>
        )}
      </Section>

      <Section title="当前生效值与来源">
        {showSkeleton ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : (
          <dl className="fd-setting-values">
            {Object.entries(settings).map(([key, value]) => (
              <div key={key}>
                <dt>{SETTING_LABELS[key] ?? key}</dt>
                <dd>
                  <span>{describeSetting(key, value.value)}</span>
                  <span className="fd-caption">来源：{SOURCE_LABELS[value.source] ?? value.source}</span>
                </dd>
              </div>
            ))}
          </dl>
        )}
        {note && <p className="fd-form-note">{note}</p>}
      </Section>
    </div>
  );
}
