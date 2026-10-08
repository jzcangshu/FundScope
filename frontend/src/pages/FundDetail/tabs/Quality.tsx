import { Skeleton } from "antd";
import * as fundsApi from "@/api/funds";
import type { ImportValidationFinding } from "@/api/types";
import { Disclosure, ErrorState, LevelTag, QualityBadge, Section, useAsyncData } from "@/components";
import { FindingTable } from "@/components/FindingTable";
import { dateStr } from "@/utils/format";
import { useAuth } from "@/app/auth";

/** `asOf` selects the published version of that exact date; without one the backend returns `version_id: null`. */
export function QualitySection({ fundId, asOf }: { fundId: number; asOf?: string }) {
  const { session } = useAuth();
  const state = useAsyncData(() => fundsApi.quality(fundId, asOf).then((result) => result.data), [fundId, asOf], "数据质量加载失败");
  const { data: quality, loading, error, reload } = state;

  if (error) {
    return (
      <Section id="quality" title="数据质量">
        <ErrorState message={error} onRetry={() => void reload()} retrying={loading} />
      </Section>
    );
  }
  if (!quality) {
    return (
      <Section id="quality" title="数据质量">
        <Skeleton active paragraph={{ rows: 4 }} />
      </Section>
    );
  }
  if (quality.version_id === null) {
    return (
      <Section id="quality" title="数据质量">
        <p className="fd-caption">{asOf ? "该日期没有已发布的估值，没有可展示的校验结果。" : "该产品尚无已发布的估值版本。"}</p>
      </Section>
    );
  }

  const critical = quality.validation.filter((finding) => finding.level === "critical");
  const warnings = quality.validation.filter((finding) => finding.level === "warning");
  const info = quality.validation.filter((finding) => finding.level === "info");
  const issues = [...critical, ...warnings];
  const isViewer = session?.role === "viewer" || session?.role === "boss";

  return (
    <Section id="quality" title="数据质量">
      <div className="fd-quality-summary">
        <div className="fd-quality-summary__head">
          <div className="fd-quality-summary__status">
            <QualityBadge status={quality.quality_status} />
            <strong>{issues.length ? `发现 ${issues.length} 项需要关注` : "校验通过"}</strong>
          </div>
          <span className="fd-quality-summary__date">估值日 <b className="num">{dateStr(quality.valuation_date)}</b></span>
        </div>
        <dl className="fd-quality-summary__counts">
          <div><dt>阻断级</dt><dd className="num">{critical.length}</dd></div>
          <div><dt>警告级</dt><dd className="num">{warnings.length}</dd></div>
          <div><dt>基础校验通过</dt><dd className="num">{info.length}</dd></div>
        </dl>
        {(issues.length === 0 || (info.length > 0 && isViewer)) && (
          <div className="fd-quality-summary__notes">
            {issues.length === 0 && <p>最新已发布版本没有阻断级或警告级问题。</p>}
            {info.length > 0 && isViewer && <p>资产负债、份额净资产、持仓市值等 {info.length} 项基础一致性检查均已通过。</p>}
          </div>
        )}
      </div>

      {(issues.length > 0 || (info.length > 0 && !isViewer)) && (
        <div className="fd-quality-details fd-stack">
          {issues.length > 0 && (isViewer ? <IssueList findings={issues} /> : <FindingTable findings={issues} />)}
          {info.length > 0 && !isViewer && (
            <Disclosure summary={`技术校验明细（${info.length} 项已通过）`}>
              <FindingTable findings={info} />
            </Disclosure>
          )}
        </div>
      )}
    </Section>
  );
}

/** Viewers see the findings as plain lines instead of the operator's table. */
function IssueList({ findings }: { findings: ImportValidationFinding[] }) {
  return (
    <div className="fd-stack fd-stack--tight">
      {findings.map((finding, index) => (
        <div className="fd-quality-issue" key={`${finding.rule_code}-${index}`}>
          <LevelTag level={finding.level} />
          <span>{finding.message || "未提供说明"}</span>
        </div>
      ))}
    </div>
  );
}
