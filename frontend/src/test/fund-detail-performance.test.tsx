import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { FundDetail as FundDetailData } from "@/api/types";
import { useAuth } from "@/app/auth";
import { useAsyncData } from "@/components";
import FundDetail from "@/pages/FundDetail";

vi.mock("@/app/auth", () => ({ useAuth: vi.fn() }));
vi.mock("@/components", async () => ({
  ...await vi.importActual<typeof import("@/components")>("@/components"),
  useAsyncData: vi.fn(),
  useInterval: vi.fn(),
}));
vi.mock("@/pages/FundDetail/tabs/NavSeries", () => ({
  default: ({ performance }: { performance?: unknown }) => (
    <>{performance ? <section aria-label="收益与回撤表" /> : null}</>
  ),
}));
vi.mock("@/pages/FundDetail/tabs/Positions", () => ({
  PositionsSection: () => null,
}));
vi.mock("@/pages/FundDetail/tabs/Quality", () => ({ QualitySection: () => null }));

const fund: FundDetailData = {
  id: 1, name: "测试产品", product_code: null, strategy: null, manager: null,
  establishment_date: "2024-01-01", notes: null, aliases: [], share_classes: [],
  status: "active", current_version_id: 1, valuation_date: "2026-09-14",
  quality_status: "valid", analysis_status: "ready", analysis_run_id: 1,
  total_assets: "120000000", net_asset_value: "110000000", unit_nav: "1.1",
  cumulative_unit_nav: "1.1", daily_return: "0.01", cumulative_return: "0.10",
  inception_drawdown: null,
  period_returns: { daily: "0.01", wtd: null, mtd: null, qtd: null, ytd: null, cumulative: "0.10" },
};

function renderRole(role: "operator" | "admin" | "viewer") {
  vi.mocked(useAuth).mockReturnValue({ session: { role } } as ReturnType<typeof useAuth>);
  vi.mocked(useAsyncData).mockReturnValue({
    data: fund, error: null, loading: false, refreshing: false,
    reload: vi.fn(), setData: vi.fn(), clearError: vi.fn(),
  });
  return render(
    <MemoryRouter initialEntries={["/funds/1"]}>
      <Routes><Route path="/funds/:id" element={<FundDetail />} /></Routes>
    </MemoryRouter>,
  );
}

describe("fund detail report visibility", () => {
  it.each(["operator", "admin", "viewer"] as const)("shows performance for %s", (role) => {
    renderRole(role);
    expect(screen.getByRole("link", { name: "收益与回撤" })).toHaveAttribute("href", "#performance");
    expect(screen.getByRole("region", { name: "收益与回撤表" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "持仓" })).not.toBeInTheDocument();
  });
});
