import { fireEvent, render, screen } from "@testing-library/react";
import { useAsyncData } from "@/components";
import NavSeriesSection from "@/pages/FundDetail/tabs/NavSeries";

vi.mock("@/components", async () => ({
  ...await vi.importActual<typeof import("@/components")>("@/components"),
  useAsyncData: vi.fn(),
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/pages/FundDetail/tabs/Performance", () => ({ PerformanceSection: () => null }));
vi.mock("recharts", async () => ({
  ...await vi.importActual<typeof import("recharts")>("recharts"),
  ResponsiveContainer: () => null,
}));

beforeEach(() => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});
afterEach(() => vi.unstubAllGlobals());

it("shows the observed YTD curve and actual start instead of an unavailable panel", () => {
  const points = [
    ["2026-06-01", "1.00"], ["2026-07-01", "1.20"],
    ["2026-08-01", "0.90"], ["2026-09-01", "1.10"],
  ].map(([valuation_date, adjusted_nav]) => ({
    valuation_date, adjusted_nav, unit_nav: adjusted_nav,
    daily_return: null, cumulative_return: null,
  }));
  vi.mocked(useAsyncData).mockReturnValue({
    data: { points }, error: null, loading: false, refreshing: false,
    reload: vi.fn(), setData: vi.fn(), clearError: vi.fn(),
  });
  render(<NavSeriesSection
    fundId={1}
    valuationDate="2026-09-01"
    performance={{ valuationDate: "2026-09-01", cumulativeReturn: null, establishmentDate: null }}
  />);

  fireEvent.click(screen.getByRole("radio", { name: "今年" }));
  expect(screen.getByRole("img", { name: "区间回撤走势，今年" })).toBeInTheDocument();
  expect(screen.getByText(/自 2026-06-01 起计算已观测回撤，不代表完整区间/)).toBeInTheDocument();
  expect(screen.getAllByText("-25.00%").length).toBeGreaterThan(0);
  expect(screen.queryByText("历史不足，缺少区间起点估值")).not.toBeInTheDocument();
});
