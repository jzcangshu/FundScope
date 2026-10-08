import { useState } from "react";
import { Button, Input, Modal } from "antd";
import { DeleteOutlined, PlayCircleOutlined } from "@ant-design/icons";
import * as systemApi from "@/api/system";
import type { MaintenanceResult } from "@/api/types";
import {
  Disclosure,
  EmptyState,
  Facts,
  PageError,
  PageHeader,
  Section,
  Stat,
  StatusText,
  describeError,
  useToast,
} from "@/components";
import { formatBytes } from "@/utils/format";

const CONFIRM_PHRASE = "DELETE_EXPIRED_SOURCE_FILES";

const SKIP_REASON_LABEL: Record<string, string> = {
  referenced_by_job: "仍被后台任务引用",
  pending_review: "关联版本待复核",
  backup_missing: "缺少备份",
  audit_lock: "审计锁定",
  not_expired: "未到保留期",
};

export default function AdminRetention() {
  const toast = useToast();
  const [result, setResult] = useState<MaintenanceResult | null>(null);
  const [busy, setBusy] = useState<"preview" | "execute" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [executeOpen, setExecuteOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("");

  const summary = result?.summary ?? {};
  const isDryRun = summary.dry_run !== false;
  const failed = result?.status === "failed";
  const canExecute = Boolean(result) && !failed && isDryRun;
  const skipped = (summary.skipped_reasons ?? {}) as Record<string, number>;
  const skippedEntries = Object.entries(skipped).filter(([, count]) => Number(count) > 0);
  const errorCount = Number(summary.error_count ?? 0);
  const executeReady = confirmation === CONFIRM_PHRASE && reason.trim().length > 0;

  async function runPreview() {
    setBusy("preview");
    setError(null);
    try {
      setResult((await systemApi.previewRetention()).data);
      toast.success("清理预演已完成");
    } catch (caught) {
      setError(describeError(caught, "清理预演失败，请检查运维状态"));
    } finally {
      setBusy(null);
    }
  }

  async function execute() {
    if (!executeReady) return;
    setBusy("execute");
    setError(null);
    try {
      setResult((await systemApi.executeRetention(reason.trim())).data);
      setExecuteOpen(false);
      setConfirmation("");
      setReason("");
      toast.success("清理执行完成");
    } catch (caught) {
      setError(describeError(caught, "清理执行失败，系统未绕过安全检查"));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="fd-page fd-page--narrow">
      <PageHeader
        title="数据保留"
        status={
          result ? (
            <StatusText tone={failed ? "danger" : "success"}>{failed ? "失败" : isDryRun ? "预演成功" : "执行成功"}</StatusText>
          ) : (
            <span>尚未预演</span>
          )
        }
        extra={
          <>
            <Button icon={<PlayCircleOutlined />} onClick={() => void runPreview()} loading={busy === "preview"} disabled={busy !== null}>清理预演</Button>
            <Button danger type="primary" icon={<DeleteOutlined />} disabled={!canExecute || busy !== null} onClick={() => setExecuteOpen(true)}>确认执行</Button>
          </>
        }
      >
        <PageError message={error} onClose={() => setError(null)} />
        <p className="fd-note">
          按系统设置中的保留天数滚动清理已过期的原始估值文件。必须先预演，再输入确认短语执行。
        </p>
        <p className="fd-form-note">
          只作用于已过保留期且通过备份、任务引用、复核与审计锁检查的文件，数据库中的标准化数据与审计记录不会被删除。
        </p>
      </PageHeader>

      <Section title={result ? (isDryRun ? "预演结果" : "执行结果") : "清理结果"}>
        {!result ? (
          <EmptyState compact title="尚未预演" hint="点击“清理预演”查看将被清理的文件数量与容量；预演不会删除任何文件。" />
        ) : (
          <div className="fd-stack">
            <div className="fd-stats fd-stats--compact" role="group" aria-label="清理结果概览">
              <Stat label="候选文件" value={<>{Number(summary.candidate_count ?? 0)}<small>个</small></>} />
              <Stat label="候选容量" value={formatBytes(Number(summary.total_size ?? 0))} />
              <Stat label={isDryRun ? "预计删除" : "已删除"} value={<>{Number(summary.deleted_count ?? 0)}<small>个</small></>} />
              <Stat label={isDryRun ? "预计释放" : "已释放"} value={formatBytes(Number(summary.deleted_size ?? 0))} />
            </div>
            {errorCount > 0 && (
              <PageError message={`有 ${errorCount} 个文件处理出错${result.error_code ? `（${result.error_code}）` : ""}`} />
            )}
            {skippedEntries.length > 0 && (
              <Disclosure summary="跳过原因">
                <Facts items={skippedEntries.map(([key, count]) => ({ label: SKIP_REASON_LABEL[key] ?? key, value: count }))} />
              </Disclosure>
            )}
          </div>
        )}
      </Section>

      <Modal
        open={executeOpen}
        title="确认执行原始文件清理"
        onCancel={() => setExecuteOpen(false)}
        onOk={() => void execute()}
        okText="确认执行"
        okButtonProps={{ danger: true, disabled: !executeReady, loading: busy === "execute" }}
        destroyOnClose
      >
        <div className="fd-stack fd-stack--tight">
          <p className="fd-caption">
            将删除预演中的 {Number(summary.deleted_count ?? 0)} 个文件（{formatBytes(Number(summary.deleted_size ?? 0))}）。请输入固定确认短语并填写原因；后端仍会执行全部安全检查。
          </p>
          <Input
            placeholder={`请输入 ${CONFIRM_PHRASE}`}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value.trim())}
            autoComplete="off"
            status={confirmation && confirmation !== CONFIRM_PHRASE ? "error" : undefined}
            aria-label="确认短语"
          />
          <Input.TextArea
            placeholder="操作原因，将写入审计日志"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={1000}
            showCount
            rows={3}
            aria-label="操作原因"
          />
        </div>
      </Modal>
    </div>
  );
}
