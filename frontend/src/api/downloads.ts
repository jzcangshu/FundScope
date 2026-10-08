import { isDesktopRuntime } from "@/app/runtime";
import { apiRequest, type ApiRequestOptions } from "./client";

export async function downloadBlob(path: string, filename: string, options: ApiRequestOptions = {}): Promise<boolean> {
  if (isDesktopRuntime()) {
    return saveWithDialog(path, filename, options);
  }
  const blob = await apiRequest<Blob>(path, options);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
  return true;
}

/**
 * Desktop exports bypass the webview download pipeline (blob anchors never
 * surface a save dialog there). Ask for a destination first so cancelled
 * dialogs cost no network traffic, then fetch and persist the file.
 * Returns false when the user cancels the save dialog.
 */
async function saveWithDialog(path: string, filename: string, options: ApiRequestOptions = {}): Promise<boolean> {
  const { invoke } = await import("@tauri-apps/api/core");
  const destination = await invoke<string | null>("pick_download_destination", {
    defaultFilename: filename,
  });
  if (!destination) return false;
  const blob = await apiRequest<Blob>(path, options);
  await invoke("write_saved_file", {
    path: destination,
    contents: new Uint8Array(await blob.arrayBuffer()),
  });
  return true;
}
