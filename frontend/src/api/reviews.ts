import { apiRequest } from "./client";
import type { ApiEnvelope, ApiPage, ReviewItem } from "./types";

export function listReviews(params: { status?: string; page?: number; page_size?: number } = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
  return apiRequest<ApiPage<ReviewItem>>(`/reviews?${query.toString()}`);
}

export function acknowledgeReview(versionId: number, payload: { allow_publish: boolean; note: string }) {
  return apiRequest<ApiEnvelope<{ version_id: number; status: string }>>(`/reviews/${versionId}/acknowledge`, { method: "POST", body: payload });
}

export function publishVersion(versionId: number, payload: { reason?: string; confirm_warnings?: boolean } = {}) {
  return apiRequest<ApiEnvelope<{ version_id: number; analysis_run_id: number | null }>>(`/valuations/${versionId}/publish`, { method: "POST", body: payload });
}

export function batchPublish(reason: string) {
  return apiRequest<ApiEnvelope<{ requested: number; published: number; failed: Array<{ version_id: number; error: string }>; ignored_findings: number }>>("/reviews/batch-publish", { method: "POST", body: { reason } });
}

export function rejectVersion(versionId: number, reason: string) {
  return apiRequest<ApiEnvelope<{ version_id: number; status: string }>>(`/valuations/${versionId}/reject`, { method: "POST", body: { reason } });
}

export function revokeVersion(versionId: number, reason: string) {
  return apiRequest<ApiEnvelope<{ version_id: number; status: string }>>(`/valuations/${versionId}/revoke`, { method: "POST", body: { reason } });
}

export function restoreVersion(versionId: number, reason: string) {
  return apiRequest<ApiEnvelope<{ version_id: number; status: string }>>(`/valuations/${versionId}/restore`, { method: "POST", body: { reason } });
}
