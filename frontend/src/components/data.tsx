import { useState, type CSSProperties, type ReactNode } from "react";
import { Tooltip } from "antd";
import { Link } from "react-router-dom";
import { ArrowLeftOutlined } from "@ant-design/icons";
import { can, type Capability } from "@/utils/permissions";
import { useAuth } from "@/app/auth";
import type { AnalysisStatus, QualityStatus, ValidationLevel } from "@/utils/constants";
import {
  ANALYSIS_STATUS_HINT,
  ANALYSIS_STATUS_LABEL,
  QUALITY_STATUS_LABEL,
  VALIDATION_LEVEL_LABEL,
  toAnalysisStatus,
} from "@/utils/constants";

/* ---------- Num: tabular monospace figures ---------- */
export function Num({
  children,
  className = "",
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <span className={`num ${className}`.trim()} style={style}>
      {children === null || children === undefined ? "—" : children}
    </span>
  );
}

/* ---------- StatusText: a coloured dot + plain text (no tag backgrounds) ---------- */
export type Tone = "success" | "warning" | "danger" | "info" | "neutral";

export function StatusText({ tone, children, muted = false, title }: { tone: Tone; children: ReactNode; muted?: boolean; title?: string }) {
  return (
    <span className={`fd-status ${muted ? "fd-status--muted" : ""}`.trim()} title={title}>
      <span className={`q-dot q-dot--${tone}`} aria-hidden="true" />
      {children}
    </span>
  );
}

/* ---------- QualityBadge: semantic dot + label ---------- */
const QUALITY_TONE: Record<QualityStatus, Tone> = {
  valid: "success",
  warning: "warning",
  partial: "warning",
  stale: "danger",
  pending: "info",
};

export function QualityBadge({ status, showLabel = true }: { status: QualityStatus; showLabel?: boolean }) {
  const label = QUALITY_STATUS_LABEL[status] ?? status;
  return (
    <StatusText tone={QUALITY_TONE[status] ?? "neutral"} title={showLabel ? undefined : label}>
      {showLabel ? label : <span className="fd-visually-hidden">{label}</span>}
    </StatusText>
  );
}

/* ---------- AnalysisBadge: ready / pending / stale with explanation ---------- */
const ANALYSIS_TONE: Record<AnalysisStatus, Tone> = {
  ready: "success",
  pending: "info",
  stale: "warning",
};

export function AnalysisBadge({ status, showHint = true }: { status: string | null | undefined; showHint?: boolean }) {
  const normalized = toAnalysisStatus(status);
  const body = (
    <StatusText tone={ANALYSIS_TONE[normalized]} muted={normalized === "pending"}>
      {ANALYSIS_STATUS_LABEL[normalized]}
    </StatusText>
  );
  if (!showHint) return body;
  return <Tooltip title={ANALYSIS_STATUS_HINT[normalized]}>{body}</Tooltip>;
}

/* ---------- PageHeader: the sheet's letterhead ---------- */
/**
 * Renders the tinted band at the top of every sheet: back link, title, a
 * status line ("截至 08-24 · 16 只产品 · ● 有效"), actions on the right, and
 * whatever the page puts underneath (error banner, headline figures, anchor
 * navigation). Pages should not render `.fd-page__bar` themselves.
 */
export function PageHeader({
  title,
  status,
  desc,
  extra,
  back,
  children,
}: {
  title: ReactNode;
  /** Short facts, one `<span>` each; the separator dots are added by CSS. */
  status?: ReactNode;
  /** A single explanatory sentence. Prefer `status`; use this only when a sentence is genuinely needed. */
  desc?: ReactNode;
  extra?: ReactNode;
  back?: { to: string; label: string };
  children?: ReactNode;
}) {
  return (
    <header className="fd-page__head">
      <div className="fd-page__bar">
        <div className="fd-page__heading">
          {back && (
            <Link className="fd-page__back" to={back.to}>
              <ArrowLeftOutlined /> {back.label}
            </Link>
          )}
          <h1 className="fd-page__title">{title}</h1>
          {status && <div className="fd-statusline">{status}</div>}
          {desc && <p className="fd-page__desc">{desc}</p>}
        </div>
        {extra && <div className="fd-page__actions">{extra}</div>}
      </div>
      {children}
    </header>
  );
}

/* ---------- Stat: one headline figure in the letterhead's row of large numbers ---------- */
export function Stat({ label, value, sub, loading = false }: { label: ReactNode; value: ReactNode; sub?: ReactNode; loading?: boolean }) {
  return (
    <div className="fd-stat" aria-busy={loading}>
      <div className="fd-stat__label">{label}</div>
      <div className="fd-stat__value num">{loading ? <span className="fd-muted">—</span> : value}</div>
      {sub && <div className="fd-stat__sub">{sub}</div>}
    </div>
  );
}

/* ---------- Section: a hairline-separated block on the sheet ---------- */
export function Section({
  id,
  title,
  count,
  tools,
  ariaLabel,
  className = "",
  children,
}: {
  id?: string;
  title?: ReactNode;
  /** A small figure or note shown after the title. */
  count?: ReactNode;
  tools?: ReactNode;
  ariaLabel?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section id={id} className={`fd-section ${className}`.trim()} aria-label={ariaLabel ?? (typeof title === "string" ? title : undefined)}>
      {(title || tools) && (
        <div className="fd-section__head">
          <div className="fd-section__title">
            {title}
            {count !== undefined && count !== null && <small>{count}</small>}
          </div>
          {tools && <div className="fd-section__tools">{tools}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/* ---------- Disclosure: a quiet "show more" fold (native <details>) ---------- */
export function Disclosure({ summary, children, className = "" }: { summary: ReactNode; children: ReactNode; className?: string }) {
  return (
    <details className={`fd-disclosure ${className}`.trim()}>
      <summary>{summary}</summary>
      <div className="fd-disclosure__body">{children}</div>
    </details>
  );
}

/* ---------- Facts: label / value pairs laid out in a grid ---------- */
export function Facts({ items, columns }: { items: Array<{ label: ReactNode; value: ReactNode; wide?: boolean }>; columns?: number }) {
  return (
    <dl className="fd-facts" style={columns ? { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` } : undefined}>
      {items.map((item, index) => (
        <div key={index} className={item.wide ? "fd-facts__item--wide" : undefined}>
          <dt>{item.label}</dt>
          <dd>{item.value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

/* ---------- RoleGuard: hide write affordances for roles without the capability ---------- */
export function RoleGuard({ cap, children }: { cap: Capability; children: ReactNode }) {
  const { session } = useAuth();
  if (!can(session?.role, cap, session?.fund_access_mode)) return null;
  return <>{children}</>;
}

/* ---------- LevelTag: validation level as dot + text ---------- */
const LEVEL_TONE: Record<ValidationLevel, Tone> = { critical: "danger", warning: "warning", info: "neutral" };

export function LevelTag({ level }: { level: ValidationLevel }) {
  return <StatusText tone={LEVEL_TONE[level]}>{VALIDATION_LEVEL_LABEL[level]}</StatusText>;
}

/* ---------- Chip: a quiet neutral label ("当前", "重复", "已忽略") ---------- */
export function Chip({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`fd-chip ${className}`.trim()}>{children}</span>;
}

/* ---------- Compare: actual / expected / difference block used in finding tables ---------- */
export function Compare({ actual, expected, difference }: { actual: ReactNode; expected: ReactNode; difference: ReactNode }) {
  return (
    <div className="fd-compare">
      <span>实际</span><b>{actual ?? "—"}</b>
      <span>期望</span><b>{expected ?? "—"}</b>
      <span>差异</span><b>{difference ?? "—"}</b>
    </div>
  );
}

/* ---------- Truncate: collapse long text in dense table cells ---------- */
export function Truncate({
  value,
  maxChars = 80,
  className = "",
}: {
  value?: string | number | boolean | null | object;
  maxChars?: number;
  className?: string;
}) {
  const text = (() => {
    if (value === null || value === undefined || value === "") return "";
    if (typeof value === "string") return value;
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  })();
  const [expanded, setExpanded] = useState(false);
  const textClass = `fd-truncate ${typeof value === "object" && value !== null ? "fd-truncate--code" : ""} ${className}`.trim();
  if (!text) return <span className="fd-caption">—</span>;
  const overflows = text.length > maxChars;
  if (!overflows) {
    return (
      <Tooltip title={text} mouseEnterDelay={0.4}>
        <span className={textClass}>{text}</span>
      </Tooltip>
    );
  }
  const shown = expanded ? text : `${text.slice(0, maxChars)}…`;
  return (
    <span className={textClass}>
      <Tooltip title={text} mouseEnterDelay={0.4}>
        <span>{shown}</span>
      </Tooltip>{" "}
      <button
        type="button"
        className="fd-link-button"
        aria-expanded={expanded}
        onClick={(event) => {
          event.stopPropagation();
          setExpanded((current) => !current);
        }}
      >
        {expanded ? "收起" : "展开"}
      </button>
    </span>
  );
}
