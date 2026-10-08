import { test, expect, type Page } from "@playwright/test";

const boss = { id: 2, username: "bob_e", display_name: "负责人", role: "boss", status: "active", last_login_at: null, navigation: [], fund_access_mode: "selected", fund_ids: [1] };
const products = [{ id: 7, name: "示例产品一号", product_code: "P007", status: "inactive" }];
const grantOptions = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, name: `候选产品${index + 1}`, product_code: `P${index + 1}`, status: index === 100 ? "inactive" : "active" }));
const accounts = [
  { ...boss, id: 1, username: "carol_admin", display_name: "管理员", role: "admin", fund_access_mode: "all", fund_ids: [], funds: [] },
  { ...boss, funds: [{ ...products[0], id: 1, name: "老板获授权产品", status: "active" }] },
  { ...boss, id: 3, username: "amy_ops", display_name: "产品负责人", role: "operator", fund_ids: [7], funds: products },
  ...Array.from({ length: 19 }, (_, index) => ({ ...boss, id: index + 4, username: `viewer${index}`, display_name: `看板用户${index}`, role: "viewer", fund_ids: [], funds: [] })),
  { ...boss, id: 23, username: "archived_viewer", display_name: "已停用员工", role: "viewer", status: "disabled", fund_ids: [], funds: [] },
];

async function setup(page: Page, role = "boss") {
  const state = { role, failed: false, failChange: false, failOptions: false, requests: [] as string[], writes: [] as string[], rows: [...accounts], changes: [] as Record<string, unknown>[], catalogPages: [] as number[] };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    if (request.method() !== "GET") state.writes.push(path);
    if (path === "/auth/status") return route.fulfill({ json: { data: { initialized: true } } });
    if (path === "/auth/me") return route.fulfill({ json: { data: { ...boss, role: state.role } } });
    if (path === "/users/permissions/funds") {
      if (state.failOptions) return route.fulfill({ status: 500, json: { detail: "catalog unavailable" } });
      const pageNumber = Number(url.searchParams.get("page") ?? 1);
      state.catalogPages.push(pageNumber);
      return route.fulfill({ json: { data: grantOptions.slice((pageNumber - 1) * 100, pageNumber * 100), meta: { total: grantOptions.length, page: pageNumber, page_size: 100 } } });
    }
    if (/^\/users\/\d+\/fund-access$/.test(path) && request.method() === "PATCH") {
      const payload = request.postDataJSON();
      state.changes.push(payload);
      if (state.failChange) return route.fulfill({ status: 403, json: { detail: "Forbidden" } });
      const targetId = Number(path.split("/")[2]);
      state.rows = state.rows.map((row) => row.id === targetId ? { ...row, ...payload, funds: payload.fund_access_mode === "selected" ? grantOptions.filter((fund) => payload.fund_ids.includes(fund.id)) : [] } : row);
      return route.fulfill({ json: { data: state.rows.find((row) => row.id === targetId) } });
    }
    if (path === "/users/permissions") {
      state.requests.push(url.search);
      if (state.failed) return route.fulfill({ status: 500, json: { detail: "读取失败，请重试" } });
      const q = url.searchParams.get("q") ?? "";
      const roleFilter = url.searchParams.get("role");
      const statusFilter = url.searchParams.get("status");
      const rows = state.rows.filter((row) => (state.role !== "boss" || row.status === "active") && (row.username.includes(q) || row.display_name.includes(q)) && (!roleFilter || row.role === roleFilter) && (!statusFilter || row.status === statusFilter));
      const pageNumber = Number(url.searchParams.get("page") ?? 1);
      return route.fulfill({ json: { data: rows.slice((pageNumber - 1) * 20, pageNumber * 20), meta: { total: rows.length, page: pageNumber, page_size: 20 } } });
    }
    return route.fulfill({ json: { data: [], meta: { total: 0 } } });
  });
  return state;
}

test("boss can inspect grants, filter, page and refresh without account administration controls", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  const state = await setup(page);
  await page.goto("/account-permissions");
  await expect(page.getByRole("heading", { name: "账号权限一览", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "账号权限一览", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "账号管理", exact: true })).toHaveCount(0);
  const operator = page.getByRole("row").filter({ has: page.getByText("amy_ops", { exact: true }) });
  await expect(operator).toContainText("示例产品一号");
  await expect(operator).toContainText("（已停用）");
  await expect(page.getByText("archived_viewer", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "状态", exact: true })).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: "功能权限", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /新增账号|重置密码|撤销会话/ })).toHaveCount(0);
  await expect(operator.getByRole("button", { name: "修改可见产品" })).toBeVisible();
  for (const username of ["carol_admin", "bob_e"]) {
    await expect(page.getByRole("row").filter({ has: page.getByText(username, { exact: true }) }).getByRole("button", { name: "修改可见产品" })).toHaveCount(0);
  }
  await expect(page.locator(".ant-spin-blur")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("boss-permissions-desktop.png"), animations: "disabled" });
  await page.locator(".ant-pagination-item-2").click();
  await expect.poll(() => state.requests.at(-1)).toContain("page=2");
  await expect(page.getByText("viewer18", { exact: true })).toBeVisible();
  await page.getByRole("searchbox", { name: "搜索账号" }).fill("产品负责人");
  await page.getByRole("searchbox", { name: "搜索账号" }).press("Enter");
  await expect(operator).toBeVisible();
  await expect.poll(() => state.requests.at(-1)).toContain("page=1");
  await expect(page.getByText("viewer18", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "清除筛选" }).click();
  await expect(page.getByRole("searchbox", { name: "搜索账号" })).toHaveValue("");
  await page.getByRole("combobox", { name: "角色", exact: true }).click();
  await page.locator(".ant-select-item-option-content").getByText("业务员", { exact: true }).click();
  await expect.poll(() => state.requests.at(-1)).toContain("role=operator");
  await expect(page.locator("tbody tr.ant-table-row")).toHaveCount(1);
  await page.getByRole("heading", { name: "账号权限一览", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath("boss-permissions-mobile.png"), fullPage: true, animations: "disabled" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  state.failed = true;
  await page.getByRole("button", { name: /刷新$/ }).click();
  await expect(page.getByRole("alert")).toContainText("账号权限加载失败");
  state.failed = false;
  state.rows = [];
  await page.getByRole("button", { name: /刷新$/ }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator("tbody tr.ant-table-row")).toHaveCount(0);
  expect(state.writes).toEqual([]);
});

test("admin can still filter and inspect disabled accounts", async ({ page }) => {
  const state = await setup(page, "admin");
  await page.goto("/account-permissions");
  await page.getByRole("combobox", { name: "状态", exact: true }).click();
  await page.locator(".ant-select-item-option-content").getByText("已禁用", { exact: true }).click();
  await expect.poll(() => state.requests.at(-1)).toContain("status=disabled");
  const row = page.getByRole("row").filter({ has: page.getByText("archived_viewer", { exact: true }) });
  await expect(row).toContainText("已禁用");
  await expect(page.locator("tbody tr.ant-table-row")).toHaveCount(1);
});

for (const role of ["viewer", "operator"]) {
  test(`${role} cannot access the permission overview by direct URL`, async ({ page }) => {
    const state = await setup(page, role);
    await page.goto("/account-permissions");
    await expect(page.getByText("当前账号无权访问此页面")).toBeVisible();
    await expect(page.getByRole("link", { name: "账号权限一览", exact: true })).toHaveCount(0);
    expect(state.requests).toEqual([]);
  });
}

test("boss cannot enter account management and loses the overview after a role change", async ({ page }) => {
  const state = await setup(page);
  await page.goto("/admin/users");
  await expect(page.getByText("当前账号无权访问此页面")).toBeVisible();
  await page.getByRole("link", { name: "账号权限一览", exact: true }).click();
  await expect(page.getByText("示例产品一号", { exact: false })).toBeVisible();
  await page.getByRole("row").filter({ has: page.getByText("amy_ops", { exact: true }) }).getByRole("button", { name: "修改可见产品" }).click();
  await expect(page.getByRole("dialog", { name: "可见产品：amy_ops" })).toBeVisible();
  state.role = "viewer";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByText("当前账号无权访问此页面")).toBeVisible();
  await expect(page.getByText("示例产品一号", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "账号权限一览", exact: true })).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("boss can assign, clear and grant all products with a reason and recover from errors", async ({ page }, testInfo) => {
  const state = await setup(page);
  await page.goto("/account-permissions");
  const row = page.getByRole("row").filter({ has: page.getByText("viewer0", { exact: true }) });
  await row.getByRole("button", { name: "修改可见产品" }).click();
  const dialog = page.getByRole("dialog", { name: "可见产品：viewer0" });
  await expect.poll(() => state.catalogPages).toEqual([1, 2]);
  await dialog.getByRole("combobox", { name: "选择可见产品" }).focus();
  await dialog.getByRole("combobox", { name: "选择可见产品" }).fill("P101");
  await page.locator(".ant-select-item-option-content").getByText("候选产品101 · P101（已停用）", { exact: true }).click();
  await dialog.getByText("保存后立即生效，并记录修改人、修改原因及前后产品范围。").click();
  await dialog.getByRole("button", { name: "保存观看权限" }).click();
  await expect(dialog.getByText("请填写修改原因")).toBeVisible();
  expect(state.changes).toEqual([]);
  await dialog.getByLabel("修改原因").fill("  调整观看产品  ");
  state.failChange = true;
  await dialog.getByRole("button", { name: "保存观看权限" }).click();
  await expect(dialog.getByText("当前账号没有执行该操作的权限")).toBeVisible();
  await expect(dialog.getByLabel("修改原因")).toHaveValue("  调整观看产品  ");
  await expect(dialog.getByText(/已选择 1 只产品/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("boss-edit-permissions.png"), animations: "disabled" });
  state.failChange = false;
  await dialog.getByRole("button", { name: "保存观看权限" }).click();
  await expect(dialog).not.toBeVisible();
  expect(state.changes.at(-1)).toEqual({ fund_access_mode: "selected", fund_ids: [101], reason: "调整观看产品" });
  await expect(row).toContainText("候选产品101");
  await row.getByRole("button", { name: "修改可见产品" }).click();
  await dialog.getByRole("button", { name: "清空选择" }).click();
  await dialog.getByLabel("修改原因").fill("收回全部观看权限");
  await dialog.getByRole("button", { name: "保存观看权限" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(row).toContainText("未分配产品");
  expect(state.changes.at(-1)).toMatchObject({ fund_access_mode: "selected", fund_ids: [] });
  await row.getByRole("button", { name: "修改可见产品" }).click();
  await dialog.getByText("指定产品", { exact: true }).click();
  await page.locator(".ant-select-item-option-content").getByText("全部产品（包括以后新增的产品）", { exact: true }).click();
  await dialog.getByLabel("修改原因").fill("恢复全部产品");
  await dialog.getByRole("button", { name: "保存观看权限" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(row).toContainText("全部产品");
  expect(state.changes.at(-1)).toEqual({ fund_access_mode: "all", fund_ids: [], reason: "恢复全部产品" });
  expect(state.writes.every((path) => path === "/users/4/fund-access")).toBe(true);
});

test("a failed product catalog can be retried without losing the account's grants", async ({ page }) => {
  const state = await setup(page);
  state.failOptions = true;
  await page.goto("/account-permissions");
  await page.getByRole("row").filter({ has: page.getByText("amy_ops", { exact: true }) }).getByRole("button", { name: "修改可见产品" }).click();
  const dialog = page.getByRole("dialog", { name: "可见产品：amy_ops" });
  await expect(dialog.getByRole("alert")).toContainText("产品列表加载失败");
  await expect(dialog.getByText(/已选择 1 只产品/)).toBeVisible();
  state.failOptions = false;
  await dialog.getByRole("button", { name: /重\s*试/ }).click();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await expect(dialog.getByText(/已选择 1 只产品/)).toBeVisible();
  await expect.poll(() => state.catalogPages).toEqual([1, 2]);
  expect(state.writes).toEqual([]);
});
