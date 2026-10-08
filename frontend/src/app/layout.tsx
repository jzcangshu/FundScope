import { useCallback, useEffect, useMemo, useState, type DragEvent, type ReactNode } from "react";
import { Avatar, Dropdown, Tooltip } from "antd";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  AppstoreOutlined,
  AuditOutlined,
  CheckSquareOutlined,
  CloudUploadOutlined,
  ControlOutlined,
  DatabaseOutlined,
  FundOutlined,
  LogoutOutlined,
  MailOutlined,
  MenuFoldOutlined,
  MenuOutlined,
  MenuUnfoldOutlined,
  MoonOutlined,
  PartitionOutlined,
  PieChartOutlined,
  SettingOutlined,
  SunOutlined,
  TeamOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { ROLE_LABEL } from "@/utils/constants";
import { isProductScoped, navForRole } from "@/utils/permissions";
import { useAuth } from "@/app/auth";
import { useTheme } from "@/app/theme";
import { CacheStatus, UpdateBanner } from "@/components";

export const PRODUCT_NAME = "FundScope";
export const PRODUCT_TAGLINE = "PRIVATE FUND VALUATION";
export const PRODUCT_SHORT_TAGLINE = "FUND VALUATION";

const SIDEBAR_KEY = "fundscope.sidebar";

interface NavItem {
  key: string;
  label: string;
  path: string;
  group: string;
  icon: ReactNode;
}

const ALL_NAV: NavItem[] = [
  { key: "dashboard", label: "公司总览", path: "/dashboard", group: "总览", icon: <PieChartOutlined /> },
  { key: "risk", label: "风险事件", path: "/risk", group: "总览", icon: <WarningOutlined /> },
  { key: "funds", label: "产品列表", path: "/funds", group: "产品分析", icon: <FundOutlined /> },
  { key: "imports", label: "导入中心", path: "/imports", group: "数据运营", icon: <CloudUploadOutlined /> },
  { key: "reviews", label: "异常复核", path: "/reviews", group: "数据运营", icon: <CheckSquareOutlined /> },
  { key: "mail", label: "邮件接入", path: "/mail", group: "数据运营", icon: <MailOutlined /> },
  { key: "adminFunds", label: "产品管理", path: "/admin/funds", group: "基础配置", icon: <AppstoreOutlined /> },
  { key: "adminSubjects", label: "科目映射", path: "/admin/subjects", group: "基础配置", icon: <PartitionOutlined /> },
  { key: "adminRiskRules", label: "风险规则", path: "/admin/risk-rules", group: "基础配置", icon: <ControlOutlined /> },
  { key: "adminUsers", label: "账号管理", path: "/admin/users", group: "系统管理", icon: <TeamOutlined /> },
  { key: "accountPermissions", label: "账号权限一览", path: "/account-permissions", group: "系统管理", icon: <TeamOutlined /> },
  { key: "adminAudit", label: "审计日志", path: "/admin/audit", group: "系统管理", icon: <AuditOutlined /> },
  { key: "adminSettings", label: "系统设置", path: "/admin/settings", group: "系统管理", icon: <SettingOutlined /> },
  { key: "adminRetention", label: "数据保留", path: "/admin/retention", group: "系统管理", icon: <DatabaseOutlined /> },
];

const GROUP_ORDER = ["总览", "产品分析", "数据运营", "基础配置", "系统管理"];

/** The supplied red and green brand mark, shared by every entry point. */
export function BrandMark({ size = "md" }: { size?: "md" | "lg" }) {
  return (
    <img
      className={`fd-brand-mark ${size === "lg" ? "fd-brand-mark--lg" : ""}`.trim()}
      src="/brand/fundscope-mark.png"
      alt=""
      aria-hidden="true"
      width={size === "lg" ? 40 : 32}
      height={size === "lg" ? 40 : 32}
      draggable={false}
    />
  );
}

function Brand({ to = "/dashboard", draggable = true }: { to?: string; draggable?: boolean }) {
  return (
    <Link to={to} className="fd-brand" aria-label={`${PRODUCT_NAME} 首页`} draggable={draggable}>
      <BrandMark />
      <div className="fd-brand__copy">
        <strong>{PRODUCT_NAME}</strong>
        <span>{PRODUCT_TAGLINE}</span>
      </div>
    </Link>
  );
}

// A drag started from selectable nav text or a nav link can wedge all WebView2
// mouse/keyboard input until restart (WebView2Feedback #5613/#5695/#5715; the
// desktop host replaces the webview's default OLE drop target), so the sidebar
// never lets one start. antd menus/tooltips portal outside the sidebar DOM and
// must stay unaffected.
export function preventSidebarDragStart(event: DragEvent<HTMLElement>): void {
  if (event.target instanceof Node && event.currentTarget.contains(event.target)) {
    event.preventDefault();
  }
}

export function ThemeToggle({ className }: { className?: string }) {
  const { mode, toggle } = useTheme();
  const label = mode === "dark" ? "切换到浅色主题" : "切换到深色主题";
  return (
    <Tooltip title={label} placement="right">
      <button type="button" className={`fd-icon-button ${className ?? ""}`.trim()} aria-label={label} onClick={toggle}>
        {mode === "dark" ? <SunOutlined /> : <MoonOutlined />}
      </button>
    </Tooltip>
  );
}

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_KEY) === "collapsed";
  } catch {
    return false;
  }
}

export function AppLayout() {
  const { session, logout } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const navigate = useNavigate();
  const loc = useLocation();
  const allowed = useMemo(() => new Set(navForRole(session?.role ?? "viewer", session?.fund_access_mode)), [session?.role, session?.fund_access_mode]);
  const visibleNav = useMemo(() => ALL_NAV.filter((item) => allowed.has(item.key)).map((item) => item.key === "dashboard" && isProductScoped(session) ? { ...item, label: "选定产品总览" } : item), [allowed, session]);
  const groups = useMemo(
    () => GROUP_ORDER.filter((group) => visibleNav.some((item) => item.group === group)),
    [visibleNav],
  );
  const selected = visibleNav.find((item) => loc.pathname === item.path || loc.pathname.startsWith(`${item.path}/`));
  // display_name is optional on the backend; fall back to the login name rather than "未登录".
  const userLabel = session?.display_name?.trim() || session?.username || "未登录";

  useEffect(() => {
    document.title = selected ? `${selected.label} · ${PRODUCT_NAME}` : `${PRODUCT_NAME} · ${PRODUCT_SHORT_TAGLINE}`;
  }, [selected]);

  useEffect(() => {
    setMobileOpen(false);
  }, [loc.pathname]);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(SIDEBAR_KEY, next ? "collapsed" : "expanded");
      } catch {
        /* storage unavailable */
      }
      return next;
    });
  }, []);

  const userMenu = {
    items: [
      { key: "who", label: <span className="fd-muted">{session ? `${userLabel} · ${ROLE_LABEL[session.role]}` : ""}</span>, disabled: true },
      { type: "divider" as const },
      { key: "logout", label: "退出登录", icon: <LogoutOutlined />, danger: true },
    ],
    onClick: async ({ key }: { key: string }) => {
      if (key !== "logout") return;
      try {
        await logout();
      } finally {
        navigate("/login", { replace: true });
      }
    },
  };

  const collapseLabel = collapsed ? "展开侧栏" : "收起侧栏";

  return (
    <div className="fd-shell" data-sidebar={collapsed ? "collapsed" : "expanded"}>
      <a className="fd-skip-link" href="#fd-main-content">跳到主要内容</a>
      <div className={`fd-mobile-backdrop ${mobileOpen ? "is-visible" : ""}`} onClick={() => setMobileOpen(false)} aria-hidden="true" />
      <aside className={`fd-sidebar ${mobileOpen ? "is-open" : ""}`} aria-label="主导航">
        <div className="fd-sidebar__brand" onDragStartCapture={preventSidebarDragStart}>
          <Brand draggable={false} />
        </div>
        <nav className="fd-nav" onDragStartCapture={preventSidebarDragStart}>
          {groups.map((group) => (
            <section className="fd-nav-group" key={group} aria-label={group}>
              <div className="fd-nav-label">{group}</div>
              {visibleNav
                .filter((item) => item.group === group)
                .map((item) => {
                  const active = selected?.key === item.key;
                  const link = (
                    <Link
                      key={item.key}
                      to={item.path}
                      className={`fd-nav-item ${active ? "is-active" : ""}`.trim()}
                      aria-current={active ? "page" : undefined}
                      aria-label={collapsed ? item.label : undefined}
                      draggable={false}
                    >
                      <span className="fd-nav-icon" aria-hidden="true">{item.icon}</span>
                      <span className="fd-nav-item__label">{item.label}</span>
                    </Link>
                  );
                  return collapsed ? (
                    <Tooltip key={item.key} title={item.label} placement="right" mouseEnterDelay={0.2}>
                      {link}
                    </Tooltip>
                  ) : (
                    link
                  );
                })}
            </section>
          ))}
        </nav>
        <div className="fd-sidebar__footer">
          <Dropdown menu={userMenu} placement="topLeft" trigger={["click"]}>
            <button type="button" className="fd-user" aria-label="打开用户菜单" aria-haspopup="menu">
              <Avatar size={30} className="fd-user-avatar">{(session ? userLabel : "?").slice(0, 1).toUpperCase()}</Avatar>
              <div className="fd-user-copy">
                <strong>{userLabel}</strong>
                <span>{session ? ROLE_LABEL[session.role] : ""}</span>
              </div>
            </button>
          </Dropdown>
          <div className="fd-sidebar__tools">
            <ThemeToggle />
            <Tooltip title={collapseLabel} placement="right">
              <button type="button" className="fd-icon-button fd-sidebar__collapse" aria-label={collapseLabel} aria-pressed={collapsed} onClick={toggleCollapsed}>
                {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              </button>
            </Tooltip>
          </div>
        </div>
      </aside>
      <main className="fd-main">
        <header className="fd-mobilebar">
          <button type="button" className="fd-mobile-menu" aria-label="打开导航" aria-expanded={mobileOpen} onClick={() => setMobileOpen(true)}>
            <MenuOutlined />
          </button>
          <Brand />
        </header>
        <div className="fd-topbar-status"><CacheStatus /><UpdateBanner /></div>
        <div className="fd-content" id="fd-main-content" tabIndex={-1}>
          <Outlet />
        </div>
      </main>
    </div>
  );
}
