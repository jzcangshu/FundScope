import { useEffect, useState } from "react";
import { Button, Form, Select } from "antd";
import * as fundsApi from "@/api/funds";
import * as authApi from "@/api/auth";
import type { FundAccessMode, FundAccessOption } from "@/api/types";
import type { UserRole } from "@/utils/constants";
import { PageError, describeError } from "./feedback";

export interface FundAccessValues {
  fund_access_mode: FundAccessMode;
  fund_ids: number[];
}

export function normalizeFundAccess(role: UserRole, values: Partial<FundAccessValues>): FundAccessValues {
  const mode = role === "admin" ? "all" : values.fund_access_mode ?? "all";
  return { fund_access_mode: mode, fund_ids: mode === "selected" ? [...new Set(values.fund_ids ?? [])] : [] };
}

/** Fetch every page, including inactive products: authorization is independent of status. */
export async function loadFundAccessOptions(source: "catalog" | "permissions" = "catalog"): Promise<FundAccessOption[]> {
  const funds: FundAccessOption[] = [];
  let page = 1;
  while (true) {
    const response = await (source === "permissions" ? authApi.listPermissionFunds : fundsApi.list)({ page, page_size: 100 });
    funds.push(...response.data);
    if (funds.length >= response.meta.total) return funds;
    if (!response.data.length) throw new Error("产品列表不完整，请重试");
    page += 1;
  }
}

export function FundAccessFields({ role, source = "catalog" }: { role: UserRole; source?: "catalog" | "permissions" }) {
  const form = Form.useFormInstance();
  const mode = Form.useWatch("fund_access_mode", form) as FundAccessMode | undefined;
  const ids = (Form.useWatch("fund_ids", form) ?? []) as number[];
  const [funds, setFunds] = useState<FundAccessOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (role === "admin" || mode !== "selected") return;
    let active = true;
    setLoading(true);
    setError(null);
    void loadFundAccessOptions(source).then((rows) => { if (active) setFunds(rows); })
      .catch((caught) => { if (active) setError(describeError(caught, "产品列表加载失败")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [role, mode, source, retry]);

  if (role === "admin") return <p className="fd-form-note">管理员始终可访问全部产品，无需配置。</p>;
  const known = new Set(funds.map((fund) => fund.id));
  const options = funds.map((fund) => ({ value: fund.id, label: `${fund.name}${fund.product_code ? ` · ${fund.product_code}` : ""}${fund.status === "inactive" ? "（已停用）" : ""}` }));
  // Preserve grants when a page fails to load or a product was subsequently deleted.
  ids.filter((id) => !known.has(id)).forEach((id) => options.push({ value: id, label: `产品 #${id}` }));

  return <>
    <Form.Item label="可见产品" name="fund_access_mode" rules={[{ required: true, message: "请选择产品范围" }]}>
      <Select aria-label="可见产品范围" options={[{ value: "all", label: "全部产品（包括以后新增的产品）" }, { value: "selected", label: "指定产品" }]} />
    </Form.Item>
    {mode === "selected" && <>
      <Form.Item name="fund_ids" label="选择产品" extra={ids.length ? `已选择 ${ids.length} 只产品；查看、导出及后台操作均按此范围隔离。` : "未选择任何产品：该账号登录后无法查看或操作任何产品。"}>
        <Select mode="multiple" showSearch allowClear aria-label="选择可见产品" placeholder="搜索产品名称或代码，可多选" optionFilterProp="label" options={options} loading={loading} maxTagCount="responsive" />
      </Form.Item>
      <PageError message={error} onRetry={() => setRetry((value) => value + 1)} retrying={loading} />
      {!loading && !error && !funds.length && <p className="fd-form-note">暂无可分配的产品。</p>}
      {ids.length > 0 && <Button type="link" size="small" onClick={() => form.setFieldValue("fund_ids", [])}>清空选择</Button>}
    </>}
  </>;
}
