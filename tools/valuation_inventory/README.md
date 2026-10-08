# 历史估值文件只读盘点工具

该工具对指定历史目录递归扫描，计算 SHA-256（安全哈希），识别 `.xls`（旧版 Excel）/`.xlsx`（新版 Excel）中的估值表、产品和表内估值日期，并按主目录与 `gz`（归档目录）做去重和冲突分类。它只生成报告，绝不移动、重命名、覆盖或删除源文件。

它是历史迁移的盘点层，不是正式导入器：不写业务数据库、不发布估值版本、不执行分析、不修改 Excel。正式迁移通过 `backend/app/migration`（历史迁移模块）调用导入 API（接口）。

## 运行环境

项目后端要求 Python 3.12+（Python 运行时）。依赖由 `backend/pyproject.toml`（后端项目配置）声明，常用本地命令如下：

```powershell
Set-Location F:\AgentWorks\基金分析看板
\.venv\Scripts\python.exe -m pip install -e "backend[test]"
```

如果只需使用盘点功能，至少需要 `xlrd`（旧版 Excel 读取库）和 `openpyxl`（新版 Excel 读取库）；推荐直接安装项目测试依赖以保持版本一致。

## 命令

```powershell
\.venv\Scripts\python.exe -m tools.valuation_inventory `
  --root "F:\AgentWorks\估值表A" `
  --out "F:\AgentWorks\基金分析看板\artifacts\valuation-inventory" `
  --format all
```

参数：

| 参数 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--root` | 是 | 无 | 只读扫描根目录 |
| `--out` | 是 | 无 | 报告输出目录，不能是扫描根目录或其子目录 |
| `--format` | 否 | `all` | `json`、`csv`、`markdown` 或 `all` |
| `--workers` | 否 | `1` | 扫描并发线程数；结果排序保持确定性 |
| `--parse-xls` / `--no-parse-xls` | 否 | 开启 | 是否读取 `.xls` 内容；关闭后只做有限元数据盘点 |

工具会隔离单文件读取异常，尽量继续完成整目录扫描。返回码为 0 只表示扫描任务完成，不表示每个文件都已识别为可导入估值表；具体以报告中的 `parse_status`（解析状态）、`error_type`（错误类型）和 `action`（迁移动作）为准。

## 识别规则

### 文件和工作表

- 递归扫描文件，记录相对路径、文件名、扩展名、大小、修改时间、来源区域、哈希和解析状态。
- `.xls` 使用 `xlrd`，`.xlsx` 使用 `openpyxl` 只读模式；工作簿中寻找第一个能识别估值表表头的工作表。
- 估值表表头需要同时包含“科目代码”和“科目名称”，并且工作簿前部存在“估值日期”或强净值关键词。
- 交易记录 `.xlsx`、备份旁车文件和未知文件只做分类/哈希，不当作估值表导入。

### 产品和日期

- 产品候选来自目录名、文件名、工作表名称和表头附近标题文本。
- 默认目录包含梦溪一号、千帆一号、天衡上将；产品别名可以通过工具实现的配置扩展。
- 多个来源给出互相冲突的产品身份时标记 `identity_conflict`（身份冲突），不自动选择。
- 估值日期优先读取表内标签或日期单元格，支持 `20260402`、`2026-04-02`、`2026/04/02`、`2026年4月2日` 等形式。
- 同名跨年份文件以表内日期为准，文件名只用于辅助报告，不能拿来去重。

## 输出文件

`--format all` 会生成五份文件：

| 文件 | 内容 |
| --- | --- |
| `inventory.json` | 每个文件的完整清单和扫描汇总 |
| `inventory.csv` | 每文件一行的扁平清单，方便筛选 |
| `summary.md` | 统计、日期缺口、解析异常和复核提示 |
| `dedup-groups.json` | 重复组、冲突组、区域独有日期和同名跨区域对 |
| `migration-candidates.csv` | 后端迁移可消费的候选动作 |

报告只写相对 `--root` 的路径，不泄露本机绝对路径。生成报告不会改变源目录内容；读取动作可能刷新 Windows 文件系统访问时间，这是操作系统元数据行为，不是工具写入业务内容。

## 去重口径

去重键是“候选产品 + 表内估值日期 + SHA-256”。

| 分类 | 处理建议 |
| --- | --- |
| `same_content_duplicate` | 同产品、同日期、同哈希；主目录优先，其他副本跳过 |
| `same_date_conflict` | 同产品、同日期、哈希不同；全部标记人工复核，不自动选择 |
| `primary_only` | 主目录有、`gz` 无；可作为主目录候选 |
| `gz_only` | `gz` 有、主目录无；可作为补充候选，但须业务确认 |
| `unresolved_identity` | 产品、日期或哈希不可靠；人工复核 |

迁移模块会把可上传项归为 `import`（导入）或 `import_gz_only`（仅归档补充），把冲突归为 `needs_review`，把非估值和重复副本归为跳过项。这个动作分类不等于估值版本已经入库或发布。

## 测试和可重复性

测试全部使用临时目录生成的合成样本，不读取真实历史目录：

```powershell
\.venv\Scripts\python.exe -m pytest tools/valuation_inventory/tests -q
\.venv\Scripts\python.exe -m ruff check tools/valuation_inventory
\.venv\Scripts\python.exe -m ruff format --check tools/valuation_inventory
```

串行和并行扫描应产生相同的确定性报告排序。迁移前应保留本次清单指纹和报告，样本、冲突和覆盖率验收见 [历史数据迁移指南](../../docs/历史数据迁移指南.md)。
