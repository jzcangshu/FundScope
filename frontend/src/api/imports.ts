import { apiRequest } from "./client";
import type {
  ApiEnvelope,
  ApiPage,
  ImportBatch,
  ImportBatchDetail,
  ImportBatchFile,
  ImportValidationVersion,
  Job,
} from "./types";
import type { ImportBatchStatus, SourceType } from "@/utils/constants";

export interface ImportBatchListParams {
  source_type?: SourceType;
  status?: ImportBatchStatus;
  page?: number;
  page_size?: number;
}

export interface ImportBatchRow extends ImportBatch {
  job: Job | null;
}

export function listBatches(params: ImportBatchListParams = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) query.set(key, String(value));
  }
  return apiRequest<ApiPage<ImportBatchRow>>(`/imports?${query.toString()}`);
}

export function createBatch(source_type: SourceType = "upload") {
  return apiRequest<ApiEnvelope<ImportBatch>>("/imports", {
    method: "POST",
    body: { source_type },
  });
}

export function uploadFile(batchId: number, file: File) {
  const body = new FormData();
  body.append("file", file);
  return apiRequest<ApiEnvelope<ImportBatchFile>>(`/imports/${batchId}/files`, {
    method: "POST",
    body,
  });
}

export function completeBatch(batchId: number) {
  return apiRequest<ApiEnvelope<ImportBatchDetail>>(`/imports/${batchId}/complete`, {
    method: "POST",
  });
}

export function getBatch(batchId: number) {
  return apiRequest<ApiEnvelope<ImportBatchDetail>>(`/imports/${batchId}`);
}

export function retryBatch(batchId: number) {
  return apiRequest<ApiEnvelope<ImportBatchDetail>>(`/imports/${batchId}/retry`, {
    method: "POST",
  });
}

export function getValidations(batchId: number) {
  return apiRequest<ApiPage<ImportValidationVersion>>(`/imports/${batchId}/validations`);
}
