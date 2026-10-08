import { useCallback, useEffect, useState } from "react";
import { Button, Progress } from "antd";
import { CloudDownloadOutlined, RocketOutlined } from "@ant-design/icons";
import { isDesktopRuntime } from "@/app/runtime";
import { downloadAndInstall, findPendingUpdate, relaunchApp } from "@/utils/desktopUpdate";

type UpdatePhase =
  | { kind: "idle" }
  | { kind: "available"; version: string; notes: string | null }
  | { kind: "downloading"; progress: number }
  | { kind: "ready" }
  | { kind: "failed"; message: string };

/**
 * Desktop-only update prompt: checks the release manifest once on startup,
 * offers a one-click download + install, then restarts into the new version.
 * The web build has nothing to update, so it renders nothing.
 */
export function UpdateBanner() {
  const [phase, setPhase] = useState<UpdatePhase>({ kind: "idle" });

  useEffect(() => {
    if (!isDesktopRuntime()) return;
    let cancelled = false;
    void (async () => {
      try {
        const update = await findPendingUpdate();
        if (cancelled || !update) return;
        setPhase({ kind: "available", version: update.version, notes: update.body ?? null });
      } catch (error) {
        console.warn("update check failed", error);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const install = useCallback(async () => {
    try {
      // Re-resolve the pending update; the check is cheap against the cached
      // manifest and keeps the download handle scoped to this click.
      const update = await findPendingUpdate();
      if (!update) {
        setPhase({ kind: "idle" });
        return;
      }
      setPhase({ kind: "downloading", progress: 0 });
      await downloadAndInstall(update, (progress) => setPhase({ kind: "downloading", progress: progress.percent }));
      setPhase({ kind: "ready" });
      // The NSIS installer finishes silently; relaunching completes the update.
      await relaunchApp();
    } catch (error) {
      setPhase({ kind: "failed", message: error instanceof Error ? error.message : String(error) });
    }
  }, []);

  if (!isDesktopRuntime() || phase.kind === "idle") return null;

  return (
    <div className="fd-update-banner" role="status" aria-live="polite">
      {phase.kind === "available" && (
        <>
          <CloudDownloadOutlined aria-hidden="true" />
          <div className="fd-update-banner__copy">
            <strong>新版本 v{phase.version} 可用</strong>
            {phase.notes && <span className="fd-update-banner__notes">{phase.notes}</span>}
          </div>
          <Button type="primary" size="small" icon={<RocketOutlined />} onClick={() => void install()}>
            下载并安装
          </Button>
        </>
      )}
      {phase.kind === "downloading" && (
        <>
          <CloudDownloadOutlined aria-hidden="true" />
          <div className="fd-update-banner__copy">
            <strong>正在下载更新…</strong>
            <Progress percent={phase.progress} size="small" showInfo={false} />
          </div>
          <span className="fd-update-banner__pct num">{phase.progress}%</span>
        </>
      )}
      {phase.kind === "ready" && (
        <>
          <RocketOutlined aria-hidden="true" />
          <div className="fd-update-banner__copy">
            <strong>更新已就绪</strong>
            <span className="fd-update-banner__notes">重启应用即可完成升级</span>
          </div>
          <Button type="primary" size="small" onClick={() => void relaunchApp()}>
            重启应用
          </Button>
        </>
      )}
      {phase.kind === "failed" && (
        <>
          <div className="fd-update-banner__copy">
            <strong>更新失败</strong>
            <span className="fd-update-banner__notes">{phase.message}</span>
          </div>
          <Button size="small" onClick={() => void install()}>重试</Button>
        </>
      )}
    </div>
  );
}
