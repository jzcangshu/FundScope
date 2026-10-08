import { apiRequest } from "./client";
import type { ApiEnvelope, MailMessage, MailSettings, MailSyncResult, MailSyncSchedule } from "./types";

export function getSettings() { return apiRequest<ApiEnvelope<MailSettings>>("/mail/settings"); }
export function updateSettings(username: string) {
  return apiRequest<ApiEnvelope<MailSettings>>("/mail/settings", { method: "PUT", body: { username } });
}
export function updateCredential(authorization_code: string) {
  return apiRequest<ApiEnvelope<{ configured: boolean; credential_source: string; credential_writable: boolean }>>("/mail/credential", { method: "PUT", body: { authorization_code } });
}
export function testConnection() { return apiRequest<ApiEnvelope<{ connected: boolean }>>("/mail/test-connection", { method: "POST" }); }
export function syncNow() { return apiRequest<ApiEnvelope<MailSyncResult>>("/mail/sync", { method: "POST", headers: { "x-async-sync": "1" } }); }
export function cancelSync(runId: string) { return apiRequest<ApiEnvelope<{ run_id: string; status: string }>>(`/mail/sync/${encodeURIComponent(runId)}/cancel`, { method: "POST" }); }
export function pause() { return apiRequest<ApiEnvelope<{ auto_sync_enabled: boolean }>>("/mail/pause", { method: "POST" }); }
export function resume() { return apiRequest<ApiEnvelope<{ auto_sync_enabled: boolean }>>("/mail/resume", { method: "POST" }); }
export function listSyncRuns() { return apiRequest<ApiEnvelope<MailSyncResult[]>>("/mail/sync-runs"); }
export function updateSchedule(schedule: MailSyncSchedule) {
  return apiRequest<ApiEnvelope<MailSettings>>("/mail/schedule", { method: "PUT", body: schedule });
}
export function listMessages() { return apiRequest<ApiEnvelope<MailMessage[]>>("/mail/messages"); }
