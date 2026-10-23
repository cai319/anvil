import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

export function PageHeader({
  kicker,
  title,
  subtitle,
  actions,
}: {
  kicker?: string;
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="page-header-main">
        {kicker && <span className="page-kicker">{kicker}</span>}
        <h1 className="page-title">{title}</h1>
        {subtitle && <div className="page-subtitle">{subtitle}</div>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function SectionHeader({
  title,
  count,
  actions,
}: {
  title: ReactNode;
  count?: number;
  actions?: ReactNode;
}) {
  return (
    <div className="section-header">
      <h2 className="section-title">{title}</h2>
      {count != null && <span className="count">{count}</span>}
      {actions && <span className="actions">{actions}</span>}
    </div>
  );
}

export function EmptyState({
  icon = "inbox",
  title,
  hint,
  action,
}: {
  icon?: IconName;
  title: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon"><Icon name={icon} size={18} /></div>
      <strong>{title}</strong>
      {hint && <p className="muted">{hint}</p>}
      {action && <div style={{ marginTop: 10 }}>{action}</div>}
    </div>
  );
}

export function StatusPill({
  status,
  label,
}: {
  status: string;
  label?: ReactNode;
}) {
  return <span className={`status status-${status}`}>{label ?? status}</span>;
}
