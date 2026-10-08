import { test, expect, type Route } from "@playwright/test";

/** Business data always requires live authorization, including in the desktop shell. */

const ADMIN_SESSION = {
  id: 1,
  username: "admin",
  display_name: "管理员",
  role: "admin",
  fund_access_mode: "all",
  fund_ids: [],
  status: "active",
  last_login_at: null,
  navigation: [
    "dashboard",
    "risk",
    "funds",
    "imports",
    "reviews",
    "mail",
    "adminFunds",
    "adminSubjects",
    "riskConfig",
    "adminUsers",
    "adminAudit",
    "adminSettings",
    "adminRetention",
  ],
};

const OVERVIEW_PAYLOAD = {
  data: {
    as_of: "2026-09-06",
    total_net_assets: "1000000.00",
    fund_count: 1,
    company_index: "1000.00",
    company_daily_return: "0.0010",
    risk_event_count: 0,
    quality_status: "ok",
    funds: [
      {
        id: 1,
        name: "测试基金",
        valuation_date: "2026-09-06",
        total_assets: "1000000.00",
        net_asset_value: "1.0000",
        unit_nav: "1.0000",
        daily_return: "0.0010",
        analysis_status: "fresh",
        analysis_run_id: 1,
      },
    ],
  },
  meta: {
    as_of: "2026-09-06",
    coverage: { available: 1, total: 1 },
    analysis_status: "fresh",
    analysis_run_id: 1,
  },
};

const SERIES_PAYLOAD = {
  data: { methodology: "weighted", points: [] },
  meta: { max_drawdown: null, current_drawdown: null },
};

const FUNDS_LIST_PAYLOAD = {
  data: [
    {
      id: 1,
      name: "测试基金",
      short_name: "测试",
      status: "active",
      valuation_date: "2026-09-06",
      unit_nav: "1.0000",
      accumulated_unit_nav: "1.0000",
      daily_return: "0.0010",
      quality_status: "ok",
    },
  ],
  meta: { page: 1, page_size: 100, total: 1 },
};

/** Request counter verifies each page load is authorized by the server. */
interface HitCounter {
  overview: { value: number };
}

async function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    headers: { "content-type": "application/json" },
    json: body,
  });
}

/** Authentication and remaining dashboard endpoints. */
async function stubAuthAndMisc(route: Route) {
  const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
  if (path === "/auth/status") return fulfillJson(route, { data: { initialized: true } });
  if (path === "/auth/me") return fulfillJson(route, { data: ADMIN_SESSION });
  if (path === "/dashboard/series") return fulfillJson(route, SERIES_PAYLOAD);
  if (path === "/funds") return fulfillJson(route, FUNDS_LIST_PAYLOAD);
  // 其它接口返回空 envelope，避免页面因缺字段抛错。
  return fulfillJson(route, { data: {}, meta: {} });
}

test.describe("桌面客户端在线授权读取", () => {
  test.beforeEach(async ({ context }) => {
    await context.clearCookies();
    await context.clearPermissions();
  });

  test("每次访问都重新请求并校验当前权限", async ({ page, context }) => {
    const hits: HitCounter = { overview: { value: 0 } };

    await context.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
      if (path === "/dashboard/overview") {
        hits.overview.value += 1;
        return fulfillJson(route, OVERVIEW_PAYLOAD);
      }
      return stubAuthAndMisc(route);
    });

    await page.goto("/dashboard");
    await expect(page.getByText("测试基金")).toBeVisible();
    expect(hits.overview.value).toBe(1);

    // Even within the former cache TTL, a reload must obtain fresh authorization.
    await page.reload();
    await expect(page.getByText("测试基金")).toBeVisible();
    expect(hits.overview.value).toBe(2);
  });

  test("顶栏显示在线读取状态并允许刷新", async ({ page, context }) => {
    await context.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
      if (path === "/dashboard/overview") return fulfillJson(route, OVERVIEW_PAYLOAD);
      return stubAuthAndMisc(route);
    });

    await page.goto("/dashboard");
    await expect(page.getByText("测试基金")).toBeVisible();

    // The online status and manual refresh remain available.
    const refreshButton = page.getByRole("button", { name: "刷新数据" });
    await expect(refreshButton).toBeVisible();
    await expect(page.locator(".fd-cache-status__indicator").last()).toBeVisible();
  });
});
