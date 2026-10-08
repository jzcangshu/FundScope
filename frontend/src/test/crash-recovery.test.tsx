import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Update } from "@tauri-apps/plugin-updater";
import { CrashRecovery, RecoveryRoute } from "@/app/CrashRecovery";
import { reportCrash } from "@/app/crashReporting";
import { downloadAndInstall, findPendingUpdate, relaunchApp } from "@/utils/desktopUpdate";

vi.mock("@/utils/desktopUpdate", () => ({
  findPendingUpdate: vi.fn(),
  downloadAndInstall: vi.fn(),
  relaunchApp: vi.fn(),
}));

const ATTEMPT_KEY = "fund-dashboard.forced-update-attempt";

/** The shape RecoveryScreen actually reads off a pending update. */
function update(version: string): Update {
  return { version, body: `notes for ${version}` } as unknown as Update;
}

function goDesktop(): void {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
}

/** antd pads two-character Chinese labels with a space, so match leniently. */
const RETRY = /重\s*试/;

/** Holds the download open so the in-progress phase can be asserted. */
function holdDownload(): () => void {
  let finish: () => void = () => {};
  vi.mocked(downloadAndInstall).mockImplementation(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
  );
  return () => finish();
}

function Boom(): JSX.Element {
  throw new Error("Cannot read properties of undefined (reading 'dashboard')");
}

/** Mirrors AppLayout: a nested layout route whose render throws. */
function BoomLayout(): JSX.Element {
  return (
    <div>
      <Boom />
      <div>never reached</div>
    </div>
  );
}

describe("crash recovery", () => {
  beforeEach(() => {
    vi.mocked(findPendingUpdate).mockResolvedValue(null);
    vi.mocked(downloadAndInstall).mockResolvedValue(undefined);
    vi.mocked(relaunchApp).mockResolvedValue(undefined);
    // React logs every caught render error; keep the report readable.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    window.localStorage.clear();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("renders children untouched while nothing throws", () => {
    render(
      <CrashRecovery>
        <div>工作台</div>
      </CrashRecovery>,
    );
    expect(screen.getByText("工作台")).toBeInTheDocument();
    expect(findPendingUpdate).not.toHaveBeenCalled();
  });

  it("replaces a crashed tree with a recovery screen instead of a dead end", async () => {
    render(
      <CrashRecovery>
        <Boom />
      </CrashRecovery>,
    );
    expect(await screen.findByText("应用出了点问题")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
    // The thrown message stays available for whoever is diagnosing this.
    expect(screen.getByText(/reading 'dashboard'/)).toBeInTheDocument();
  });

  it("hands React Router's own error page over to the recovery screen", async () => {
    // The production path: AppLayout throws inside a nested layout route, and React
    // Router catches it before any outer boundary can. Without the errorElement the
    // user would get the router's built-in page, which offers no way to upgrade out
    // of the crash — the dead end this whole module exists to remove.
    const router = createMemoryRouter(
      [
        {
          errorElement: <RecoveryRoute />,
          children: [{ element: <BoomLayout />, children: [{ path: "/dashboard", element: <div>工作台</div> }] }],
        },
      ],
      { initialEntries: ["/dashboard"] },
    );
    render(<RouterProvider router={router} />);
    expect(await screen.findByText("应用出了点问题")).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
    expect(screen.queryByText("never reached")).toBeNull();
  });

  it("never touches the updater on the web build", async () => {
    render(
      <CrashRecovery>
        <Boom />
      </CrashRecovery>,
    );
    expect(await screen.findByText(/刷新页面即可重新加载/)).toBeInTheDocument();
    expect(findPendingUpdate).not.toHaveBeenCalled();
    expect(downloadAndInstall).not.toHaveBeenCalled();
  });

  it("offers retry and a fresh check when the running version is current", async () => {
    goDesktop();
    render(
      <CrashRecovery>
        <Boom />
      </CrashRecovery>,
    );
    expect(await screen.findByText(/当前已是最新版本/)).toBeInTheDocument();
    expect(downloadAndInstall).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: RETRY })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "再检查一次" })).toBeInTheDocument();
  });

  it("installs an available update without waiting to be asked", async () => {
    goDesktop();
    const pending = update("0.2.10");
    vi.mocked(findPendingUpdate).mockResolvedValue(pending);
    const finishDownload = holdDownload();

    render(
      <CrashRecovery>
        <Boom />
      </CrashRecovery>,
    );

    expect(await screen.findByText("正在更新到 v0.2.10")).toBeInTheDocument();
    expect(downloadAndInstall).toHaveBeenCalledWith(pending, expect.any(Function));
    expect(relaunchApp).not.toHaveBeenCalled();

    finishDownload();
    await waitFor(() => expect(relaunchApp).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("更新完成")).toBeInTheDocument();
  });

  it("does not loop when the same update already failed to clear the crash", async () => {
    goDesktop();
    const pending = update("0.2.10");
    vi.mocked(findPendingUpdate).mockResolvedValue(pending);
    window.localStorage.setItem(ATTEMPT_KEY, JSON.stringify({ version: "0.2.10", at: Date.now() }));

    render(
      <CrashRecovery>
        <Boom />
      </CrashRecovery>,
    );

    expect(await screen.findByText(/已尝试更新到 v0\.2\.10/)).toBeInTheDocument();
    expect(downloadAndInstall).not.toHaveBeenCalled();

    // The decision goes back to the user rather than being taken away.
    fireEvent.click(screen.getByRole("button", { name: /仍要更新 v0\.2\.10/ }));
    await waitFor(() => expect(downloadAndInstall).toHaveBeenCalledWith(pending, expect.any(Function)));
  });

  it("retries in place, re-rendering the tree that previously threw", async () => {
    let shouldThrow = true;
    function Flaky() {
      if (shouldThrow) throw new Error("boom");
      return <div>已恢复</div>;
    }
    render(
      <CrashRecovery>
        <Flaky />
      </CrashRecovery>,
    );
    expect(await screen.findByText(/刷新页面即可重新加载/)).toBeInTheDocument();

    shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: RETRY }));
    expect(await screen.findByText("已恢复")).toBeInTheDocument();
  });

  it("ignores a stray uncaught error but escalates when they keep coming", async () => {
    goDesktop();
    vi.mocked(findPendingUpdate).mockResolvedValue(update("0.2.10"));
    holdDownload();
    render(
      <CrashRecovery>
        <div>工作台</div>
      </CrashRecovery>,
    );

    reportCrash(new Error("stray rejection"), "global");
    reportCrash(new Error("stray rejection"), "global");
    // Two errors are not yet a pattern; the app stays usable.
    expect(screen.getByText("工作台")).toBeInTheDocument();
    expect(findPendingUpdate).not.toHaveBeenCalled();

    reportCrash(new Error("stray rejection"), "global");
    expect(await screen.findByText("正在更新到 v0.2.10")).toBeInTheDocument();
  });

  it("keeps a repeatedly failing app on screen when no update can help", async () => {
    goDesktop();
    render(
      <CrashRecovery>
        <div>工作台</div>
      </CrashRecovery>,
    );
    for (let i = 0; i < 3; i += 1) reportCrash(new Error("boom"), "global");

    await waitFor(() => expect(findPendingUpdate).toHaveBeenCalledTimes(1));
    // No update exists, so the working app is left alone.
    expect(screen.getByText("工作台")).toBeInTheDocument();
  });
});
