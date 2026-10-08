import type { Update } from "@tauri-apps/plugin-updater";
import { isDesktopRuntime } from "@/app/runtime";

/** Bytes received so far, plus a rounded percentage for progress bars. */
export interface UpdateProgress {
  received: number;
  total: number;
  percent: number;
}

/**
 * Ask the release manifest whether a newer build exists. Resolves to null on the
 * web build (there is nothing to update) and whenever the running version is
 * already current.
 *
 * Shared by the update banner and the crash recovery screen so both go through
 * exactly one implementation of the check / download / relaunch sequence.
 */
export async function findPendingUpdate(): Promise<Update | null> {
  if (!isDesktopRuntime()) return null;
  const { check } = await import("@tauri-apps/plugin-updater");
  return (await check()) ?? null;
}

/**
 * Download the pending update and let the NSIS installer run silently. Resolves
 * once the new version is on disk, at which point a relaunch boots into it.
 */
export async function downloadAndInstall(update: Update, onProgress?: (progress: UpdateProgress) => void): Promise<void> {
  let received = 0;
  let total = 0;
  await update.downloadAndInstall((event) => {
    if (event.event === "Started") {
      total = event.data.contentLength ?? 0;
      onProgress?.({ received, total, percent: 0 });
    } else if (event.event === "Progress") {
      received += event.data.chunkLength;
      // Hold the bar at 99 until the installer reports Finished, so "100%" always
      // means the bytes are on disk rather than merely counted.
      onProgress?.({ received, total, percent: total > 0 ? Math.min(99, Math.round((received / total) * 100)) : 0 });
    } else {
      onProgress?.({ received, total, percent: 100 });
    }
  });
}

/** Restart into the freshly installed build. */
export async function relaunchApp(): Promise<void> {
  const { relaunch } = await import("@tauri-apps/plugin-process");
  await relaunch();
}
