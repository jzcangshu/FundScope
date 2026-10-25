# 后端 API 接口说明

本文是当前版本后端接口的调用说明，依据 `backend/app/api` 的实际路由和运行时 OpenAPI（接口描述）生成结果整理。接口前缀为 `/api/v1`；部署后可访问 `/docs`（交互式接口文档）和 `/openapi.json`（机器可读接口描述）查看运行实例的最终契约。

本文中的接口状态只有两种：

- **已实现**：当前代码已提供，本文描述的路径和字段可以作为调用依据。
- **未提供**：当前没有路由，客户端不得发送占位请求。

## 1. 调用约定

### 1.1 会话

登录成功后服务端设置名为 `fund_session` 的 Cookie（浏览器会话凭据）。浏览器或客户端必须携带 Cookie，不使用 `Authorization: Bearer`（令牌授权头）。生产环境 Cookie 具备 HttpOnly（脚本不可读取）、Secure（仅 HTTPS 发送）、SameSite=Lax（限制跨站发送）属性。

前端使用同源请求并携带凭据。API 客户端应把 `401` 视为会话失效，清理本地会话状态并回到登录页；不能把 `403` 当成未登录。

### 1.2 JSON 响应

普通成功响应使用：

```json
{
  "data": {},
  "meta": {}
}
```

`meta` 不是每个接口都有。分页查询通常返回：

```json
{
  "meta": {
    "page": 1,
    "page_size": 20,
    "total": 0
  }
}
```

金额、净值、收益率、权重和阈值在自定义 JSON 响应中通常以十进制字符串返回，避免 JavaScript（网页脚本语言）浮点误差；日期为 `YYYY-MM-DD`，时间为 ISO 8601（国际日期时间格式）。登出成功返回 `204 No Content`。

### 1.3 错误

- `400`：请求语义错误，例如密码错误或空更新。
- `401`：没有有效会话。
- `403`：角色没有该接口权限。
- `404`：资源不存在或原始文件不可访问。
- `409`：状态机、唯一性、并发或初始化条件不允许。
- `413`：上传超过单文件大小限制。
- `422`：请求体、查询参数或日期范围不符合校验规则。
- `502/503`：邮件连接或系统依赖暂不可用。

FastAPI 默认的参数校验错误为 `422`，响应体含 `detail` 数组。业务错误通常为 `{"detail": "..."}`；客户端应展示安全的 `detail`，不要自行推断不存在的错误字段。

### 1.4 角色缩写

| 角色 | 含义 |
|---|---|
| `admin` | 系统管理员 |
| `operator` | 业务员 |
| `viewer` | 普通看板；除风险规则配置外只读 |
| 登录用户 | `admin`、`operator`、`viewer` 任一有效会话 |

后端鉴权是最终边界，前端隐藏按钮不能替代接口权限。账号产品范围也会在服务端验证：`all` 为全部产品，`selected` 为 `fund_ids` 指定的产品（空数组表示无产品）。集合接口先过滤再分页、统计；越权单产品接口返回 `404`；受限账号访问跨产品全局功能返回 `403`。所有 API 响应带 `Cache-Control: no-store, private`。

## 2. 健康和认证

### `GET /health/live`（公开存活检查）

无需登录。返回固定的非敏感信息：

```json
{"status":"ok","service":"fund-dashboard-api"}
```

它只表示 API 进程存活，不检查数据库、worker（后台任务进程）或邮件。

### `GET /api/v1/auth/status`（初始化状态）

无需登录。返回数据库中是否已经存在账号：

```json
{"data":{"initialized":true}}
```

### `POST /api/v1/auth/initialize`（初始化第一个管理员）

无需登录，仅在还没有任何账号时成功。请求体：

```json
{
  "username": "admin",
  "password": "至少 8 个字符",
  "display_name": "系统管理员"
}
```

`username` 长度 1--100，`password` 长度 8--256，`display_name` 可空且最多 255。成功返回 `201` 和当前用户对象，并设置登录 Cookie。初始化完成后再次调用返回 `409`。

当前用户对象字段：`id`、`username`、`display_name`、`role`、`status`、`last_login_at` 、`navigation`、`fund_access_mode` 和 `fund_ids`。初始化接口不接受角色，创建的账号固定为 `admin`。

### `POST /api/v1/auth/login`（登录）

无需登录。请求体：

```json
{"username":"admin","password":"..."}
```

用户名长度 1--100，密码长度 1--256。成功返回 `200`、用户对象并设置会话 Cookie；账号不存在、密码错误、账号禁用或账号暂时锁定都使用安全的通用失败语义，不暴露账号是否存在。连续失败达到实现阈值后会暂时锁定账号。

### `POST /api/v1/auth/logout`（退出登录）

需要登录。服务端吊销当前会话并清除 Cookie，成功返回 `204`。

### `GET /api/v1/auth/me`（当前用户）

需要登录。返回当前用户对象和前端导航提示。`navigation` 只用于页面显示，不能当作权限判断。

### `POST /api/v1/auth/change-password`（修改当前密码）

需要登录。请求体：

```json
{
  "old_password": "当前密码",
  "new_password": "新密码"
}
```

新密码长度 8--256。旧密码错误返回 `400`；成功返回 `{"data":{"changed":true}}`。服务端会按认证服务规则处理当前账号的其他会话。

## 3. 用户管理

以下接口全部只允许 `admin`。

### `GET /api/v1/users`（用户列表）

查询参数：

| 参数 | 类型 | 默认/范围 | 说明 |
|---|---|---|---|
| `q` | 字符串 | 可选，最多 100 | 按用户名包含匹配 |
| `role` | 枚举 | 可选 | `admin`、`operator`、`viewer` |
| `status` | 枚举 | 可选 | `active`、`disabled` |
| `page` | 整数 | 1 | 页码 |
| `page_size` | 整数 | 20，1--100 | 每页数量 |

返回用户对象数组和分页 `meta`。密码、密码哈希、会话令牌都不会返回。

### `POST /api/v1/users`（创建用户）

请求体：

```json
{
  "username": "operator01",
  "password": "至少 8 个字符",
  "role": "operator",
  "display_name": "业务员"
}
```

用户名 1--100，密码 8--256，角色必须是三个枚举值之一。成功返回 `201` 用户对象；用户名冲突返回 `409`。

### `POST /api/v1/users/{user_id}/disable`（禁用用户）

请求体可省略，也可传：`{"reason":"原因"}`。原因最多 2000 字符。返回更新后的用户对象。受保护的管理员或不存在的用户可能返回 `409` 或 `404`。

### `POST /api/v1/users/{user_id}/enable`（启用用户）

无需请求体。返回更新后的用户对象。

### `POST /api/v1/users/{user_id}/reset-password`（重置密码）

请求体：`{"password":"至少 8 个字符","reason":"可选原因"}`。密码长度 8--256，原因最多 2000。成功返回用户对象；不存在返回 `404`。

### `PATCH /api/v1/users/{user_id}/role`（修改角色）

请求体：`{"role":"viewer","reason":"可选原因"}`。成功返回用户对象；受保护账号或不存在返回 `409`/`404`。

### `POST /api/v1/users/{user_id}/revoke-sessions`（撤销全部会话）

请求体可省略，也可传可选原因：`{"reason":"原因"}`。成功返回用户对象，不返回任何令牌。

## 4. 看板查询

以下接口允许所有登录用户。它们只读取活动产品的 `published`（已发布）估值版本。

### `GET /api/v1/dashboard/overview`（公司总览）

查询参数：`as_of` 可选，格式 `YYYY-MM-DD`。未传时每个活动产品取各自最新已发布版本；传入时只取该精确估值日，不向前回退。

`data` 字段：

| 字段 | 说明 |
|---|---|
| `as_of` | 请求的估值日，未传时为 `null` |
| `total_net_assets` | 当前选中已发布快照的净资产合计，无快照时为 `null` |
| `fund_count` | 有已发布版本的活动产品数 |
| `company_index` | 分析已就绪时的公司指数，否则为 `null` |
| `company_daily_return` | 分析已就绪时的公司日收益，否则为 `null` |
| `risk_event_count` | 状态为 `open` 或 `acknowledged` 的风险事件数；传入 `as_of` 时只统计该估值日的事件 |
| `quality_status` | 当前实现返回 `valid` 或 `warning`（有任一产品含阻断或警告级发现时为 `warning`，否则为 `valid`） |
| `funds` | 各产品摘要数组 |

每个 `funds` 项包含：`id`、`name`、`valuation_date`、`total_assets`、`net_asset_value`、`unit_nav`、`daily_return`、`analysis_status` 和 `analysis_run_id`。`meta` 还包含 `coverage.available`、`coverage.total`、`analysis_status` 和 `analysis_run_id`。

分析状态为 `ready`（就绪）、`pending`（等待）或 `stale`（过期）。公司指数不是外部市场基准。

### `GET /api/v1/dashboard/series`（公司净值与回撤序列）

查询参数：`start`、`end` 可选，均为日期。窗口跨度不能超过 5 年（超出返回 `422`），`start` 晚于 `end` 返回 `422`；未传参数时默认窗口为最新估值日前 365 天。

返回：

```json
{
  "data": {
    "methodology": "daily_nav_weighted_index",
    "points": [
      {
        "valuation_date": "2026-08-25",
        "company_index": "1.0500000000",
        "company_daily_return": "0.0040",
        "drawdown": "-0.0200",
        "total_net_assets": "90000.0000000000",
        "effective_fund_count": 3
      }
    ]
  },
  "meta": {
    "coverage": {"available": 1, "total": 1},
    "start": "2025-08-26",
    "end": "2026-08-25",
    "max_drawdown": "-0.0200",
    "current_drawdown": "-0.0200",
    "analysis_status": "ready"
  }
}
```

每个估值日取最新一次成功分析运行的公司指标；只纳入当天存在活动产品已发布版本的日期。`analysis_status` 的语义与总览一致：公司范围分析运行等待中为 `pending`，最近一次失败为 `stale`（此时 `points` 为空且不返回过期组合结果），可正常取数时为 `ready`。数据库尚无已发布估值时返回空 `points` 且 `analysis_status` 为 `pending`。

### `GET /api/v1/dashboard/fund-drawdowns`（产品区间回撤）

查询参数：`range`（范围）为 `1m`（近一月）、`3m`（近三月）、`ytd`（今年）、`1y`（近一年）或 `all`（全部）；`as_of`（精确估值日）可选。返回各产品的实际起止日期、目标起点、区间当前回撤、最大回撤及数据状态。

“今年”以结束估值日所在年份的 1 月 1 日为目标起点，优先包含当天或此前最近一条估值。缺少该基准时，从当年首条已有估值起算，并返回 `status=incomplete`（历史不完整）、实际 `start`（起点）及 `reason`（原因），明确“不代表完整区间”。首条净值无效时仍不可用，区间内部缺失保留断点；其他固定区间不自动缩短历史。

产品详情的今年回撤图、净值明细和 `/api/v1/exports/funds/{fund_id}/nav-series`（净值序列导出）采用同一规则。导出指定 `range=ytd`（今年）时，`X-Drawdown-Status`（回撤数据状态）和 `X-Drawdown-Start`（实际起点）记录上述限制。“今年收益”仍沿用原有收益基准，不用部分历史冒充全年收益。该调整不修改已保存的历史指标或风险事件。

### `GET /api/v1/funds`（产品分析列表）

查询参数：

| 参数 | 类型 | 默认/范围 |
|---|---|---|
| `q` | 字符串 | 可选，最多 255，匹配标准名称 |
| `status` | 枚举 | 可选，`active` 或 `inactive` |
| `as_of` | 日期 | 可选，精确估值日 |
| `page` | 整数 | 1 起 |
| `page_size` | 整数 | 20，1--100 |

每项返回 `id`、`name`、`product_code`、`status`、`current_version_id`、`valuation_date`、`unit_nav`、`daily_return`、`quality_status`、`analysis_status` 和 `analysis_run_id`。没有符合日期的已发布版本时相关值为 `null`，不会回退到更早日期。

### `GET /api/v1/funds/{fund_id}`（产品详情摘要）

查询参数：可选 `as_of` 精确估值日。返回产品人工维护主数据、别名、份额类别主数据，以及当前已发布版本摘要：

```text
id, name, product_code, strategy, manager, establishment_date, notes,
aliases, share_classes, status, current_version_id, valuation_date,
quality_status, analysis_status, analysis_run_id,
total_assets, net_asset_value, unit_nav, cumulative_unit_nav,
daily_return, cumulative_return,
period_returns: { daily, wtd, mtd, qtd, ytd, cumulative }
```

`strategy`（策略）和 `manager`（管理人）如果存在，是人工目录字段，不代表从 Excel 自动识别。不存在产品返回 `404`。

### `GET /api/v1/funds/{fund_id}/nav-series`（净值序列）

查询参数：`start`、`end` 可选，均为日期；当前实现不允许 `start` 晚于 `end` 的请求，窗口跨度不能超过 5 年（超出返回 `422`）。未传参数时默认返回最近 365 天；只传 `end` 时最多保留结束日前 5 年的有界历史。返回：

```json
{
  "data": {
    "methodology": "cumulative_unit_nav",
    "total_return": "0.1234",
    "points": [
      {
        "valuation_date": "2026-08-24",
        "unit_nav": "1.1000",
        "cumulative_unit_nav": "1.2000",
        "cumulative_payout": "0.0000",
        "adjusted_nav": "1.2000",
        "daily_return": "0.0100",
        "cumulative_return": "0.2000",
        "analysis_status": "ready",
        "analysis_run_id": 12,
        "metric_source": "persisted"
      }
    ]
  },
  "meta": {
    "coverage": {"available": 1, "total": 1},
    "analysis_status": "ready",
    "metric_source": "persisted"
  }
}
```

`methodology` 可能是 `cumulative_unit_nav`、`unit_nav_plus_cumulative_payout`、`unit_nav` 及其不完整变体。`metric_source` 可能是 `persisted`、`calculated_fallback`、`mixed` 或 `none`。不存在产品返回 `404`。

### `GET /api/v1/funds/{fund_id}/positions`（持仓分页）

查询参数：`as_of` 可选；`page` 默认 1；`page_size` 默认 50，范围 1--200。后端固定按市值降序、记录编号升序排列。

每项返回：`security_code`、`security_name`、`market`、`account`、`quantity`、`market_price`、`market_value`、`nav_weight` 和 `suspension_info`。市场和账户允许为空。`meta` 包含分页字段和实际 `valuation_date`。不存在产品返回 `404`，存在产品但没有该日期版本返回空数组。

### `GET /api/v1/funds/{fund_id}/quality`（数据质量）

查询参数：可选 `as_of`。没有版本时返回 `version_id: null`、空 `validation` 和 `quality_status: pending`。

有版本时返回 `version_id`、`valuation_date`、`quality_status` 和校验数组。每条校验包含：`rule_code`、`level`、`actual_value`、`expected_value`、`difference`、`source_location`、`message`。级别为 `critical`、`warning` 或 `info`。

## 5. 导入和原始文件

以下接口只允许 `admin` 和 `operator`。系统支持逐个接收 `.xls` 和 `.xlsx`，不实现分片上传。

### `POST /api/v1/imports`（创建导入批次）

请求体可只传来源类型：

```json
{"source_type":"upload"}
```

`source_type` 可为 `upload`、`email`、`migration`、`other`，默认 `upload`。返回 `201`：`id`、`source_type`、`file_count`、`status`、`created_at`。

### `POST /api/v1/imports/{batch_id}/files`（上传一个文件）

使用 `multipart/form-data`（表单文件上传），字段名必须为 `file`。默认最大文件大小 20 MiB（兆字节），由 `MAX_UPLOAD_BYTES` 配置但不能超过 20 MiB。

成功返回 `201`：`id`、`original_filename`、`file_hash`、`file_size`、`duplicate`。文件会先写临时目录，校验扩展名和文件头，计算 SHA-256（安全哈希），再以服务端随机对象名保存。相同哈希只建立批次关联，不复制原始文件。

可能返回 `400`（文件类型/文件头无效）、`404`（批次不存在）、`409`（批次状态不允许或关联冲突）和 `413`（文件过大）。

### `POST /api/v1/imports/{batch_id}/complete`（完成上传）

无需请求体。服务端确认批次后创建 `process_import_batch`（导入处理）后台任务并返回批次和任务摘要。调用后由 worker 负责解析、标准化和校验。校验同时无 `critical`（阻断级）和 `warning`（警告级）的版本会在该批次处理事务内自动发布；含警告的版本保持 `publishable`，含阻断级发现的版本进入 `pending_review`。批次处理完成后，每个受影响产品最多创建一个覆盖本批次自动发布估值日范围的分析任务。

### `GET /api/v1/imports`（导入批次列表）

查询参数：`source_type`、`status`、`page`、`page_size`。`status` 可为 `created`、`queued`、`processing`、`completed`、`failed`；分页范围为 1--100。

每项包含批次字段以及 `job`。任务字段包含 `id`、`type`、`status`、`attempts`、`max_attempts`、`locked_at`、`started_at`、`finished_at`、`error_code`、`next_retry_at` 和 `can_retry`。不会返回租约令牌。

### `GET /api/v1/imports/{batch_id}`（导入批次详情）

返回批次基本字段、`files` 和 `job`。文件项为 `id`、`original_filename`、`file_hash`、`file_size`、`duplicate`。产品识别详情和完整处理日志不在此响应。

### `POST /api/v1/imports/{batch_id}/retry`（重试技术失败）

无需请求体。只允许批次和任务都处于失败状态，且错误编号属于 `batch_processing_failed` 或 `max_attempts_exceeded`。成功会重置任务并重新排队；业务校验失败不是这个接口的重试语义。

### `GET /api/v1/imports/{batch_id}/validations`（批次校验）

返回该批次产生的估值版本数组。每项包含 `version_id`、`fund_id`、`valuation_date`、`status` 和 `findings`。`findings` 的字段与产品质量接口相同；`meta.version_count` 表示版本数量。

### `GET /api/v1/imports/{batch_id}/source/{source_file_id}`（受控下载原始文件）

服务端校验批次与文件的关联、原始对象是否仍在配置根目录内，以及当前用户角色。成功返回原始文件流，并记录 `import.source_download` 审计日志。不存在、被清理或路径不安全时统一返回 `404`。原始文件名只用于下载响应名，不参与服务端路径拼接。

## 6. 复核和估值版本发布

以下接口只允许 `admin` 和 `operator`。

### `GET /api/v1/reviews`（复核队列）

查询参数：`status` 默认为 `pending_review`，可传完整估值版本状态枚举；`page` 默认 1，`page_size` 默认 20，范围 1--100。

每项返回摘要：`id`（此处即 `version_id`）、`fund_id`、`fund_name`、`valuation_date`、`version_no`、`status`、`critical_count`、`warning_count`、`ignored_count`、`source_file_id`、`source_filename`、`source_file_hash`、`source_file_size`、`import_batch_id`，以及与产品质量接口相同结构的 `findings` 数组。

### `POST /api/v1/reviews/{version_id}/acknowledge`（记录复核决定）

请求体：

```json
{"allow_publish":true,"note":"已核对原表"}
```

`note` 长度 1--2000。只有 `pending_review` 版本可调用；允许发布时变为 `publishable`，否则变为 `rejected`。返回 `version_id` 和新状态。它记录复核意见，但不会直接把版本变成 `published`。

### `POST /api/v1/valuations/{version_id}/publish`（发布）

请求体可省略字段：

```json
{"reason":"可选发布原因","confirm_warnings":true}
```

只有 `publishable` 版本可发布。阻断级校验不能发布；存在警告时必须显式 `confirm_warnings: true`。服务端锁定产品、将同产品同估值日旧已发布版本变为 `superseded`（已替代），再发布当前版本，并创建分析任务。导入批次中的干净版本不需要调用此接口，已由批处理自动发布；本接口主要用于人工确认含警告版本，或其他已进入 `publishable` 状态的版本。

返回：`version_id`、`fund_id`、`valuation_date`、`superseded_version_ids`、`analysis_run_id` 和 `validation_ignored_count`（发布时被忽略的阻断级发现数量）。状态冲突和校验不通过返回 `409`。

### `POST /api/v1/reviews/batch-publish`（批量发布 publishable 队列）

只允许 `admin` 和 `operator`。请求体：

```json
{"reason":"已核对警告"}
```

`reason` 长度 1--2000。当前 publishable 队列中的所有版本会被并发发布；含阻断级校验的版本会被跳过并计入失败。返回 `requested`、`published`、`failed`（每项含 `version_id` 与失败原因）和 `ignored_findings`。

### `POST /api/v1/valuations/{version_id}/reject`（驳回）

请求体：`{"reason":"必须填写原因"}`，原因长度 1--2000。`pending_review` 或 `publishable` 版本可以被驳回，返回新状态 `rejected`。

### `POST /api/v1/valuations/{version_id}/revoke`（撤回已发布版本）

请求体必须包含原因。服务端将版本置为 `revoked`（已作废）并创建分析任务；返回 `version_id`、`status` 和 `analysis_run_id`。

### `POST /api/v1/valuations/{version_id}/restore`（恢复版本）

请求体必须包含原因。服务端在状态机和唯一发布约束下恢复允许恢复的历史版本，必要时替代当前已发布版本，并创建分析任务。返回 `version_id`、`status`、`superseded_version_ids` 和 `analysis_run_id`。

已发布、已替代和已作废版本及其标准化明细没有通用编辑或删除接口。

## 7. 产品和解析配置维护

以下接口只允许 `admin` 和 `operator`；它们维护人工配置，不修改已有估值明细。

### 7.1 产品

#### `POST /api/v1/funds`（新增产品）

请求体：

```json
{
  "standard_name":"梦溪一号",
  "product_code":"M001",
  "establishment_date":"2024-06-24",
  "strategy":"可选人工字段",
  "manager":"可选人工字段",
  "notes":"可选",
  "aliases":[
    {
      "alias":"梦溪一号",
      "source_location":"可选",
      "match_priority":0,
      "valid_from":"2024-06-24",
      "valid_to":null
    }
  ]
}
```

`standard_name`、`product_code`、`establishment_date` 和至少一个 `aliases` 必填；别名数组 1--100 项。产品名、产品代码、别名不能与已有冲突；成功返回 `201` 产品主数据。

#### `PATCH /api/v1/funds/{fund_id}`（修改产品）

所有字段可选，只更新提交的字段；空请求返回 `400`。产品名、代码唯一性会重新校验。产品状态不在此接口修改。

#### `POST /api/v1/funds/{fund_id}/enable`（启用产品）

无需请求体，将产品设为 `active`（启用），返回产品主数据并记录审计。

#### `POST /api/v1/funds/{fund_id}/disable`（停用产品）

请求体必须为 `{"reason":"原因"}`，将产品设为 `inactive`（停用），不删除历史估值数据。

### 7.2 别名

#### `GET /api/v1/funds/{fund_id}/aliases`（别名列表）

返回该产品别名，按 `match_priority` 降序和编号排序；响应含 `meta.total`。

#### `POST /api/v1/funds/{fund_id}/aliases`（新增别名）

请求字段为 `alias`（必填，1--255）、`source_location`（可选）、`match_priority`（0--1000，默认 0）、`valid_from` 和 `valid_to`。日期起点不能晚于终点；成功返回 `201`。

#### `PATCH /api/v1/funds/{fund_id}/aliases/{alias_id}`（修改别名）

所有字段可选，只更新提交字段；会校验唯一性和有效期。

#### `DELETE /api/v1/funds/{fund_id}/aliases/{alias_id}`（删除别名）

请求体可选：`{"reason":"原因"}`。这是唯一提供删除语义的目录接口，仅删除人工别名，不删除估值或原始文件；成功返回 `{"id":...,"deleted":true}`。

### 7.3 份额类别主数据

#### `GET /api/v1/funds/{fund_id}/share-classes`（份额类别列表）

可选查询参数 `status=active|inactive`。返回 `id`、`fund_id`、`share_code`、`share_name`、`status`、`enabled_from`、`disabled_from` 和 `notes`。

#### `POST /api/v1/funds/{fund_id}/share-classes`（新增份额类别）

请求体至少含 `share_code` 和 `share_name`，可带启用日、停用日和备注。一个产品内份额代码唯一。

#### `PATCH /api/v1/funds/{fund_id}/share-classes/{share_class_id}`（修改份额类别）

部分更新份额代码、名称、日期和备注，校验日期范围与代码唯一性。

#### `POST /api/v1/funds/{fund_id}/share-classes/{share_class_id}/disable`（停用份额类别）

请求体必须含 `reason`，可带 `disabled_from`。返回更新后的份额类别。

#### `POST /api/v1/funds/{fund_id}/share-classes/{share_class_id}/enable`（启用份额类别）

无需请求体，清除停用状态并返回份额类别。

### 7.4 科目映射

#### `GET /api/v1/subjects/mappings`（科目映射列表）

查询参数：`status=active|inactive`、`category`（标准类别精确匹配）、`page` 默认 1、`page_size` 默认 20，范围 1--100。

每项返回 `id`、`subject_code_or_prefix`、`raw_name_pattern`、`standard_category`、`is_leaf`、`include_in_holdings`、`valid_from`、`valid_to`、`rule_version` 和 `status`。

#### `POST /api/v1/subjects/mappings`（新增科目映射）

请求体字段：`subject_code_or_prefix`、`raw_name_pattern`、`standard_category`、`is_leaf`、`include_in_holdings`、`valid_from`、`valid_to`、`rule_version` 和 `status`。代码/前缀与名称模式至少提供一个；如果字段显式传空字符串会被拒绝；有效期必须正确。

#### `PATCH /api/v1/subjects/mappings/{mapping_id}`（修改科目映射）

部分更新上述字段；至少保留代码/前缀或名称模式之一，标准类别和规则版本不能清空。

#### `POST /api/v1/subjects/mappings/{mapping_id}/disable`（停用科目映射）

请求体可带可选 `reason`。停用只影响之后的解析，不改写已落库的 `account_subject_daily`（科目每日快照）。

## 8. 风险规则和事件

### `GET /api/v1/risk/rules`（规则列表）

所有登录用户可读。查询参数：`rule_code`、`enabled`、`include_history`、`page`、`page_size`。默认每个规则编码只返回最新版本；`include_history=true` 返回全部版本。

每项字段：`id`、`rule_code`、`rule_type`、`scope`、`threshold`、`severity`、`valid_from`、`valid_to`、`version`、`enabled`。

### `POST /api/v1/risk/rules`（新增规则）

所有登录用户（`admin`、`operator`、`viewer`）可调用。请求体字段：

```json
{
  "rule_code":"daily_return_warning",
  "rule_type":"daily_return",
  "scope":"all",
  "threshold":-0.02,
  "severity":"warning",
  "valid_from":"2026-01-01",
  "valid_to":null,
  "version":null,
  "enabled":true
}
```

支持的 `rule_type` 只有：`daily_return`、`max_drawdown`、`current_drawdown`、`single_position_weight`、`top_five_weight`、`concentration`。`severity` 为 `info`、`warning` 或 `critical`。没有提供版本时服务端为同一 `rule_code` 生成下一个数字版本；有效期不能反向。

### `PATCH /api/v1/risk/rules/{rule_id}`（创建规则新版本）

所有登录用户（`admin`、`operator`、`viewer`）可调用。请求体可部分更新规则类型、作用域、阈值、严重度、有效期和启用状态。服务端不修改旧规则，而是为同一 `rule_code` 创建新版本；返回新规则记录。

### `GET /api/v1/risk/events`（风险事件列表）

所有登录用户可读。查询参数：`fund_id`、`rule_code`、`severity`、`status`、`start`、`end`、`page`、`page_size`。日期范围使用估值日，结束日期不能早于开始日期。

每项字段：`id`、`risk_rule_id`、`rule_code`、`fund_id`、`fund_name`、`valuation_date`、`severity`、`status`、`first_triggered_at`、`last_triggered_at`、`handling_note`、`evidence_snapshot`、`handled_by_user_id`、`handled_at`、`evidence_reference`、`created_at`。

### `POST /api/v1/risk/events/{event_id}/handle`（处理风险事件）

只允许 `admin` 和 `operator`。请求体：

```json
{
  "status":"acknowledged",
  "handling_note":"已核查",
  "evidence_reference":"可选引用"
}
```

`status` 不能为 `open`，可为 `acknowledged`、`resolved` 或 `ignored`。`handling_note` 长度 1--4000；服务端记录处理人、时间和审计。

### `POST /api/v1/risk/events/{event_id}/resolve`（兼容处理路径）

请求和权限与 `/handle` 相同，执行同一处理逻辑。新客户端优先使用 `/handle`，但当前两条路径都是真实路由。

## 9. 邮件接入

设置查看、同步记录和立即同步允许 `admin`、`operator`；账号、授权码、连接测试和自动同步开关只允许 `admin`。

### `GET /api/v1/mail/settings`（非敏感邮箱设置）

返回 `host`、`port`、`username`、`configured`、`credential_source`、`credential_writable` 和 `auto_sync_enabled`。永远不返回授权码。邮件连接环境变量不完整时仍返回安全的未配置状态。

### `PUT /api/v1/mail/settings`（更新邮箱账号）

仅管理员。请求体必须严格为：

```json
{"username":"funds@example.com"}
```

账号会去除首尾空白后以非敏感系统配置持久化；长度限制为 1--320。该接口只接受 `username`，不会接受或保存 IMAP（邮件接收协议）服务器、端口或密码字段。返回完整的非敏感邮箱设置，但永远不返回授权码。账号更新会记录脱敏审计事件 `mail.username_updated`；后续连接测试、立即同步和维护同步都会使用最新账号。

### `PUT /api/v1/mail/credential`（更新授权码）

仅管理员。请求体必须严格为：

```json
{"authorization_code":"QQ 邮箱授权码"}
```

授权码去除首尾空白后长度 1--256。服务端将其原子写入受控凭据文件，并只返回 `configured`、`credential_source`、`credential_writable` 等状态，不写入数据库、响应、日志或审计摘要。若授权码由环境变量管理，返回 `409`；受控文件不可写，返回 `503`。

### `POST /api/v1/mail/pause`（暂停自动同步）

仅管理员。保存 `auto_sync_enabled=false`，返回该开关状态。重复调用幂等；只影响调度触发，不阻止立即同步。

### `POST /api/v1/mail/resume`（恢复自动同步）

仅管理员。保存 `auto_sync_enabled=true`，返回该开关状态。重复调用幂等。

### `POST /api/v1/mail/test-connection`（测试邮箱连接）

仅管理员，无请求体，不导入附件。成功返回 `{"data":{"connected":true}}`。未配置或凭据文件不可读返回 `503`，`detail` 分别为 `mail_not_configured` 或 `mail_credential_unavailable`；连接超时、DNS（域名解析）、TLS（安全连接）或其他连接失败返回 `502`，`detail` 分别为 `mail_connection_timeout`、`mail_dns_failed`、`mail_tls_failed` 或 `mail_connection_failed`。响应和日志都不包含授权码。

### `POST /api/v1/mail/sync`（立即同步）

管理员或业务员可调用，无请求体。默认在当前请求内执行只读 IMAP（邮件接收协议）同步并返回运行结果，不返回后台任务编号。结果包括 `run_id`、`status`、邮件/附件计数、重复/忽略/失败计数、错误数和错误编号列表。

如果请求头携带 `x-async-sync: 1`，同步会改为后台任务模式：服务端把工作登记到 `mail_sync` 后台任务并立即返回运行编号；客户端可通过 `GET /api/v1/mail/sync-runs` 查看进度与结果，并使用 `POST /api/v1/mail/sync/{run_id}/cancel` 请求取消。仅允许 `admin` 和 `operator` 调用。

### `POST /api/v1/mail/sync/{run_id}/cancel`（取消异步同步运行）

管理员或业务员可调用。当 `x-async-sync: 1` 触发的后台同步仍在 `pending` 或 `running` 状态时，把取消请求标记到对应任务；不存在或不在可取消状态时返回 `404 detail=mail_sync_not_running`。成功返回 `{"data":{"run_id":...,"status":"cancelling"}}`。

### `GET /api/v1/mail/sync-runs`（同步记录）

管理员或业务员可读。返回同步运行数组，包含时间、处理数量、跳过数量和安全错误摘要；不会返回邮件正文或授权码。

### `PUT /api/v1/mail/schedule`（修改定时同步）

仅管理员。请求体为定时同步配置对象：

```json
{"mode":"interval","interval_minutes":30}
```

或：

```json
{"mode":"scheduled","times":[{"time":"09:00","days":[1,2,3,4,5]},{"time":"18:00","days":[]}]}
```

`mode` 必须为 `"interval"` 或 `"scheduled"`。间隔模式下 `interval_minutes` 范围 1--1440。定时模式下 `times` 每项 `time` 格式 `"HH:MM"`，`days` 为空数组表示每天，`[1,2,3,4,5]` 表示周一到周五。最多 10 个时间点。成功返回更新后的邮箱设置（含 `schedule` 字段）。

## 10. 系统设置、运维和审计

### `GET /api/v1/system/settings`（系统设置）

仅管理员。返回白名单设置及来源：

| 键 | 类型和范围 |
|---|---|
| `source_retention_days` | 整数，1--3650 |
| `mail_sync_schedule` | 对象（见下方邮件同步调度） |
| `mail_sync_enabled` | 布尔值 |
| `backup_retention_days` | 整数，1--3650 |
| `timezone` | 已安装的时区名称 |

每项格式为 `{"value":...,"source":"default|environment|database"}`。`meta.runtime_note` 会说明哪些值只是持久化配置、哪些会在当前进程中读取。

`mail_sync_schedule` 支持两种模式：

- 间隔模式：`{"mode":"interval","interval_minutes":30}`
- 定时模式：`{"mode":"scheduled","times":[{"time":"09:00","days":[1,2,3,4,5]}]}`

`days` 为空数组表示每天，`[1,2,3,4,5]` 表示周一到周五（ISO weekday，1=周一，7=周日）。

### `PATCH /api/v1/system/settings`（修改系统设置）

仅管理员。可以直接传键值，也可以放在 `settings` 对象中，例如：

```json
{"settings":{"mail_sync_enabled":false,"mail_sync_schedule":{"mode":"interval","interval_minutes":30}}}
```

未知键、类型错误、范围错误和非法时区返回 `422`。成功返回完整有效设置、来源和运行时说明，并写审计。

### `GET /api/v1/system/health`（受保护依赖健康）

仅管理员。数据库可用时返回 `status=ok`、`database=ok` 和服务名；数据库不可用时返回 `status=degraded`、`database=unavailable` 和服务名。不返回连接地址、路径或秘密。

### `GET /api/v1/system/operations`（运维摘要）

管理员或业务员可读。返回数据库、worker 心跳、任务队列计数、维护最近成功/失败、备份状态和磁盘使用摘要。不会返回租约令牌、原始路径、数据库连接或授权码。状态可能为 `ok`、`degraded` 或 `critical`。

### `POST /api/v1/system/retention/preview`（原始文件清理预演）

仅管理员，无请求体。始终为预演，不删除文件；返回候选数量、跳过原因、估算大小和错误摘要。预演会执行与真实清理相同的安全判断，包括已发布/待复核/失败任务/备份保护。

### `POST /api/v1/system/retention/execute`（执行原始文件清理）

仅管理员。请求体必须为：

```json
{
  "confirmation":"DELETE_EXPIRED_SOURCE_FILES",
  "reason":"已核对预演结果"
}
```

`confirmation` 必须完全匹配固定字符串，`reason` 长度 1--1000 且不能是空白。服务端只删除通过安全判断且超过保留期的原始文件；数据库标准化数据和审计日志不删除。结果包含 `command`、`status`、`summary` 和失败时的 `error_code`。

### `GET /api/v1/audit-logs`（审计查询）

管理员或业务员可读。查询参数：`actor_user_id`、`action`、`resource_type`、`result=success|failure`、`start`、`end`、`page`、`page_size`。时间范围不能反向。

返回 `id`、`actor_user_id`、`action`、`resource_type`、`resource_id`、`summary`、`reason`、`result` 和 `created_at`。服务端递归过滤密码、令牌、授权码、连接串和数据库地址；没有审计删除接口。

## 11. CSV 导出

以下接口允许所有登录用户，除了导入报告只允许 `admin` 和 `operator`。均返回 `text/csv; charset=utf-8` 流，带 UTF-8 BOM（字节顺序标记）和固定安全文件名。每次导出都会记录 `export.*` 审计日志，不缓存完整文件。

响应头还包括：

- `Content-Disposition`：下载文件名；
- `X-Exported-At`：导出时间，UTC ISO 8601；
- `X-Data-As-Of`：查询截至日期或区间。

导出文本若以 `=`, `+`, `-`, `@` 开头会增加单引号，防止电子表格公式注入。没有数据时仍返回带表头的空 CSV。

| 方法和路径 | 参数 | 文件内容 |
|---|---|---|
| `GET /api/v1/exports/overview` | `as_of` | 产品编号、产品名称、估值日、净资产、单位净值、日收益 |
| `GET /api/v1/exports/funds` | `q`、`as_of` | 产品编号、产品名称、状态、估值日、单位净值、日收益 |
| `GET /api/v1/exports/funds/{fund_id}/overview` | `as_of` | 产品、版本、总资产、总负债、净资产、净值和收益摘要 |
| `GET /api/v1/exports/funds/{fund_id}/nav-series` | `start`、`end` | 净值、累计派现、调整后净值、日/累计收益和口径 |
| `GET /api/v1/exports/funds/{fund_id}/allocation` | `as_of`、`denominator` | 标准资产类别、市值、权重 |
| `GET /api/v1/exports/funds/{fund_id}/positions` | `as_of`、`account`、`market` | 证券、账户、市场、数量、成本、市值、权重、估值增值、停牌信息 |
| `GET /api/v1/exports/funds/{fund_id}/share-classes` | `as_of` | 份额、净资产、实收资本、净值和日/年/月/季/周收益 |
| `GET /api/v1/exports/imports` | `source_type`、`status`、`start`、`end` | 批次、来源、文件数、批次状态、任务状态、创建时间 |

`denominator` 只能是 `net_asset_value`、`total_assets` 或 `market_value`。所有产品导出只读取活动产品的已发布版本；指定不存在或停用产品返回 `404`，指定产品没有该版本时返回带表头的空文件。日期区间起止反向返回 `422`。

## 12. 当前未提供的接口

以下路径或能力当前不存在，客户端不得根据旧草案调用：

- `/api/v1/funds/{fund_id}/overview` 的 JSON 页面概览；该路径只有 `/api/v1/exports/funds/{fund_id}/overview` CSV 导出。
- 产品级资产配置 JSON 查询、份额每日 JSON 查询、产品级独立回撤/峰谷序列查询；公司级净值与回撤序列已由 `/api/v1/dashboard/series` 提供。
- 版本历史、版本差异、统一字段来源查询和产品详情直达原始文件。
- 重新解析、重新校验、批量重试全部、已知例外、映射试跑和风险规则试算。
- 网页目录批量导入、邮件附件导航和完整导入处理日志。
- 外部行情、交易流水、申赎流水、合同、监管、组织或 Windows 客户端专用 API。

旧的 `/api/v1/imports/{import_id}/publish|reject|revoke|restore` 不存在；估值版本生命周期使用 `/api/v1/valuations/{version_id}/...`。


## 账号产品范围

`POST /api/v1/users` 增加可选 `fund_access_mode`（`all` / `selected`，默认 `all`）与 `fund_ids`（正整数数组，默认 `[]`）。管理员只能使用 `all` 且数组为空；全部模式下不得传产品编号。指定产品可以为空，不自动获得以后新增的产品。未知产品返回 `422`，创建整体回滚。

`PATCH /api/v1/users/{user_id}/fund-access` 仅管理员使用，返回更新后的用户对象。请求示例：

```json
{"fund_access_mode":"selected","fund_ids":[1,3],"reason":"调整负责产品"}
```

三个字段必填，原因不得为空白。无效产品或配置返回 `422`，用户不存在返回 `404`，非管理员返回 `403`。更改记录前后授权并写入审计，已有登录在下一次请求读取新授权，无需重新登录。

登录、`/auth/me`、用户列表和用户变更响应均返回 `fund_access_mode` 与 `fund_ids`；全部模式数组为空。viewer/operator 互转保留范围；提升为 admin 时改为全部并清空授权关系。
