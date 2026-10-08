import { Component, useCallback, useEffect, useRef, useState, type ErrorInfo, type ReactNode } from "react";
import { Button, Progress } from "antd";
import { CloudDownloadOutlined, RocketOutlined, WarningOutlined } from "@ant-design/icons";
import { useRevalidator, useRouteError } from "react-router-dom";
import type { Update } from "@tauri-apps/plugin-updater";
import { isDesktopRuntime } from "./runtime";
import { setCrashHandler, type CrashSource } from "./crashReporting";
import { downloadAndInstall, findPendingUpdate, relaunchApp, type UpdateProgress } from "@/utils/desktopUpdate";

const ATTEMPT_KEY = "fund-dashboard.forced-update-attempt";
/** How long a forced update that did not clear the crash suppresses the next one. */
const ATTEMPT_WINDOW_MS = 10 * 60 * 1000;
/** Uncaught non-render errors tolerated before the build counts as broken. */
const GLOBAL_CRASH_THRESHOLD = 3;

type ManualReason = "browser" | "no-update" | "check-failed" | "install-failed" | "repeat";

type Phase =
  | { kind: "checking" }
  | { kind: "forced"; version: string; progress: UpdateProgress }
  | { kind: "restarting"; version: string }
  | { kind: "manual"; reason: ManualReason; detail?: string; version?: string };

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  return typeof error === "string" ? error : String(error);
}

function readAttempt(): { version: string; at: number } | null {
  try {
    const raw = window.localStorage.getItem(ATTEMPT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { version?: unknown; at?: unknown };
    if (typeof parsed.version !== "string" || typeof parsed.at !== "number") return null;
    return { version: parsed.version, at: parsed.at };
  } catch {
    return null;
  }
}

function rememberAttempt(version: string): void {
  try {
    window.localStorage.setItem(ATTEMPT_KEY, JSON.stringify({ version, at: Date.now() }));
  } catch {
    /* storage unavailable — the attempt simply is not remembered */
  }
}

/** An update we already installed without clearing the crash must not loop forever. */
function attemptIsFresh(version: string): boolean {
  const attempt = readAttempt();
  return attempt !== null && attempt.version === version && Date.now() - attempt.at < ATTEMPT_WINDOW_MS;
}

function describe(phase: Phase): { title: string; body: string } {
  if (phase.kind === "checking") return { title: "应用出了点问题", body: "正在检查是否有新版本…" };
  if (phase.kind === "forced") return { title: `正在更新到 v${phase.version}`, body: "检测到新版本，正在自动下载并安装；完成后应用会自行重启。" };
  if (phase.kind === "restarting") return { title: "更新完成", body: "正在重启应用…" };
  switch (phase.reason) {
    case "no-update":
      return { title: "应用出了点问题", body: "当前已是最新版本，重试或刷新页面即可。" };
    case "check-failed":
      return { title: "应用出了点问题", body: `检查更新失败：${phase.detail ?? "未知原因"}` };
    case "install-failed":
      return { title: "自动更新失败", body: phase.detail ?? "下载或安装过程中出错。" };
    case "repeat":
      return { title: "应用出了点问题", body: `已尝试更新到 v${phase.version}，问题仍然存在。` };
    default:
      return { title: "应用出了点问题", body: "刷新页面即可重新加载。" };
  }
}

/**
 * Shown in place of a crashed app tree. On the desktop build it first asks the
 * release manifest for a newer version and, when one exists, installs it without
 * asking: a crash the user cannot act on is exactly the case where the running
 * build is the problem. When no update can help, it hands back the ordinary
 * retry / reload controls instead of a dead end.
 */
export function RecoveryScreen({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const [phase, setPhase] = useState<Phase>({ kind: "checking" });
  const [checkNonce, setCheckNonce] = useState(0);
  const pending = useRef<Update | null>(null);

  const runForcedUpdate = useCallback(async (update: Update) => {
    rememberAttempt(update.version);
    setPhase({ kind: "forced", version: update.version, progress: { received: 0, total: 0, percent: 0 } });
    try {
      await downloadAndInstall(update, (progress) => setPhase({ kind: "forced", version: update.version, progress }));
      setPhase({ kind: "restarting", version: update.version });
      await relaunchApp();
    } catch (installError) {
      setPhase({ kind: "manual", reason: "install-failed", detail: messageOf(installError), version: update.version });
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      if (!isDesktopRuntime()) {
        // The web build reloads to the current front end; there is nothing to install.
        setPhase({ kind: "manual", reason: "browser" });
        return;
      }
      let update: Update | null;
      try {
        update = await findPendingUpdate();
      } catch (checkError) {
        if (!cancelled) setPhase({ kind: "manual", reason: "check-failed", detail: messageOf(checkError) });
        return;
      }
      if (cancelled) return;
      pending.current = update;
      if (!update) {
        setPhase({ kind: "manual", reason: "no-update" });
        return;
      }
      if (attemptIsFresh(update.version)) {
        setPhase({ kind: "manual", reason: "repeat", version: update.version });
        return;
      }
      await runForcedUpdate(update);
    })();
    return () => {
      cancelled = true;
    };
  }, [error, checkNonce, runForcedUpdate]);

  const { title, body } = describe(phase);
  const manual = phase.kind === "manual" ? phase : null;
  const canForce = manual?.version !== undefined && (manual.reason === "repeat" || manual.reason === "install-failed");

  return (
    <div className="fd-recovery" role="alert">
      <div className="fd-recovery__card">
        <div className="fd-recovery__mark" aria-hidden="true">
          {phase.kind === "forced" || phase.kind === "restarting" ? <CloudDownloadOutlined /> : <WarningOutlined />}
        </div>
        <h1 className="fd-recovery__title">{title}</h1>
        <p className="fd-recovery__body">{body}</p>
        {phase.kind === "forced" && (
          <div className="fd-recovery__progress">
            <Progress percent={phase.progress.percent} size="small" showInfo={false} />
          </div>
        )}
        {manual && (
          <div className="fd-recovery__actions">
            {canForce && (
              <Button type="primary" icon={<RocketOutlined />} onClick={() => pending.current && void runForcedUpdate(pending.current)}>
                仍要更新 v{manual.version}
              </Button>
            )}
            {onRetry && <Button onClick={onRetry}>重试</Button>}
            <Button onClick={() => window.location.reload()}>重新加载</Button>
            {(manual.reason === "no-update" || manual.reason === "check-failed") && (
              <Button onClick={() => setCheckNonce((value) => value + 1)}>再检查一次</Button>
            )}
          </div>
        )}
        <details className="fd-recovery__detail">
          <summary>技术详情</summary>
          <pre>{messageOf(error)}</pre>
        </details>
      </div>
    </div>
  );
}

/**
 * Router-level safety net. React Router catches errors raised inside a route and
 * renders its own default "Unexpected Application Error!" page, so the boundary
 * below never sees them — this is what actually replaces that page, and it is why
 * a crash inside AppLayout no longer strands the user without an upgrade path.
 */
export function RecoveryRoute() {
  const error = useRouteError();
  const revalidator = useRevalidator();
  return <RecoveryScreen error={error} onRetry={() => revalidator.revalidate()} />;
}

interface CrashRecoveryState {
  failure: { error: unknown } | null;
}

/**
 * Catches what the router cannot: errors raised outside a route, such as a
 * provider failing during mount. Uncaught errors that never enter the render
 * phase are watched separately and only escalate once they repeat.
 */
export class CrashRecovery extends Component<{ children: ReactNode }, CrashRecoveryState> {
  state: CrashRecoveryState = { failure: null };

  private globalCrashes = 0;

  static getDerivedStateFromError(error: unknown): CrashRecoveryState {
    return { failure: { error } };
  }

  componentDidMount(): void {
    setCrashHandler(this.onCrashReported);
  }

  componentWillUnmount(): void {
    setCrashHandler(null);
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error("[crash] render error", error, info.componentStack);
  }

  private onCrashReported = (error: unknown, source: CrashSource): void => {
    // A failure this boundary caught is already on screen; only errors React
    // never saw need the escalation path.
    if (source !== "global") return;
    this.globalCrashes += 1;
    if (this.globalCrashes < GLOBAL_CRASH_THRESHOLD) return;
    // Confirm an update exists before taking over, so a lone stray rejection
    // never hijacks an app that is otherwise working.
    void findPendingUpdate()
      .then((update) => {
        if (update) this.setState({ failure: { error } });
      })
      .catch(() => {
        /* nothing to escalate to */
      });
  };

  private retry = (): void => {
    this.globalCrashes = 0;
    this.setState({ failure: null });
  };

  render(): ReactNode {
    if (this.state.failure) return <RecoveryScreen error={this.state.failure.error} onRetry={this.retry} />;
    return this.props.children;
  }
}
