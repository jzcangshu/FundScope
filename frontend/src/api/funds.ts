import { apiRequest } from "./client";
import type { ApiEnvelope, ApiPage, FundDetail, FundListItem, NavSeries, Position } from "./types";

export interface FundListParams { q?: string; status?: string; as_of?: string; page?: number; page_size?: number; }
export interface PositionParams { as_of?: string; page?: number; page_size?: number; }

export function list(params: FundListParams = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
  return apiRequest<ApiPage<FundListItem>>(`/funds?${query.toString()}`);
}
export function detail(id: number, asOf?: string) {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return apiRequest<ApiEnvelope<FundDetail>>(`/funds/${id}${query}`);
}
export function navSeries(id: number, start?: string, end?: string) {
  const query = new URLSearchParams();
  if (start) query.set("start", start); if (end) query.set("end", end);
  return apiRequest<ApiEnvelope<NavSeries>>(`/funds/${id}/nav-series?${query.toString()}`);
}
export function positions(id: number, params: PositionParams = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined) query.set(key, String(value)); });
  return apiRequest<ApiPage<Position>>(`/funds/${id}/positions?${query.toString()}`);
}
export function quality(id: number, asOf?: string) {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return apiRequest<ApiEnvelope<import("./types").FundQuality>>(`/funds/${id}/quality${query}`);
}
