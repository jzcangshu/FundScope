# FundScope 贡献指南

面向私有基金估值运营场景的开源看板：接收日常估值 Excel，在服务端完成归档、标准化、质量校验、自动发布/人工复核、后台分析和风险事件生成，再通过网页看板和 CSV 导出提供结果，并附一个 Windows 桌面客户端（Tauri 2）。

本文件面向开发者与 AI 协作代理，描述仓库约定与质量门禁。通用介绍见 `README.md`，长期文档见 `docs/README.md`。

## 仓库结构

- `backend/app/` — FastAPI 应用：路由、导入/解析/校验/发布、分析、配置、系统运维
- `backend/alembic/` — 数据库迁移
- `backend/tests/` — 后端测试（路由、导入、解析、迁移、e2e）
- `frontend/` — React + TypeScript + Vite 前端，以及 Tauri 桌面外壳（`src-tauri/`）
- `tools/valuation_inventory/` — 只读历史文件盘点工具（独立测试套件）
- `deploy/` — Docker Compose、Caddy 配置和初始化模板
- `docs/` — 权威文档（产品范围、UX、架构、API、迁移、运行手册）

## 本地开发

环境要求：Python 3.12+、Node.js 20.19+，可选 Docker（生产部署）。

后端（Windows PowerShell 示例，其他平台用对应 venv 路径）：

```powershell
python -m venv .venv
\.venv\Scripts\python.exe -m pip install -e "backend[test]"
\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload
```

开发默认使用本地 SQLite（`data/dev.db`）。首次做界面联调时写入演示数据：

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m app.dev_seed
```

该脚本生成 15 只虚构产品、90 个估值日的版本、快照、持仓、指标与风险样例，产品代码带 `DEMO-` 前缀，重复执行自动跳过。所有产品名均为虚构示例名，不代表真实基金。

前端（另开终端）：

```powershell
Set-Location frontend
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173`，Vite 把 `/api` 与 `/health` 代理到 `http://127.0.0.1:8000`。

## 质量门禁（提交前必跑）

与 `.github/workflows/quality.yml` 等价，推送前本地跑全。**后端两步格式检查必须都跑**，只跑 `ruff check` 会漏掉格式失败。

```powershell
Set-Location <repo root>
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

- `ty check` 的路径参数是 `backend/app`，不是整个 `backend` 或仓库根。
- `ruff` / `ty` 在 `backend/pyproject.toml` 里锁定精确版本，`.pre-commit-config.yaml` 的钩子修订号与之对应；升级时两处必须同改。
- 可选：安装 `.pre-commit-config.yaml` 钩子把检查挂到 `git commit` 上（`ty` 钩子要求虚拟环境已激活，否则报 `No module named ty`；临时跳过钩子用 `git commit --no-verify`）。

## 仓库约定

- 业务数据只来自估值 Excel。产品策略、管理人、别名、份额类别、科目映射、风险规则和账号是人工维护配置。
- 新增或修改路由、权限、数据字段、指标口径、状态机时，必须在同一个提交中更新对应权威文档（`docs/`）与测试。
- 不提交秘密：`.env`、私钥（`*.pem`/`*.key`）、授权码、Cookie、数据库备份、原始 Excel 源文件或生产数据。
- 示例凭据、域名与密码一律使用占位符（如 `fundscope.example.com`）。
- 代码与文档中的产品名、机构名必须保持虚构示例，不得引入真实基金或企业信息。
