import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Form } from "antd";
import * as fundsApi from "@/api/funds";
import * as authApi from "@/api/auth";
import { FundAccessFields, loadFundAccessOptions, normalizeFundAccess } from "@/components/FundAccessFields";
import type { FundListItem } from "@/api/types";

vi.mock("@/api/funds", () => ({ list: vi.fn() }));
vi.mock("@/api/auth", () => ({ listPermissionFunds: vi.fn() }));

beforeAll(() => {
  window.matchMedia ??= ((query: string) => ({ matches: false, media: query, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }) as unknown as MediaQueryList);
});

function fund(id: number, status: "active" | "inactive" = "active") {
  return { id, name: `产品 ${id}`, product_code: `P${id}`, status } as FundListItem;
}

function Fields({ role = "viewer", mode = "all", ids = [] }: { role?: "viewer" | "operator" | "admin"; mode?: "all" | "selected"; ids?: number[] }) {
  return <Form initialValues={{ fund_access_mode: mode, fund_ids: ids }}><FundAccessFields role={role} /></Form>;
}

describe("account product scope", () => {
  it("defaults new accounts to all products and always normalizes administrators to all", () => {
    expect(normalizeFundAccess("viewer", {})).toEqual({ fund_access_mode: "all", fund_ids: [] });
    expect(normalizeFundAccess("operator", {})).toEqual({ fund_access_mode: "all", fund_ids: [] });
    expect(normalizeFundAccess("admin", { fund_access_mode: "selected", fund_ids: [9] })).toEqual({ fund_access_mode: "all", fund_ids: [] });
    expect(normalizeFundAccess("viewer", { fund_access_mode: "selected", fund_ids: [] })).toEqual({ fund_access_mode: "selected", fund_ids: [] });
  });

  it("loads every product page without filtering inactive products", async () => {
    vi.mocked(fundsApi.list).mockResolvedValueOnce({ data: [fund(1)], meta: { total: 2 } })
      .mockResolvedValueOnce({ data: [fund(2, "inactive")], meta: { total: 2 } });
    await expect(loadFundAccessOptions()).resolves.toEqual([fund(1), fund(2, "inactive")]);
    expect(fundsApi.list).toHaveBeenNthCalledWith(1, { page: 1, page_size: 100 });
    expect(fundsApi.list).toHaveBeenNthCalledWith(2, { page: 2, page_size: 100 });
  });

  it("does not silently accept a truncated page sequence", async () => {
    vi.mocked(fundsApi.list).mockResolvedValue({ data: [], meta: { total: 2 } });
    await expect(loadFundAccessOptions()).rejects.toThrow("产品列表不完整");
  });

  it("uses the permission catalog to load all grant options for a scoped boss", async () => {
    vi.mocked(authApi.listPermissionFunds).mockResolvedValueOnce({ data: [fund(1)], meta: { total: 2 } })
      .mockResolvedValueOnce({ data: [fund(2, "inactive")], meta: { total: 2 } });
    await expect(loadFundAccessOptions("permissions")).resolves.toEqual([fund(1), fund(2, "inactive")]);
    expect(authApi.listPermissionFunds).toHaveBeenNthCalledWith(1, { page: 1, page_size: 100 });
    expect(authApi.listPermissionFunds).toHaveBeenNthCalledWith(2, { page: 2, page_size: 100 });
  });

  it("shows administrators an explanation without an editable scope", () => {
    render(<Fields role="admin" />);
    expect(screen.getByText("管理员始终可访问全部产品，无需配置。")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("explains an empty scope and offers searchable active and inactive products", async () => {
    vi.mocked(fundsApi.list).mockResolvedValue({ data: [fund(1), fund(2, "inactive")], meta: { total: 2 } });
    render(<Fields role="operator" mode="selected" />);
    expect(await screen.findByText("未选择任何产品：该账号登录后无法查看或操作任何产品。")).toBeInTheDocument();
    await waitFor(() => expect(fundsApi.list).toHaveBeenCalled());
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "选择可见产品" }));
    expect(await screen.findByText("产品 2 · P2（已停用）")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "选择可见产品" }), { target: { value: "P2" } });
    expect(screen.queryByText("产品 1 · P1")).not.toBeInTheDocument();
  });

  it("preserves existing selected IDs and presents errors with retry", async () => {
    vi.mocked(fundsApi.list).mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ data: [fund(7)], meta: { total: 1 } });
    render(<Fields mode="selected" ids={[7]} />);
    expect(await screen.findByText("产品列表加载失败")).toBeInTheDocument();
    expect(screen.getByText(/已选择 1 只产品/)).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "选择可见产品" }));
    expect(await screen.findByText("产品 #7")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(screen.getAllByText("产品 7 · P7").length).toBeGreaterThan(0));
    expect(screen.getByText(/已选择 1 只产品/)).toBeInTheDocument();
  });
});
