import { render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { AuthProvider, useAuth } from "@/app/auth";
import * as authApi from "@/api/auth";
import { apiRequest, ApiError } from "@/api/client";

vi.mock("@/api/auth", () => ({
  authStatus: vi.fn(),
  login: vi.fn(),
  initialize: vi.fn(),
  me: vi.fn(),
  logout: vi.fn(),
  changePassword: vi.fn(),
}));

const admin = {
  id: 1,
  username: "admin",
  display_name: "系统管理员",
  role: "admin" as const,
  fund_access_mode: "all" as const,
  fund_ids: [],
  status: "active" as const,
  last_login_at: null,
  navigation: ["dashboard"],
};

function Probe() {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="loading">{String(auth.loading)}</span>
      <span data-testid="initialized">{String(auth.initialized)}</span>
      <span data-testid="scope">{auth.session?.fund_access_mode}:{auth.session?.fund_ids.join(",")}</span>
      <span data-testid="username">{auth.session?.username ?? "none"}</span>
      <button onClick={() => void auth.login("admin", "password123")}>login</button>
      <button onClick={() => void auth.logout()}>logout</button>
    </div>
  );
}

describe("AuthProvider", () => {
  it("stops at initialization status when the system has no users", async () => {
    vi.mocked(authApi.authStatus).mockResolvedValue({ initialized: false });

    render(<AuthProvider><Probe /></AuthProvider>);

    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("initialized")).toHaveTextContent("false");
    expect(screen.getByTestId("username")).toHaveTextContent("none");
    expect(authApi.me).not.toHaveBeenCalled();
  });

  it("loads the real session after initialization", async () => {
    vi.mocked(authApi.authStatus).mockResolvedValue({ initialized: true });
    vi.mocked(authApi.me).mockResolvedValue(admin);

    render(<AuthProvider><Probe /></AuthProvider>);

    await waitFor(() => expect(screen.getByTestId("username")).toHaveTextContent("admin"));
    expect(screen.getByTestId("initialized")).toHaveTextContent("true");
  });

  it("stores the session returned by login", async () => {
    vi.mocked(authApi.authStatus).mockResolvedValue({ initialized: true });
    vi.mocked(authApi.me).mockRejectedValue(new ApiError(401, "Unauthorized"));
    vi.mocked(authApi.login).mockResolvedValue(admin);

    render(<AuthProvider><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));

    await act(async () => {
      screen.getByRole("button", { name: "login" }).click();
    });

    expect(authApi.login).toHaveBeenCalledWith({ username: "admin", password: "password123" });
    expect(screen.getByTestId("username")).toHaveTextContent("admin");
  });

  it("does not clear a new session when an older request later returns 401", async () => {
    vi.mocked(authApi.authStatus).mockResolvedValue({ initialized: true });
    vi.mocked(authApi.me).mockRejectedValue(new ApiError(401, "Unauthorized"));
    vi.mocked(authApi.login).mockResolvedValue(admin);

    render(<AuthProvider><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));

    let resolveResponse: ((response: Response) => void) | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () => new Promise<Response>((resolve) => { resolveResponse = resolve; }),
    );
    const staleRequest = apiRequest("/protected").catch((error: unknown) => error);

    await act(async () => {
      screen.getByRole("button", { name: "login" }).click();
    });
    expect(screen.getByTestId("username")).toHaveTextContent("admin");

    await act(async () => {
      resolveResponse?.(
        new Response(JSON.stringify({ detail: "Unauthorized" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        }),
      );
      await staleRequest;
    });

    expect(screen.getByTestId("username")).toHaveTextContent("admin");
  });
  it("refreshes changed grants on focus and rejects responses from the prior scope", async () => {
    const viewer = { ...admin, role: "viewer" as const, username: "viewer" };
    vi.mocked(authApi.authStatus).mockResolvedValue({ initialized: true });
    vi.mocked(authApi.me).mockResolvedValue(viewer);
    render(<AuthProvider><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("username")).toHaveTextContent("viewer"));
    let resolve!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise((done) => { resolve = done; }));
    const pending = apiRequest("/funds/1").catch((error: unknown) => error);
    vi.mocked(authApi.me).mockResolvedValue({ ...viewer, fund_access_mode: "selected", fund_ids: [2] });
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(screen.getByTestId("scope")).toHaveTextContent("selected:2");
    resolve(new Response(JSON.stringify({ data: { id: 1 } }), { headers: { "content-type": "application/json" } }));
    await expect(pending).resolves.toMatchObject({ name: "AbortError" });
  });

  it("clears the old account immediately and blocks login while logout is pending", async () => {
    vi.mocked(authApi.authStatus).mockResolvedValue({ initialized: true });
    vi.mocked(authApi.me).mockResolvedValue(admin);
    let finish!: () => void;
    vi.mocked(authApi.logout).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<AuthProvider><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("username")).toHaveTextContent("admin"));
    await act(async () => { screen.getByRole("button", { name: "logout" }).click(); });
    expect(screen.getByTestId("username")).toHaveTextContent("none");
    expect(screen.getByTestId("loading")).toHaveTextContent("true");
    await act(async () => { finish(); });
    expect(screen.getByTestId("loading")).toHaveTextContent("false");
  });

});
