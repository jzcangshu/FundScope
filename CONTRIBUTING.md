# 贡献指南

欢迎参与 FundScope 的开发。本仓库是私有基金估值运营看板的开源形态，仓库内的所有产品名、机构名和域名均为虚构示例，不代表真实基金或企业——请勿引入真实客户、基金或企业信息。

## 报告问题

- **Bug 或功能建议**：在 Issues 中描述复现步骤、期望行为和实际行为；涉及页面时附截图，涉及接口时附请求/响应片段（先抹掉敏感字段）。
- **安全漏洞**：请遵循 `SECURITY.md`，不要公开披露。

## 本地开发

环境要求：Python 3.12+、Node.js 20.19+。快速起步见 `README.md` 的「本地开发」，仓库约定与质量门禁见 `AGENTS.md`。

## 质量门禁（提交前必跑）

与 CI（`.github/workflows/quality.yml`）等价：

```powershell
\.venv\Scripts\python.exe -m ruff check backend tools/valuation_inventory
\.venv\Scripts\python.exe -m ruff format --check backend tools/valuation_inventory
\.venv\Scripts\python.exe -m ty check backend/app
\.venv\Scripts\python.exe -m pytest backend/tests -q
\.venv\Scripts\python.exe -m pytest tools/valuation_inventory/tests -q

Set-Location frontend
npm run typecheck
npm test -- --run
npm run build
```

## 代码约定

- 业务数据唯一来自估值 Excel；产品策略、别名、科目映射、风险规则和账号是人工维护配置。
- 新增或修改路由、权限、数据字段、指标口径或状态机时，必须在同一个提交里更新 `docs/` 中的对应文档与测试。
- 不提交秘密：`.env`、私钥、授权码、Cookie、数据库备份、原始 Excel 源文件或生产数据。示例凭据一律使用占位符（如 `fundscope.example.com`）。
- 产品名、机构名保持虚构示例命名；不要以「此处原为…」的方式留下任何真实信息痕迹。
- 遵循仓库既有的命名与注释风格；新文件先看同目录同类文件的写法。

## 提交与 PR

- 提交信息说明「为什么」而非「改了什么」；一个提交只做一件事。
- PR 前确认已通过上面的门禁，并在 PR 描述中注明验证结果。
- 涉及数据库迁移的变更，必须提供可降级的迁移与迁移测试。
