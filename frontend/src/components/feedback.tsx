import type { ReactNode } from "react";
import { Alert, Button } from "antd";
import { Link } from "react-router-dom";
import { InboxOutlined, LockOutlined, SearchOutlined, WarningOutlined } from "@ant-design/icons";
import { apiErrorMessage, isApiError } from "@/api/client";

/* ---------- EmptyState: no data / no match / not configured ---------- */
export function EmptyState({
  variant = "empty",
  title,
  hint,
  action,
  compact = false,
}: {
  variant?: "empty" | "filtered" | "forbidden";
  title?: string;
  hint?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
}) {
  const defaults = {
    empty: { icon: <InboxOutlined />, title: "暂无数据" },
    filtered: { icon: <SearchOutlined />, title: "没有符合条件的记录" },
    forbidden: { icon: <LockOutlined />, title: "当前账号无权查看" },
  }[variant];
  return (
    <div className={`fd-state ${compact ? "fd-state--compact" : ""} ${variant === "forbidden" ? "fd-state--forbidden" : ""}`.trim()} role="status">
      <span className="fd-state__icon" aria-hidden="true">{defaults.icon}</span>
      <div className="fd-state__title">{title ?? defaults.title}</div>
      {hint && <div className="fd-state__hint">{hint}</div>}
      {action && <div className="fd-state__actions">{action}</div>}
    </div>
  );
}

/* ---------- Error helpers ---------- */
/** Build a user-facing error message: server detail when present, otherwise the fallback. */
export function describeError(error: unknown, fallback: string): string {
  if (isApiError(error)) {
    if (error.status === 403) return "当前账号没有执行该操作的权限";
    if (error.status >= 500) return `${fallback}（服务暂时不可用，${error.status}）`;
    return apiErrorMessage(error, fallback);
  }
  if (error instanceof DOMException && error.name === "AbortError") return `${fallback}（请求超时）`;
  return fallback;
}

/** Inline page-level error banner with optional retry. */
export function PageError({
  message,
  onRetry,
  onClose,
  retrying = false,
}: {
  message: string | null;
  onRetry?: () => void;
  onClose?: () => void;
  retrying?: boolean;
}) {
  if (!message) return null;
  return (
    <Alert
      className="fd-alert"
      type="error"
      showIcon
      message={message}
      closable={Boolean(onClose)}
      onClose={onClose}
      action={onRetry ? <Button size="small" onClick={onRetry} loading={retrying}>重试</Button> : undefined}
    />
  );
}

/** Full-block error state used when a page or section has nothing to show. */
export function ErrorState({ message, onRetry, retrying = false }: { message: string; onRetry?: () => void; retrying?: boolean }) {
  return (
    <div className="fd-state fd-state--error" role="alert">
      <span className="fd-state__icon" aria-hidden="true"><WarningOutlined /></span>
      <div className="fd-state__title">加载失败</div>
      <div className="fd-state__hint">{message}</div>
      {onRetry && <div className="fd-state__actions"><Button onClick={onRetry} loading={retrying}>重试</Button></div>}
    </div>
  );
}

/** Route-level "no permission" page. */
export function ForbiddenPage() {
  return (
    <div className="fd-page">
      <div className="fd-state fd-state--forbidden fd-state--page">
        <span className="fd-state__icon" aria-hidden="true"><LockOutlined /></span>
        <div className="fd-state__title">当前账号无权访问此页面</div>
        <div className="fd-state__hint">如需开通权限，请联系系统管理员调整账号角色或产品范围。</div>
        <div className="fd-state__actions">
          <Link to="/dashboard"><Button type="primary">返回总览</Button></Link>
        </div>
      </div>
    </div>
  );
}
