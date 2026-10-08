import { expect, test, type Page } from "@playwright/test";

/**
 * Local-only layout check for the drawdown chart.
 *
 * Requires the dev stack: backend on 127.0.0.1:8000 (sqlite + app.dev_seed,
 * admin devadmin/devpass1234) and vite on 127.0.0.1:5173. Run it with
 * `npm run test:layout`; the default e2e suite ignores this file (it serves a
 * static preview with no backend, so it cannot log in).
 */

const BASE = process.env.LAYOUT_BASE ?? "http://127.0.0.1:5173";

async function login(page: Page) {
  await page.setViewportSize({ width: 1600, height: 1100 });
  await page.goto(`${BASE}/`);
  await page.getByPlaceholder("账号").fill("devadmin");
  await page.getByPlaceholder("密码").fill("devpass1234");
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/dashboard|\/funds/, { timeout: 15000 });
}

/** Percentages printed on the drawdown chart's own axis. */
async function drawdownAxisPercents(page: Page): Promise<number[]> {
  const chart = page.getByRole("img", { name: /区间回撤走势/ }).first();
  await expect(chart).toBeVisible();
  const text = await chart.innerText();
  return [...text.matchAll(/(-?\d+(?:\.\d+)?)%/g)].map((match) => Number.parseFloat(match[1]));
}

/**
 * The range selector belongs to the left column, next to the NAV chart: the
 * head must be exactly as wide as the left column and the tabs must end at that
 * column's right edge. On the company overview the head is the first child of
 * `.fd-split`, so this pins both pages to the same geometry.
 */
async function expectRangeSelectorInLeftColumn(page: Page, sectionSelector: string) {
  const section = page.locator(sectionSelector).first();
  const head = await section.locator(".fd-section__head").first().boundingBox();
  const left = await section.locator(".fd-split > *").first().boundingBox();
  const tabs = await section.locator('[role="radiogroup"]').first().boundingBox();
  expect(head && left && tabs).toBeTruthy();

  // Same origin and same width as the left column.
  expect(Math.abs(head!.x - left!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(head!.width - left!.width)).toBeLessThanOrEqual(1);
  // The tabs sit inside that column, not over the drawdown column.
  expect(tabs!.x + tabs!.width).toBeLessThanOrEqual(left!.x + left!.width + 2);
  expect(tabs!.x).toBeGreaterThan(left!.x + left!.width / 2);
}

/**
 * Both charts in a row must end on the same line, so the two date axes read as
 * one baseline. The columns have different natural heights (the drawdown column
 * carries a summary), so this only holds while the charts flex to fill.
 *
 * The second half of the invariant matters as much as the first: nothing may
 * render below the date row inside a chart. The NAV chart gains a legend as
 * soon as it shows adjusted NAV or the CSI 300 overlay, and a bottom-anchored
 * legend would push its date row ~60px up while the drawdown chart's stays put.
 */
async function expectDateRowsAligned(page: Page, sectionSelector: string) {
  const columns = page.locator(sectionSelector).first().locator(".fd-split > *");
  const rows: { top: number; bottom: number; gapBelow: number }[] = [];
  for (let index = 0; index < 2; index += 1) {
    const column = columns.nth(index);
    const ticks = column.locator(".recharts-xAxis .recharts-cartesian-axis-tick-value");
    const boxes: { y: number; height: number }[] = [];
    const count = await ticks.count();
    for (let tick = 0; tick < count; tick += 1) {
      const box = await ticks.nth(tick).boundingBox();
      if (box) boxes.push(box);
    }
    expect(boxes.length).toBeGreaterThan(0);
    const chart = await column.locator(".fd-chart").first().boundingBox();
    expect(chart).toBeTruthy();
    const bottom = Math.max(...boxes.map((box) => box.y + box.height));
    rows.push({
      top: Math.min(...boxes.map((box) => box.y)),
      bottom,
      gapBelow: chart!.y + chart!.height - bottom,
    });
  }
  expect(Math.abs(rows[0].top - rows[1].top)).toBeLessThanOrEqual(2);
  expect(Math.abs(rows[0].bottom - rows[1].bottom)).toBeLessThanOrEqual(2);
  for (const row of rows) {
    // Only the chart's own bottom margin may sit below the labels.
    expect(row.gapBelow).toBeLessThanOrEqual(10);
  }
  expect(Math.abs(rows[0].gapBelow - rows[1].gapBelow)).toBeLessThanOrEqual(2);
}

test("product page shows the drawdown chart beside the NAV chart", async ({ page }) => {
  await login(page);
  await page.goto(`${BASE}/funds`);
  const firstFund = page.locator("a.fd-table-name").first();
  await expect(firstFund).toBeVisible();
  await firstFund.click();
  await expect(page.locator("#nav")).toBeVisible();
  await page.waitForTimeout(2500);

  // The NAV chart and the drawdown column share one row inside #nav.
  const navSection = page.locator("#nav");
  await expect(navSection.locator(".fd-split > *")).toHaveCount(2);
  await expect(navSection.getByRole("img", { name: /净值/ })).toBeVisible();
  await expect(navSection.getByRole("img", { name: /区间回撤走势/ })).toBeVisible();
  await expect(navSection.getByText("区间最大回撤")).toBeVisible();
  await expect(navSection.getByText("区间当前回撤")).toBeVisible();

  // No separate drawdown section and no extra anchor entry.
  await expect(page.locator("#drawdown")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "回撤走势" })).toHaveCount(0);

  const navBox = await navSection.locator(".fd-split > *").first().boundingBox();
  const drawdownBox = await navSection.locator(".fd-split > *").nth(1).boundingBox();
  expect(navBox && drawdownBox && Math.abs(navBox.y - drawdownBox.y) < 40).toBe(true);
  expect(drawdownBox!.x).toBeGreaterThan(navBox!.x + navBox!.width - 1);

  await expectRangeSelectorInLeftColumn(page, "#nav");
  await expectDateRowsAligned(page, "#nav");

  // A window that never falls must not scale the axis into positive percents.
  const percents = await drawdownAxisPercents(page);
  expect(percents.length).toBeGreaterThan(0);
  expect(Math.max(...percents)).toBeLessThanOrEqual(0);

  await page.screenshot({ path: "tests/e2e/artifacts/fund-detail-layout.png", fullPage: false });
});

test("overview page keeps the same side-by-side shape", async ({ page }) => {
  await login(page);
  await page.goto(`${BASE}/dashboard`);
  await expect(page.getByText("资产净值走势")).toBeVisible();
  await page.waitForTimeout(2500);

  // The drawdown column sits beside the NAV chart here too — the product page
  // now mirrors this shape rather than introducing a section of its own.
  const navBox = await page.locator(".fd-split").first().locator("> *").first().boundingBox();
  const drawdownBox = await page.locator(".fd-split").first().locator("> *").nth(1).boundingBox();
  expect(navBox && drawdownBox && Math.abs(navBox.y - drawdownBox.y) < 40).toBe(true);
  expect(drawdownBox!.x).toBeGreaterThan(navBox!.x + navBox!.width - 1);

  await expectRangeSelectorInLeftColumn(page, 'section[aria-label="走势"]');
  await expectDateRowsAligned(page, 'section[aria-label="走势"]');

  const percents = await drawdownAxisPercents(page);
  expect(percents.length).toBeGreaterThan(0);
  expect(Math.max(...percents)).toBeLessThanOrEqual(0);

  await page.screenshot({ path: "tests/e2e/artifacts/dashboard-layout.png", fullPage: false });
});
