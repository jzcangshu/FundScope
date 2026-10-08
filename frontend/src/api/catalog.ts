import { apiRequest } from "./client";
import type { ApiEnvelope, ApiPage, Alias, CatalogFund, FundDetail, ShareClass, SubjectMapping } from "./types";

export interface SubjectMappingInput {
  subject_code_or_prefix?: string | null;
  raw_name_pattern?: string | null;
  standard_category: string;
  is_leaf: boolean;
  include_in_holdings: boolean;
  rule_version: string;
}

function queryString(params: Record<string, unknown>): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
  return query.toString();
}
export function listFunds(params: { q?: string; status?: string; page?: number; page_size?: number } = {}) { return apiRequest<ApiPage<CatalogFund>>(`/funds?${queryString(params)}`); }
export function getFund(id: number) { return apiRequest<ApiEnvelope<FundDetail>>(`/funds/${id}`); }
export function createFund(payload: Record<string, unknown>) { return apiRequest<ApiEnvelope<CatalogFund>>("/funds", { method: "POST", body: payload }); }
export function updateFund(id: number, payload: Record<string, unknown>) { return apiRequest<ApiEnvelope<CatalogFund>>(`/funds/${id}`, { method: "PATCH", body: payload }); }
export function enableFund(id: number) { return apiRequest<ApiEnvelope<CatalogFund>>(`/funds/${id}/enable`, { method: "POST" }); }
export function disableFund(id: number, reason: string) { return apiRequest<ApiEnvelope<CatalogFund>>(`/funds/${id}/disable`, { method: "POST", body: { reason } }); }

/** Row counts a deletion would destroy, keyed by table. Preview-only. */
export interface FundDeletionImpact {
  removed: Record<string, number>;
  total_removed: number;
  detached_analysis_runs: number;
  preserved_company_metrics: number;
}

export function fundDeletionImpact(id: number) {
  return apiRequest<ApiEnvelope<{ fund: CatalogFund } & FundDeletionImpact>>(`/funds/${id}/deletion-impact`);
}
export function deleteFund(id: number, reason: string) {
  return apiRequest<ApiEnvelope<{ id: number; deleted: boolean } & FundDeletionImpact>>(`/funds/${id}`, {
    method: "DELETE",
    body: { reason },
  });
}
export function listAliases(fundId: number) { return apiRequest<ApiPage<Alias>>(`/funds/${fundId}/aliases`); }
export function createAlias(fundId: number, payload: Record<string, unknown>) { return apiRequest<ApiEnvelope<Alias>>(`/funds/${fundId}/aliases`, { method: "POST", body: payload }); }
export function updateAlias(fundId: number, aliasId: number, payload: Record<string, unknown>) { return apiRequest<ApiEnvelope<Alias>>(`/funds/${fundId}/aliases/${aliasId}`, { method: "PATCH", body: payload }); }
export function deleteAlias(fundId: number, aliasId: number, reason?: string) {
  return apiRequest<ApiEnvelope<{ id: number; deleted: boolean }>>(`/funds/${fundId}/aliases/${aliasId}`, {
    method: "DELETE",
    body: reason ? { reason } : {},
  });
}
export function listShareClasses(fundId: number) { return apiRequest<ApiPage<ShareClass>>(`/funds/${fundId}/share-classes`); }
export function createShareClass(fundId: number, payload: Record<string, unknown>) { return apiRequest<ApiEnvelope<ShareClass>>(`/funds/${fundId}/share-classes`, { method: "POST", body: payload }); }
export function updateShareClass(fundId: number, id: number, payload: Record<string, unknown>) { return apiRequest<ApiEnvelope<ShareClass>>(`/funds/${fundId}/share-classes/${id}`, { method: "PATCH", body: payload }); }
export function disableShareClass(fundId: number, id: number, reason: string) { return apiRequest<ApiEnvelope<ShareClass>>(`/funds/${fundId}/share-classes/${id}/disable`, { method: "POST", body: { reason } }); }
export function enableShareClass(fundId: number, id: number) { return apiRequest<ApiEnvelope<ShareClass>>(`/funds/${fundId}/share-classes/${id}/enable`, { method: "POST" }); }
export function listMappings(params: { status?: string; category?: string; page?: number; page_size?: number } = {}) { return apiRequest<ApiPage<SubjectMapping>>(`/subjects/mappings?${queryString(params)}`); }
export function createMapping(payload: SubjectMappingInput) { return apiRequest<ApiEnvelope<SubjectMapping>>("/subjects/mappings", { method: "POST", body: payload }); }
export function updateMapping(id: number, payload: SubjectMappingInput) { return apiRequest<ApiEnvelope<SubjectMapping>>(`/subjects/mappings/${id}`, { method: "PATCH", body: payload }); }
export function disableMapping(id: number, reason?: string) { return apiRequest<ApiEnvelope<SubjectMapping>>(`/subjects/mappings/${id}/disable`, { method: "POST", body: reason ? { reason } : {} }); }
