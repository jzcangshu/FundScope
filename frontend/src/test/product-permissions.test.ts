import { can, isProductScoped, navForRole } from "@/utils/permissions";

describe("product-scoped capabilities", () => {
  it.each(["viewer", "operator", "boss"] as const)("hides global configuration and ingestion for a selected-products %s", (role) => {
    expect(isProductScoped({ role, fund_access_mode: "selected" })).toBe(true);
    for (const cap of ["mail", "adminSubjects", "riskConfig", "manageRiskRules", "adminRiskRules"] as const) {
      expect(can(role, cap, "selected")).toBe(false);
    }
    expect(navForRole(role, "selected")).not.toContain("adminRiskRules");
    expect(navForRole(role, "selected")).not.toContain("mail");
  });

  it("keeps authorized product maintenance, imports and reviews for operators", () => {
    expect(navForRole("operator", "selected")).toEqual(["dashboard", "risk", "funds", "imports", "reviews", "adminFunds"]);
    expect(can("operator", "write", "selected")).toBe(true);
    expect(can("operator", "publish", "selected")).toBe(true);
    expect(can("viewer", "write", "selected")).toBe(false);
    expect(navForRole("viewer", "selected")).toEqual(["dashboard", "risk", "funds"]);
  });

  it("keeps administrators unrestricted and preserves all-products account capabilities", () => {
    expect(isProductScoped({ role: "admin", fund_access_mode: "selected" })).toBe(false);
    expect(navForRole("admin", "selected")).toEqual(navForRole("admin", "all"));
    for (const role of ["viewer", "operator", "admin"] as const) {
      expect(navForRole(role, "all")).toEqual(navForRole(role));
    }
  });
});
