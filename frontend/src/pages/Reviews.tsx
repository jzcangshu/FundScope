import { useState } from "react";
import { Alert, Button, Drawer, Input, Modal, Segmented, Table, Tooltip } from "antd";
import { Link } from "react-router-dom";
import { DownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import * as reviewsApi from "@/api/reviews";
import * as downloads from "@/api/downloads";
import type { ReviewItem } from "@/api/types";
import {
  EmptyState,
  Facts,
  FindingTable,
  PageError,
  PageHeader,
  Section,
  StatusText,
  Truncate,
  describeError,
  useAsyncData,
  useConfirm,
  useInterval,
  useMediaQuery,
  useModal,
  useToast,
  type Tone,
} from "@/components";
import { VALUATION_STATUS_LABEL, type ValuationStatus } from "@/utils/constants";
import { dateStr, formatBytes } from "@/utils/format";

const PAGE_SIZE = 20;
const VIEWS: ValuationStatus[] = ["pending_review", "publishable", "published", "revoked"];
const VIEW_HINT: Record<string, string> = {
  pending_review: "含阻断级异常的版本。复核后可发布并把异常记为人工忽略，或驳回。",
  publishable: "仅含警告级异常的版本。发布时需填写忽略原因。",
  published: "已发布版本。撤回会使看板不再展示该估值日数据，并触发重算。",
  revoked: "已作废版本。恢复后重新进入发布状态并触发重算。",
};

/** Version lifecycle state as a dot + plain text, never a coloured tag. */
const STATUS_TONE: Record<ValuationStatus, Tone> = {
  received: "info",
  parsing: "info",
  validating: "info",
  publishable: "warning",
  pending_review: "warning",
  published: "success",
  superseded: "neutral",
  rejected: "danger",
  failed: "danger",
  duplicate: "neutral",
  non_valuation: "neutral",
  revoked: "neutral",
};

const activeFindings = (row: ReviewItem) => (row.findings ?? []).filter((finding) => !finding.ignored);

export default function Reviews() {
  const toast = useToast();
  const modal = useModal();
  const confirm = useConfirm();
  const wide = useMediaQuery("(min-width: 1024px)");
  const [status, setStatus] = useState<ValuationStatus>("pending_review");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<ReviewItem | null>(null);
  const [ackOpen, setAckOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const state = useAsyncData(() => reviewsApi.listReviews({ status, page, page_size: PAGE_SIZE }), [status, page], "复核队列加载失败");
  const { data, loading, error, refreshing, reload } = state;
  const total = data?.meta.total ?? 0;
  useInterval(() => void reload({ silent: true }), 30_000, Boolean(data) && !selected);

  async function run(label: string, action: () => Promise<void>, fallback: string) {
    setBusy(true);
    setActionError(null);
    try {
      await action();
      toast.success(label);
      setSelected(null);
      setAckOpen(false);
      setNote("");
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, fallback));
    } finally {
      setBusy(false);
    }
  }

  async function acknowledgeAndPublish() {
    if (!selected || !note.trim()) return;
    const version = selected;
    const reason = note.trim();
    await run("版本已发布，异常已记为人工忽略", async () => {
      const result = await reviewsApi.acknowledgeReview(version.id, { allow_publish: true, note: reason });
      if (result.data.status === "publishable") await reviewsApi.publishVersion(version.id, { reason, confirm_warnings: true });
    }, "复核或发布失败，请检查版本状态后重试");
  }

  async function publish(row: ReviewItem) {
    const count = activeFindings(row).length;
    const reason = await confirm({
      title: "发布并忽略校验异常",
      description: `发布后，${count} 条当前异常会标记为“人工已忽略”，质量看板不再重复提醒；原始异常与操作原因仍保留在审计记录中。`,
      reasonLabel: "忽略原因",
      reasonRequired: true,
      okText: "确认发布",
    });
    if (reason === null) return;
    await run(`版本已发布，${count} 条异常已忽略`, () => reviewsApi.publishVersion(row.id, { reason, confirm_warnings: true }).then(() => undefined), "版本发布失败，请检查当前状态后重试");
  }

  async function batchPublish() {
    const reason = await confirm({
      title: "批量发布全部可发布版本",
      description: `当前队列共 ${total} 个版本。系统只处理确认瞬间仍处于“可发布”的版本；每条异常都会记为人工忽略，失败项会单独列出。`,
      reasonLabel: "统一发布 / 忽略原因",
      reasonRequired: true,
      okText: "确认批量发布",
    });
    if (reason === null) return;
    setBusy(true);
    setActionError(null);
    try {
      const result = await reviewsApi.batchPublish(reason);
      const failed = result.data.failed;
      toast.success(`批量发布完成：成功 ${result.data.published} 个${failed.length ? `，失败 ${failed.length} 个` : ""}`);
      if (failed.length) {
        modal.warning({
          title: "批量发布存在失败项",
          content: (
            <div className="fd-stack">
              <p>成功 {result.data.published} 个，失败 {failed.length} 个。失败项状态未被改变，请打开对应版本详情定位原因。</p>
              {/* Scrolls when many versions fail; one label / value row each. */}
              <div className="fd-scrollbox">
                <Facts items={failed.map((item) => ({ label: `版本 #${item.version_id}`, value: item.error, wide: true }))} />
              </div>
            </div>
          ),
          okText: "知道了",
        });
      }
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "批量发布失败，请检查当前状态后重试"));
    } finally {
      setBusy(false);
    }
  }

  async function lifecycle(kind: "reject" | "revoke" | "restore", row: ReviewItem) {
    const titles = { reject: "驳回版本", revoke: "撤回版本", restore: "恢复版本" };
    const reason = await confirm({
      title: titles[kind],
      description: `${row.fund_name} · ${dateStr(row.valuation_date)} · v${row.version_no}。操作原因将写入审计日志。`,
      reasonRequired: true,
      danger: kind !== "restore",
      okText: "确认",
    });
    if (reason === null) return;
    const call = kind === "reject" ? reviewsApi.rejectVersion : kind === "revoke" ? reviewsApi.revokeVersion : reviewsApi.restoreVersion;
    await run("版本状态已更新", () => call(row.id, reason).then(() => undefined), "版本操作失败，请检查当前状态和权限");
  }

  return (
    <div className="fd-page">
      <PageHeader
        title="异常复核"
        status={<span>{VALUATION_STATUS_LABEL[status]} <b className="num">{data ? total : "—"}</b> 个版本</span>}
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading || refreshing}>刷新</Button>
            {status === "publishable" && (
              <Button type="primary" disabled={total === 0 || busy} loading={busy} onClick={() => void batchPublish()}>
                批量发布全部（{total}）
              </Button>
            )}
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <Section
        title="版本"
        tools={
          <Segmented
            value={status}
            onChange={(value) => { setPage(1); setStatus(value as ValuationStatus); }}
            options={VIEWS.map((value) => ({ value, label: VALUATION_STATUS_LABEL[value] }))}
            aria-label="版本状态视图"
          />
        }
      >
        <p className="fd-section__note">{VIEW_HINT[status]}</p>
        <Table<ReviewItem>
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={data?.data ?? []}
          pagination={{ current: page, pageSize: PAGE_SIZE, total, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 900 }}
          locale={{ emptyText: <EmptyState compact title={`没有${VALUATION_STATUS_LABEL[status]}的版本`} hint="导入的估值表出现异常后会进入这里。" /> }}
          columns={[
            { title: "产品", dataIndex: "fund_name", ellipsis: true, render: (value: string, row) => <Link className="fd-table-name" to={`/funds/${row.fund_id}`}>{value}</Link> },
            { title: "估值日", dataIndex: "valuation_date", width: 120, render: (value: string) => <span className="num fd-table-meta">{dateStr(value)}</span> },
            { title: "版本", dataIndex: "version_no", width: 70, render: (value: number) => <span className="mono">v{value}</span> },
            {
              title: "待处理异常",
              width: 140,
              render: (_, row) => (
                <StatusText tone={row.critical_count ? "danger" : row.warning_count ? "warning" : "neutral"}>
                  <span className="num">{row.critical_count + row.warning_count}</span> 条{row.critical_count ? `（阻断 ${row.critical_count}）` : ""}
                </StatusText>
              ),
            },
            { title: "已忽略", dataIndex: "ignored_count", width: 80, align: "right", render: (value: number) => <span className="num">{value || "—"}</span> },
            { title: "原始文件", dataIndex: "source_filename", render: (value: string | null) => <Truncate value={value ?? "未关联文件"} maxChars={36} /> },
            {
              title: "操作",
              width: 240,
              render: (_, row) => (
                <span className="fd-row-actions">
                  <Button type="link" size="small" onClick={() => { setAckOpen(false); setSelected(row); }}>详情</Button>
                  {row.status === "publishable" && <Button type="link" size="small" onClick={() => void publish(row)}>发布并忽略</Button>}
                  {row.status === "pending_review" && (
                    <>
                      <Button type="link" size="small" onClick={() => { setNote(""); setSelected(row); setAckOpen(true); }}>复核并发布</Button>
                      <Button type="link" size="small" danger onClick={() => void lifecycle("reject", row)}>驳回</Button>
                    </>
                  )}
                  {row.status === "published" && <Button type="link" size="small" danger onClick={() => void lifecycle("revoke", row)}>撤回</Button>}
                  {(row.status === "revoked" || row.status === "superseded") && <Button type="link" size="small" onClick={() => void lifecycle("restore", row)}>恢复</Button>}
                </span>
              ),
            },
          ]}
        />
      </Section>

      <Modal
        open={selected !== null && ackOpen}
        title="复核并发布"
        onCancel={() => { setAckOpen(false); setSelected(null); }}
        onOk={() => void acknowledgeAndPublish()}
        okText="确认发布并忽略"
        okButtonProps={{ disabled: !note.trim(), loading: busy }}
        destroyOnClose
      >
        <Alert
          type="warning"
          showIcon
          message={`发布后将把 ${selected ? activeFindings(selected).length : 0} 条异常标记为人工忽略`}
          description="不会删除原始记录；质量看板与复核队列不再重复提示。复核意见与原因会写入审计日志。"
        />
        <Input.TextArea
          style={{ marginTop: 16 }}
          value={note}
          autoFocus
          onChange={(event) => setNote(event.target.value)}
          placeholder="填写你接受该异常的原因，例如：已与托管方核对原始估值表"
          maxLength={2000}
          showCount
          rows={4}
        />
      </Modal>

      <Drawer
        open={selected !== null && !ackOpen}
        width={wide ? 880 : "100%"}
        title={selected ? `复核详情 · ${selected.fund_name}` : "复核详情"}
        onClose={() => setSelected(null)}
        destroyOnClose
        extra={selected?.status === "publishable" && activeFindings(selected).length > 0 && (
          <Button type="primary" onClick={() => void publish(selected)}>发布并忽略这些异常</Button>
        )}
      >
        {selected && (
          <div className="fd-stack">
            <Facts
              items={[
                { label: "产品", value: <Link to={`/funds/${selected.fund_id}`}>{selected.fund_name}</Link> },
                { label: "估值日", value: <span className="num">{dateStr(selected.valuation_date)}</span> },
                { label: "版本", value: <span className="mono">v{selected.version_no}</span> },
                { label: "状态", value: <StatusText tone={STATUS_TONE[selected.status]}>{VALUATION_STATUS_LABEL[selected.status]}</StatusText> },
                { label: "待处理异常", value: <><span className="num">{activeFindings(selected).length}</span> 条</> },
                { label: "已忽略异常", value: <><span className="num">{selected.ignored_count || 0}</span> 条</> },
              ]}
            />
            <div>
              <div className="fd-section-head">
                <div className="fd-section-title">原始文件</div>
              </div>
              {selected.source_filename ? (
                <div className="fd-file-row">
                  <div className="fd-file-row__body">
                    <div className="fd-file-row__name">{selected.source_filename}</div>
                    <div className="fd-file-row__meta">
                      <span>批次 <span className="mono">#{selected.import_batch_id ?? "—"}</span></span>
                      <span className="num">{formatBytes(selected.source_file_size)}</span>
                      <span>指纹 <Tooltip title={selected.source_file_hash}><span className="fd-code fd-code--quiet">{selected.source_file_hash?.slice(0, 16) ?? "—"}</span></Tooltip></span>
                    </div>
                  </div>
                  {selected.source_file_id && selected.import_batch_id && (
                    <Button
                      className="fd-file-row__action"
                      size="small"
                      icon={<DownloadOutlined />}
                      onClick={() => void downloads.downloadBlob(`/imports/${selected.import_batch_id}/source/${selected.source_file_id}`, selected.source_filename!)}
                    >
                      下载原始文件
                    </Button>
                  )}
                </div>
              ) : (
                <span className="fd-caption">该版本没有关联可下载的原始文件。</span>
              )}
            </div>
            <div>
              <div className="fd-section-head">
                <div className="fd-section-title">逐条校验结果</div>
              </div>
              <FindingTable findings={selected.findings ?? []} compact />
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
