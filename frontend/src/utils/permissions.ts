import type { FundAccessMode } from "@/api/types";
import type { UserRole } from "./constants";

/**
 * Permission model mirrors the role matrix in docs/产品与功能范围.md.
 * viewer: read-only everywhere except the explicitly granted risk-rule configuration.
 * boss: viewer capabilities plus account permission inspection and product grants.
 * operator: + upload/import, review/publish, data operations, catalog & risk config.
 * admin: everything incl. user management & system settings.
 *
 * The backend re-validates every request; this matrix only shapes the UI.
 */
const VIEWER_CAPABILITIES = {
  dashboard: true,
  risk: true,
  riskConfig: true,
  manageRiskRules: true,
  funds: true,
  imports: false,
  reviews: false,
  mail: false,
  adminFunds: false,
  adminSubjects: false,
  adminRiskRules: false,
  adminUsers: false,
  adminAudit: false,
  adminSettings: false,
  adminRetention: false,
  write: false,
  publish: false,
  manageUsers: false,
  manageSettings: false,
  accountPermissions: false,
  manageFundAccess: false,
} as const;

const MATRIX = {
  admin: {
    dashboard: true,
    risk: true,
    riskConfig: true,
    manageRiskRules: true,
    funds: true,
    imports: true,
    reviews: true,
    mail: true,
    adminFunds: true,
    adminSubjects: true,
    adminRiskRules: true,
    adminUsers: true,
    adminAudit: true,
    adminSettings: true,
    adminRetention: true,
    write: true,
    publish: true,
    manageUsers: true,
    manageSettings: true,
    accountPermissions: true,
    manageFundAccess: true,
  },
  operator: {
    dashboard: true,
    risk: true,
    riskConfig: true,
    manageRiskRules: true,
    funds: true,
    imports: true,
    reviews: true,
    mail: true,
    adminFunds: true,
    adminSubjects: true,
    adminRiskRules: true,
    adminUsers: false,
    adminAudit: false,
    adminSettings: false,
    adminRetention: false,
    write: true,
    publish: true,
    manageUsers: false,
    manageSettings: false,
    accountPermissions: false,
    manageFundAccess: false,
  },
  viewer: VIEWER_CAPABILITIES,
  boss: { ...VIEWER_CAPABILITIES, accountPermissions: true, manageFundAccess: true },
} as const;

export type Capability = keyof (typeof MATRIX)[UserRole];

type AccessSession = { role: UserRole; fund_access_mode?: FundAccessMode };

export function isProductScoped(session: AccessSession | null | undefined): boolean {
  return Boolean(session && session.role !== "admin" && session.fund_access_mode === "selected");
}

const GLOBAL_CAPABILITIES = new Set<Capability>(["mail", "adminSubjects", "adminRiskRules", "riskConfig", "manageRiskRules"]);

/** Every role this build ships with, derived from the matrix so the two cannot drift. */
const KNOWN_ROLES = new Set<string>(Object.keys(MATRIX));

export function can(role: UserRole | undefined, cap: Capability, mode?: FundAccessMode): boolean {
  // The backend can introduce a role before a given desktop client is updated, so an
  // unknown role must degrade to "no permissions" rather than throw. Reading [cap] off
  // an undefined MATRIX[role] used to crash AppLayout — which also unmounted the update
  // banner and left the user with no in-app way to upgrade out of the problem.
  if (!role || !KNOWN_ROLES.has(role)) return false;
  if (role !== "admin" && mode === "selected" && GLOBAL_CAPABILITIES.has(cap)) return false;
  return MATRIX[role][cap] ?? false;
}

export function canEditFundAccess(
  actor: (AccessSession & { id: number }) | null | undefined,
  target: { id: number; role: UserRole },
): boolean {
  return Boolean(actor && can(actor.role, "manageFundAccess", actor.fund_access_mode)
    && target.role !== "admin" && (actor.role === "admin" || actor.id !== target.id));
}

/** Navigation keys in menu order; each key maps to the capability that guards its route. */
export const NAV_ORDER: Array<{ key: string; cap: Capability }> = [
  { key: "dashboard", cap: "dashboard" },
  { key: "risk", cap: "risk" },
  { key: "funds", cap: "funds" },
  { key: "imports", cap: "imports" },
  { key: "reviews", cap: "reviews" },
  { key: "mail", cap: "mail" },
  { key: "adminFunds", cap: "adminFunds" },
  { key: "adminSubjects", cap: "adminSubjects" },
  { key: "adminRiskRules", cap: "riskConfig" },
  { key: "adminUsers", cap: "adminUsers" },
  { key: "accountPermissions", cap: "accountPermissions" },
  { key: "adminAudit", cap: "adminAudit" },
  { key: "adminSettings", cap: "adminSettings" },
  { key: "adminRetention", cap: "adminRetention" },
];

/** Menu entries derived from the capability matrix so the menu can never disagree with route guards. */
export function navForRole(role: UserRole, mode?: FundAccessMode): string[] {
  return NAV_ORDER.filter((item) => can(role, item.cap, mode)).map((item) => item.key);
}
