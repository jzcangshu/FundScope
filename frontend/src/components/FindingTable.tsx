import { Table } from "antd";
import type { ImportValidationFinding } from "@/api/types";
import type { ValidationLevel } from "@/utils/constants";
import { dec } from "@/utils/format";
import { Chip, Compare, LevelTag, Truncate } from "./data";
import { EmptyState } from "./feedback";

/** Shared validation-finding table used by the review drawer and the fund quality section. */
export function FindingTable({ findings, showHandling = true, compact = false }: { findings: ImportValidationFinding[]; showHandling?: boolean; compact?: boolean }) {
  if (!findings.length) return <EmptyState compact hint="这条版本没有可展示的校验发现。" />;
  return (
    <Table<ImportValidationFinding>
      className="fd-finding-table"
      rowKey={(finding, index) => `${finding.rule_code}-${index ?? 0}`}
      size="small"
      pagination={false}
      dataSource={findings}
      scroll={{ x: (compact ? 820 : 950) + (showHandling ? (compact ? 150 : 160) : 0) }}
      columns={[
        {
          title: "等级",
          dataIndex: "level",
          width: compact ? 80 : 100,
          render: (value: ValidationLevel, finding) => (
            <span className="fd-inline-actions">
              <LevelTag level={value} />
              {finding.ignored && <Chip>已忽略</Chip>}
            </span>
          ),
        },
        { title: "规则", dataIndex: "rule_code", width: compact ? 140 : 170, render: (value: string) => <span className="fd-code fd-code--quiet">{value}</span> },
        { title: "说明", dataIndex: "message", width: compact ? 280 : 320, className: "fd-finding-table__message", render: (value: string) => value || "未提供说明" },
        {
          title: "数值对比",
          width: compact ? 170 : 190,
          render: (_, finding) => <Compare actual={dec(finding.actual_value, 2)} expected={dec(finding.expected_value, 2)} difference={dec(finding.difference, 2)} />,
        },
        { title: "来源位置", dataIndex: "source_location", width: compact ? 150 : 170, render: (value: string | null) => <Truncate value={value ?? "未记录"} maxChars={64} /> },
        ...(showHandling
          ? [{
              title: "处理记录",
              width: compact ? 150 : 160,
              render: (_: unknown, finding: ImportValidationFinding) => (
                <span className="fd-caption">
                  {finding.ignored ? finding.ignored_reason || "人工发布时忽略" : finding.level === "info" ? "无需处理" : "待人工处理"}
                </span>
              ),
            }]
          : []),
      ]}
    />
  );
}
