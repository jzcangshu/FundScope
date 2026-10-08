import { test, expect, type Page } from "@playwright/test";

const admin = { id: 1, username: "admin", display_name: "管理员", role: "admin", status: "active", last_login_at: null, navigation: [], fund_access_mode: "all", fund_ids: [] };
const viewer = { ...admin, id: 2, username: "viewer", display_name: "看板用户", role: "viewer", fund_access_mode: "selected", fund_ids: [1] };
const products = Array.from({ length: 101 }, (_, i) => ({ id: i + 1, name: `产品${i + 1}`, product_code: `P${i + 1}`, status: i === 100 ? "inactive" : "active" }));

async function setup(page: Page) {
  const state = { viewer: { ...viewer }, created: [] as Record<string, unknown>[], changed: [] as Record<string, unknown>[], failChange: false, pages: [] as number[] };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    let body: unknown;
    if (path === "/auth/status") body = { data: { initialized: true } };
    else if (path === "/auth/me") body = { data: admin };
    else if (path === "/users" && request.method() === "POST") {
      state.created.push(request.postDataJSON());
      body = { data: { ...admin, ...request.postDataJSON() } };
    } else if (path === "/users") body = { data: [admin, state.viewer], meta: { total: 2, page: 1, page_size: 20 } };
    else if (path === "/funds") {
      const pageNumber = Number(url.searchParams.get("page") ?? 1);
      state.pages.push(pageNumber);
      body = { data: products.slice((pageNumber - 1) * 100, pageNumber * 100), meta: { total: products.length, page: pageNumber, page_size: 100 } };
    } else if (path === "/users/2/fund-access") {
      state.changed.push(request.postDataJSON());
      if (state.failChange) return route.fulfill({ status: 403, json: { detail: "Forbidden" } });
      state.viewer = { ...state.viewer, ...request.postDataJSON() };
      body = { data: state.viewer };
    } else body = { data: {}, meta: {} };
    await route.fulfill({ status: 200, json: body });
  });
  await page.goto("/admin/users");
  await expect(page.getByText("全部产品 · 无需配置")).toBeVisible();
  return state;
}

test("create defaults to all and supports selecting an inactive product beyond the first page", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "新增账号" }).click();
  const dialog = page.getByRole("dialog", { name: "新增账号" });
  await expect(dialog.getByText("全部产品（包括以后新增的产品）")).toBeVisible();
  await dialog.getByLabel("账号", { exact: true }).fill("sales-person");
  await dialog.getByLabel("初始密码").fill("demo-password-2026");
  await dialog.getByText("全部产品（包括以后新增的产品）", { exact: true }).click();
  await page.locator(".ant-select-item-option-content").getByText("指定产品", { exact: true }).click();
  await expect.poll(() => state.pages).toEqual([1, 2]);
  await dialog.getByRole("combobox", { name: "选择可见产品" }).focus();
  await dialog.getByRole("combobox", { name: "选择可见产品" }).fill("P101");
  await page.locator(".ant-select-item-option-content").getByText("产品101 · P101（已停用）", { exact: true }).click();
  await dialog.getByRole("button", { name: /创\s*建/ }).click();
  await expect.poll(() => state.created.length).toBe(1);
  expect(state.created[0]).toMatchObject({ username: "sales-person", role: "viewer", fund_access_mode: "selected", fund_ids: [101] });
  await expect(dialog).not.toBeVisible();
});

test("edits existing scope, requires a reason, and retains the form after a server error", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "指定 1 只产品 · 修改" }).click();
  const dialog = page.getByRole("dialog", { name: "可见产品：viewer" });
  await expect(dialog.getByText(/已选择 1 只产品/)).toBeVisible();
  await dialog.getByRole("button", { name: "清空选择" }).click();
  await dialog.getByRole("button", { name: "保存产品范围" }).click();
  await expect(dialog.getByText("请填写修改原因")).toBeVisible();
  expect(state.changed).toHaveLength(0);
  await dialog.getByLabel("修改原因").fill("  转交产品负责人  ");
  state.failChange = true;
  await dialog.getByRole("button", { name: "保存产品范围" }).click();
  await expect(dialog.getByText("当前账号没有执行该操作的权限")).toBeVisible();
  await expect(dialog.getByLabel("修改原因")).toHaveValue("  转交产品负责人  ");
  state.failChange = false;
  await dialog.getByRole("button", { name: "保存产品范围" }).click();
  await expect(dialog).not.toBeVisible();
  expect(state.changed.at(-1)).toEqual({ fund_access_mode: "selected", fund_ids: [], reason: "转交产品负责人" });
  await expect(page.getByRole("button", { name: "指定 0 只产品 · 修改" })).toBeVisible();
});

test("administrators have no product scope editor", async ({ page }) => {
  await setup(page);
  const row = page.getByRole("row").filter({ has: page.getByText("admin", { exact: true }) });
  await expect(row.getByRole("button", { name: /修改/ })).toHaveCount(0);
  await page.getByRole("button", { name: "新增账号" }).click();
  const dialog = page.getByRole("dialog", { name: "新增账号" });
  await dialog.getByText("普通看板", { exact: true }).click();
  await page.locator(".ant-select-item-option-content").getByText("系统管理员", { exact: true }).click();
  await expect(dialog.getByText("管理员始终可访问全部产品，无需配置。")).toBeVisible();
  await expect(dialog.getByRole("combobox", { name: "可见产品范围" })).toHaveCount(0);
});

test("administrators can create a boss account", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "新增账号" }).click();
  const dialog = page.getByRole("dialog", { name: "新增账号" });
  await dialog.getByLabel("账号", { exact: true }).fill("boss");
  await dialog.getByLabel("初始密码").fill("demo-password-2026");
  await dialog.getByText("普通看板", { exact: true }).click();
  await page.locator(".ant-select-item-option-content").getByText("老板", { exact: true }).click();
  await expect(dialog.getByText("全部产品（包括以后新增的产品）")).toBeVisible();
  await dialog.getByRole("button", { name: /创\s*建/ }).click();
  await expect.poll(() => state.created.length).toBe(1);
  expect(state.created[0]).toMatchObject({ username: "boss", role: "boss", fund_access_mode: "all", fund_ids: [] });
});

test("an existing signed-in view drops old products when grants change", async ({ page }) => {
  let revoked = false;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const current = { ...viewer, fund_access_mode: revoked ? "selected" : "all", fund_ids: [] };
    let body: unknown = { data: {}, meta: {} };
    if (path === "/auth/status") body = { data: { initialized: true } };
    if (path === "/auth/me") body = { data: current };
    if (path === "/funds") body = { data: revoked ? [] : [{ ...products[0], quality_status: "valid", analysis_status: "ready", unit_nav: null, daily_return: null, valuation_date: null }], meta: { total: revoked ? 0 : 1 } };
    await route.fulfill({ status: 200, json: body });
  });
  await page.goto("/funds");
  await expect(page.getByRole("link", { name: "产品1", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "风险规则", exact: true })).toBeVisible();
  revoked = true;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByRole("link", { name: "产品1", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "风险规则", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "选定产品总览", exact: true })).toBeVisible();
  await expect(page.getByText("尚未分配产品", { exact: true })).toBeVisible();
  await page.goto("/admin/risk-rules");
  await expect(page.getByText("当前账号无权访问此页面")).toBeVisible();
});

test("scoped operators retain product maintenance but cannot create products or access global pages", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let body: unknown = { data: [], meta: { total: 0 } };
    if (path === "/auth/status") body = { data: { initialized: true } };
    if (path === "/auth/me") body = { data: { ...viewer, role: "operator", fund_ids: [] } };
    await route.fulfill({ status: 200, json: body });
  });
  await page.goto("/admin/funds");
  await expect(page.getByRole("heading", { name: "产品管理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "新增产品" })).toHaveCount(0);
  for (const name of ["导入中心", "异常复核"]) await expect(page.getByRole("link", { name, exact: true })).toBeVisible();
  for (const name of ["邮件接入", "科目映射", "风险规则"]) await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
  await page.goto("/mail");
  await expect(page.getByText("当前账号无权访问此页面")).toBeVisible();
});
