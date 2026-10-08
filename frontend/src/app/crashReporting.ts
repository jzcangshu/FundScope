/**
 * Errors that never reach React's render phase — a handler, a timer, a promise
 * nobody awaited — are invisible to an error boundary, yet a run of them still
 * means the build on disk is misbehaving. This registry lets the root recovery
 * screen hear about them; the screen decides whether an update can fix things.
 */

export type CrashSource = "boundary" | "global";
export type CrashHandler = (error: unknown, source: CrashSource) => void;

let handler: CrashHandler | null = null;

export function setCrashHandler(next: CrashHandler | null): void {
  handler = next;
}

export function reportCrash(error: unknown, source: CrashSource): void {
  handler?.(error, source);
}

/**
 * Watch for the two kinds of uncaught error React never sees. Installed before
 * the first render so a failure during mount is still observable, and returns a
 * detach function for tests.
 */
export function watchGlobalErrors(): () => void {
  if (typeof window === "undefined") return () => {};
  const onError = (event: ErrorEvent) => {
    console.error("[crash] uncaught error", event.error ?? event.message);
    reportCrash(event.error ?? event.message, "global");
  };
  const onRejection = (event: PromiseRejectionEvent) => {
    console.error("[crash] unhandled rejection", event.reason);
    reportCrash(event.reason, "global");
  };
  window.addEventListener("error", onError);
  window.addEventListener("unhandledrejection", onRejection);
  return () => {
    window.removeEventListener("error", onError);
    window.removeEventListener("unhandledrejection", onRejection);
  };
}
