import { apiRequest } from "./client";
import type { DashboardFundDrawdownsResponse, DashboardOverviewResponse, DashboardSeriesResponse } from "./types";
import type { RangeKey } from "@/utils/constants";

export function getOverview(asOf?: string) {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return apiRequest<DashboardOverviewResponse>(`/dashboard/overview${query}`);
}

export function getSeries(params: { start?: string; end?: string } = {}) {
  const query = new URLSearchParams();
  if (params.start) query.set("start", params.start);
  if (params.end) query.set("end", params.end);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<DashboardSeriesResponse>(`/dashboard/series${suffix}`);
}

/** One batch request for every product row's interval drawdown. */
export function getFundDrawdowns(range: RangeKey, asOf?: string) {
  const query = new URLSearchParams({ range });
  if (asOf) query.set("as_of", asOf);
  return apiRequest<DashboardFundDrawdownsResponse>(`/dashboard/fund-drawdowns?${query.toString()}`);
}
