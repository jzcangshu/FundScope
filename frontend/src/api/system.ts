import { apiRequest } from "./client";
import type { ApiEnvelope, ApiPage, AuditLog, MaintenanceResult, OperationalSummary, SystemSettings } from "./types";

export function getSettings() { return apiRequest<ApiEnvelope<SystemSettings>>("/system/settings"); }
export function updateSettings(settings: Record<string, unknown>) { return apiRequest<ApiEnvelope<SystemSettings>>("/system/settings", { method: "PATCH", body: settings }); }
export function getOperations() { return apiRequest<ApiEnvelope<OperationalSummary>>("/system/operations"); }
export function previewRetention() { return apiRequest<ApiEnvelope<MaintenanceResult>>("/system/retention/preview", { method: "POST" }); }
export function executeRetention(reason: string) { return apiRequest<ApiEnvelope<MaintenanceResult>>("/system/retention/execute", { method: "POST", body: { confirmation: "DELETE_EXPIRED_SOURCE_FILES", reason } }); }
export interface AuditParams { actor_user_id?: number; action?: string; resource_type?: string; result?: string; page?: number; page_size?: number; }
export function listAuditLogs(params: AuditParams = {}) { const query = new URLSearchParams(); Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); }); return apiRequest<ApiPage<AuditLog>>(`/audit-logs?${query.toString()}`); }
