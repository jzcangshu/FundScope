import type { UserRole } from "@/utils/constants";
import { can, canEditFundAccess, navForRole, NAV_ORDER } from "@/utils/permissions";

describe("role navigation", () => {
  it("shows the full navigation to system administrators", () => {
    expect(navForRole("admin")).toEqual([
      "dashboard",
      "risk",
      "funds",
      "imports",
      "reviews",
      "mail",
      "adminFunds",
      "adminSubjects",
      "adminRiskRules",
      "adminUsers",
      "accountPermissions",
      "adminAudit",
      "adminSettings",
      "adminRetention",
    ]);
  });

  it("shows operators every page their capabilities allow", () => {
    expect(navForRole("operator")).toEqual([
      "dashboard",
      "risk",
      "funds",
      "imports",
      "reviews",
      "mail",
      "adminFunds",
      "adminSubjects",
      "adminRiskRules",
    ]);
  });

  it("limits read-only users to view pages plus risk configuration", () => {
    expect(navForRole("viewer")).toEqual(["dashboard", "risk", "funds", "adminRiskRules"]);
  });

  it("never lists a menu entry whose route guard would reject the role", () => {
    for (const role of ["admin", "operator", "viewer", "boss"] as const) {
      for (const key of navForRole(role)) {
        const cap = key === "adminRiskRules" ? "riskConfig" : key;
        expect(can(role, cap as Parameters<typeof can>[1])).toBe(true);
      }
    }
  });

  it("grants management-only risk rule access to the read role", () => {
    expect(can("viewer", "manageRiskRules")).toBe(true);
    expect(can("viewer", "write")).toBe(false);
  });

  it.each(["all", "selected"] as const)("boss adds account permission inspection and product grants in %s mode", (mode) => {
    expect(navForRole("boss", mode)).toEqual([...navForRole("viewer", mode), "accountPermissions"]);
    for (const cap of [...NAV_ORDER.map((item) => item.cap), "write", "publish", "manageUsers", "manageSettings", "manageRiskRules", "manageFundAccess"] as const) {
      expect(can("boss", cap, mode)).toBe(["accountPermissions", "manageFundAccess"].includes(cap) || can("viewer", cap, mode));
    }
    expect(can("operator", "accountPermissions", mode)).toBe(false);
    expect(can("viewer", "accountPermissions", mode)).toBe(false);
  });

  it.each(["all", "selected"] as const)("boss can edit other non-admin accounts but not self in %s mode", (mode) => {
    const boss = { id: 2, role: "boss" as const, fund_access_mode: mode };
    for (const role of ["viewer", "operator", "boss"] as const) {
      expect(canEditFundAccess(boss, { id: 3, role })).toBe(true);
    }
    expect(canEditFundAccess(boss, { id: 1, role: "admin" })).toBe(false);
    expect(canEditFundAccess(boss, boss)).toBe(false);
    expect(canEditFundAccess({ ...boss, role: "viewer" }, { id: 3, role: "viewer" })).toBe(false);
    expect(canEditFundAccess({ ...boss, role: "operator" }, { id: 3, role: "viewer" })).toBe(false);
  });
});

describe("roles this build does not know about", () => {
  // Regression: the backend can introduce a role before a given desktop client is
  // updated. `can` used to read [cap] off an undefined MATRIX[role], which threw
  // "Cannot read properties of undefined (reading 'dashboard')" out of
  // navForRole -> Array.filter -> can, crashed AppLayout, and thereby unmounted the
  // update banner — leaving the user with no in-app way to upgrade out of the problem.
  const unknownRole = "auditor" as UserRole;

  it("resolves every capability to false instead of throwing", () => {
    for (const item of NAV_ORDER) {
      expect(() => can(unknownRole, item.cap)).not.toThrow();
      expect(can(unknownRole, item.cap)).toBe(false);
    }
    expect(can(unknownRole, "dashboard")).toBe(false);
    expect(can(unknownRole, "accountPermissions", "selected")).toBe(false);
  });

  it("yields an empty navigation instead of throwing, so the layout still renders", () => {
    expect(() => navForRole(unknownRole)).not.toThrow();
    expect(navForRole(unknownRole)).toEqual([]);
    expect(navForRole(unknownRole, "selected")).toEqual([]);
  });

  it("cannot be granted fund-access editing rights", () => {
    expect(canEditFundAccess({ id: 9, role: unknownRole }, { id: 3, role: "viewer" })).toBe(false);
  });
});
