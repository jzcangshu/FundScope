import { useState } from "react";
import { Button, Modal, Table, Tooltip, Upload } from "antd";
import { Link } from "react-router-dom";
import { DownloadOutlined, ReloadOutlined, UploadOutlined } from "@ant-design/icons";
import * as importsApi from "@/api/imports";
import * as downloads from "@/api/downloads";
import type { ImportBatchDetail, ImportValidationVersion, Job } from "@/api/types";
import {
  Chip,
  EmptyState,
  LevelTag,
  PageError,
  PageHeader,
  RoleGuard,
  Section,
  StatusText,
  Truncate,
  describeError,
  useAsyncData,
  useInterval,
  useToast,
  type Tone,
} from "@/components";
import {
  IMPORT_BATCH_STATUS_LABEL,
  JOB_STATUS_LABEL,
  SOURCE_TYPE_LABEL,
  VALUATION_STATUS_LABEL,
  type ImportBatchStatus,
  type JobStatus,
  type SourceType,
  type ValuationStatus,
} from "@/utils/constants";
import { formatBytes, timeStr } from "@/utils/format";

const PAGE_SIZE = 20;
const ACTIVE_JOBS: JobStatus[] = ["pending", "running", "retry_due"];
const JOB_TONE: Record<JobStatus, Tone> = { pending: "neutral", running: "info", retry_due: "warning", succeeded: "success", failed: "danger" };
const BATCH_TONE: Record<ImportBatchStatus, Tone> = { created: "neutral", queued: "neutral", processing: "info", completed: "success", failed: "danger" };
const VERSION_TONE: Partial<Record<ValuationStatus, Tone>> = {
  published: "success",
  publishable: "warning",
  pending_review: "danger",
  failed: "danger",
  rejected: "neutral",
  duplicate: "neutral",
  non_valuation: "neutral",
};

function JobTag({ job }: { job: Job | null }) {
  if (!job) return <span className="fd-muted">—</span>;
  const status = <StatusText tone={JOB_TONE[job.status]}>{JOB_STATUS_LABEL[job.status]}</StatusText>;
  const detail = [
    job.attempts ? `第 ${job.attempts}/${job.max_attempts} 次尝试` : null,
    job.error_code ? `错误 ${job.error_code}` : null,
    job.next_retry_at ? `下次重试 ${timeStr(job.next_retry_at)}` : null,
  ].filter(Boolean).join(" · ");
  // Tooltip needs a plain DOM child to receive the trigger handlers.
  return detail ? <Tooltip title={detail}><span>{status}</span></Tooltip> : status;
}

export default function Imports() {
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<ImportBatchDetail | null>(null);
  const [validations, setValidations] = useState<ImportValidationVersion[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const state = useAsyncData(() => importsApi.listBatches({ page, page_size: PAGE_SIZE }), [page], "导入批次加载失败");
  const { data, loading, error, refreshing, reload } = state;
  const rows = data?.data ?? [];
  const activeCount = rows.filter((row) => row.job && ACTIVE_JOBS.includes(row.job.status)).length;
  const hasActiveJob = activeCount > 0;
  useInterval(() => void reload({ silent: true }), hasActiveJob ? 5_000 : 60_000, Boolean(data));

  const detailActive = Boolean(detail?.job && ACTIVE_JOBS.includes(detail.job.status));
  useInterval(() => {
    if (!detail) return;
    void importsApi.getBatch(detail.id).then((result) => setDetail(result.data)).catch(() => undefined);
  }, 3_000, detailActive);

  async function upload(file: File) {
    setUploading(true);
    setActionError(null);
    try {
      const batch = await importsApi.createBatch();
      await importsApi.uploadFile(batch.data.id, file);
      const completed = await importsApi.completeBatch(batch.data.id);
      setValidations(null);
      setDetail(completed.data);
      toast.success(`${file.name} 已提交处理`);
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "文件上传失败，请检查文件格式后重试"));
      await reload({ silent: true });
    } finally {
      setUploading(false);
    }
  }

  async function retry(id: number) {
    setActionError(null);
    try {
      await importsApi.retryBatch(id);
      toast.success(`批次 #${id} 已重新排队`);
      await reload({ silent: true });
    } catch (caught) {
      setActionError(describeError(caught, "批次重试失败，请稍后重试"));
    }
  }

  async function openDetail(id: number, withValidations: boolean) {
    setActionError(null);
    try {
      const [batch, result] = await Promise.all([importsApi.getBatch(id), withValidations ? importsApi.getValidations(id) : Promise.resolve(null)]);
      setDetail(batch.data);
      setValidations(result ? result.data : null);
    } catch (caught) {
      setActionError(describeError(caught, "批次详情加载失败"));
    }
  }

  const pendingReview = validations?.filter((version) => version.status === "pending_review" || version.status === "publishable").length ?? 0;

  return (
    <div className="fd-page">
      <PageHeader
        title="导入中心"
        status={
          <>
            <span>共 <b className="num">{data ? data.meta.total : "—"}</b> 个批次</span>
            {hasActiveJob && <StatusText tone="info">{activeCount} 个处理中</StatusText>}
          </>
        }
        extra={
          <>
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading || refreshing}>刷新</Button>
            <RoleGuard cap="imports">
              <Upload accept=".xls,.xlsx" multiple={false} showUploadList={false} beforeUpload={(file) => { void upload(file); return false; }}>
                <Button type="primary" icon={<UploadOutlined />} loading={uploading}>上传估值表</Button>
              </Upload>
            </RoleGuard>
          </>
        }
      >
        <PageError message={error} onRetry={() => void reload()} retrying={loading} />
        <PageError message={actionError} onClose={() => setActionError(null)} />
      </PageHeader>

      <Section title="批次">
        <Table<importsApi.ImportBatchRow>
          rowKey="id"
          size="small"
          loading={loading && !data}
          dataSource={rows}
          pagination={{ current: page, pageSize: PAGE_SIZE, total: data?.meta.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          scroll={{ x: 820 }}
          locale={{ emptyText: <EmptyState compact title="还没有导入批次" hint="点击右上角“上传估值表”，或等待邮件自动同步产生批次。" /> }}
          columns={[
            { title: "批次", dataIndex: "id", width: 90, render: (value: number) => <span className="mono">#{value}</span> },
            { title: "来源", dataIndex: "source_type", width: 110, render: (value: SourceType) => SOURCE_TYPE_LABEL[value] ?? value },
            { title: "文件数", dataIndex: "file_count", width: 80, align: "right" },
            {
              title: "批次状态",
              dataIndex: "status",
              width: 110,
              render: (value: ImportBatchStatus) => <StatusText tone={BATCH_TONE[value] ?? "neutral"}>{IMPORT_BATCH_STATUS_LABEL[value] ?? value}</StatusText>,
            },
            { title: "任务状态", width: 110, render: (_, row) => <JobTag job={row.job} /> },
            { title: "创建时间", dataIndex: "created_at", width: 150, render: (value: string) => <span className="num fd-table-meta">{timeStr(value)}</span> },
            {
              title: "操作",
              width: 220,
              render: (_, row) => (
                <span className="fd-row-actions">
                  <Button type="link" size="small" onClick={() => void openDetail(row.id, false)}>详情</Button>
                  <Button type="link" size="small" onClick={() => void openDetail(row.id, true)}>校验结果</Button>
                  {row.job?.can_retry && <Button type="link" size="small" onClick={() => void retry(row.id)}>重试</Button>}
                </span>
              ),
            },
          ]}
        />
        <p className="fd-form-note">干净版本自动发布，有异常的版本进入异常复核。</p>
      </Section>

      <Modal
        open={detail !== null}
        title={detail ? `批次 #${detail.id}` : "批次详情"}
        width={760}
        onCancel={() => { setDetail(null); setValidations(null); }}
        footer={null}
        destroyOnClose
      >
        {detail && (
          <div className="fd-stack">
            <div className="fd-inline-actions">
              <span className="fd-caption">{SOURCE_TYPE_LABEL[detail.source_type] ?? detail.source_type} · {timeStr(detail.created_at)}</span>
              <StatusText tone={BATCH_TONE[detail.status] ?? "neutral"}>{IMPORT_BATCH_STATUS_LABEL[detail.status] ?? detail.status}</StatusText>
              <JobTag job={detail.job} />
              {detailActive && <span className="fd-sync-running"><span className="fd-sync-running__dot" />后台处理中，状态会自动刷新</span>}
              {detail.job?.can_retry && <Button size="small" onClick={() => void retry(detail.id)}>重试</Button>}
            </div>
            <div>
              <div className="fd-section-head">
                <span className="fd-section-title">文件</span>
              </div>
              {detail.files.length ? (
                <ul className="fd-file-list">
                  {detail.files.map((file) => (
                    <li className="fd-file-row" key={file.id}>
                      <div className="fd-file-row__body">
                        <div className="fd-file-row__name"><Truncate value={file.original_filename} maxChars={60} /></div>
                        <div className="fd-file-row__meta">
                          <span className="num">{formatBytes(file.file_size)}</span>
                          <span>指纹 <Tooltip title={file.file_hash}><span className="fd-code fd-code--quiet">{file.file_hash.slice(0, 12)}…</span></Tooltip></span>
                          {file.duplicate ? <Chip>重复</Chip> : <span>未重复</span>}
                        </div>
                      </div>
                      <Button className="fd-file-row__action" type="link" size="small" icon={<DownloadOutlined />} onClick={() => void downloads.downloadBlob(`/imports/${detail.id}/source/${file.id}`, file.original_filename)}>原文件</Button>
                    </li>
                  ))}
                </ul>
              ) : <EmptyState compact title="暂无文件" />}
            </div>
            {validations !== null && (
              <div>
                <div className="fd-section-head">
                  <span className="fd-section-title">校验结果</span>
                  {pendingReview > 0 && <Link to="/reviews">前往异常复核（{pendingReview} 个版本待处理）</Link>}
                </div>
                <Table<ImportValidationVersion>
                  rowKey="version_id"
                  size="small"
                  pagination={false}
                  dataSource={validations}
                  locale={{ emptyText: <EmptyState compact title="暂无校验结果" hint="任务完成后，此处会列出每个估值日的版本状态与校验发现。" /> }}
                  columns={[
                    { title: "产品", dataIndex: "fund_id", width: 120, render: (value: number) => <Link to={`/funds/${value}`}>产品 #{value}</Link> },
                    { title: "估值日", dataIndex: "valuation_date", width: 120 },
                    {
                      title: "版本状态",
                      dataIndex: "status",
                      width: 110,
                      render: (value: ValuationStatus) => <StatusText tone={VERSION_TONE[value] ?? "neutral"}>{VALUATION_STATUS_LABEL[value] ?? value}</StatusText>,
                    },
                    {
                      title: "校验发现",
                      dataIndex: "findings",
                      render: (items: ImportValidationVersion["findings"]) => items.length === 0 ? <span className="fd-muted">无</span> : (
                        <div className="fd-stack fd-stack--tight">
                          {items.slice(0, 3).map((item, index) => (
                            <div key={index}>
                              <span className="fd-inline-actions"><LevelTag level={item.level} /><Truncate value={item.message} maxChars={72} /></span>
                            </div>
                          ))}
                          {items.length > 3 && <div className="fd-caption">另有 {items.length - 3} 条，请在异常复核中查看</div>}
                        </div>
                      ),
                    },
                  ]}
                />
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
