import { useCallback, useEffect, useRef, useState } from "react";
import { App as AntdApp } from "antd";
import { isApiError } from "@/api/client";
import { describeError } from "./feedback";

/* ---------- Toast access ---------- */
export function useToast() {
  const { message } = AntdApp.useApp();
  return message;
}

/* ---------- Modal access ---------- */
export function useModal() {
  const { modal } = AntdApp.useApp();
  return modal;
}

/* ---------- useAsyncData: load → data / error / loading with retry ---------- */
export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refreshing: boolean;
  reload: (options?: { silent?: boolean }) => Promise<void>;
  setData: (updater: T | null | ((current: T | null) => T | null)) => void;
  clearError: () => void;
}

/**
 * Runs `loader` when `deps` change. `reload({ silent: true })` refreshes in the
 * background without showing the loading state (used for polling). Out-of-order
 * responses are dropped so a slow request never overwrites a newer one.
 */
export function useAsyncData<T>(loader: () => Promise<T>, deps: unknown[], fallback: string): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const loaderRef = useRef(loader);
  const requestId = useRef(0);
  loaderRef.current = loader;

  const reload = useCallback(async (options?: { silent?: boolean }) => {
    const id = ++requestId.current;
    if (options?.silent) setRefreshing(true);
    else {
      setLoading(true);
      setError(null);
    }
    try {
      const result = await loaderRef.current();
      if (id !== requestId.current) return;
      setData(result);
      setError(null);
    } catch (caught) {
      if (id !== requestId.current) return;
      const denied = isApiError(caught) && [401, 403, 404].includes(caught.status);
      if (denied) setData(null);
      if (denied || !options?.silent) setError(describeError(caught, fallback));
    } finally {
      if (id === requestId.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [fallback]);

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => () => { requestId.current += 1; }, []);

  return {
    data,
    error,
    loading,
    refreshing,
    reload,
    setData,
    clearError: () => setError(null),
  };
}

/* ---------- useInterval: run while `active`, pause when the tab is hidden ---------- */
export function useInterval(callback: () => void, delayMs: number | null, active = true) {
  const callbackRef = useRef(callback);
  callbackRef.current = callback;
  useEffect(() => {
    if (!active || delayMs === null) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer === null) timer = setInterval(() => callbackRef.current(), delayMs);
    };
    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => (document.visibilityState === "visible" ? start() : stop());
    document.addEventListener("visibilitychange", onVisibility);
    onVisibility();
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [delayMs, active]);
}

/* ---------- useMediaQuery ---------- */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => (typeof window !== "undefined" ? window.matchMedia(query).matches : false));
  useEffect(() => {
    const media = window.matchMedia(query);
    const listener = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(media.matches);
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, [query]);
  return matches;
}
