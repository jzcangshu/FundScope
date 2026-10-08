import { describe, expect, it } from "vitest";
import { ANALYSIS_STATUS_LABEL, AUDIT_RESOURCE_TYPE_LABEL, RISK_RULE_TYPES, RISK_RULE_TYPE_LABEL, toAnalysisStatus } from "@/utils/constants";

describe("analysis status", () => {
  it("normalises unknown backend values to pending", () => {
    expect(toAnalysisStatus("ready")).toBe("ready");
    expect(toAnalysisStatus("stale")).toBe("stale");
    expect(toAnalysisStatus("pending")).toBe("pending");
    expect(toAnalysisStatus(undefined)).toBe("pending");
    expect(toAnalysisStatus("weird")).toBe("pending");
  });

  it("has a Chinese label for every state", () => {
    for (const key of ["ready", "pending", "stale"] as const) expect(ANALYSIS_STATUS_LABEL[key]).toMatch(/[一-龥]/);
  });
});

describe("label tables", () => {
  it("covers every supported risk rule type", () => {
    for (const type of RISK_RULE_TYPES) expect(RISK_RULE_TYPE_LABEL[type]).toBeTruthy();
  });

  it("covers the audit resource types written by the backend", () => {
    for (const type of ["user", "session", "authentication", "mail_sync", "export", "source_file", "risk_event", "analysis_run"]) {
      expect(AUDIT_RESOURCE_TYPE_LABEL[type]).toBeTruthy();
    }
  });
});
