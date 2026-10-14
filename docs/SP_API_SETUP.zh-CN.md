# 美国站 SP-API 连接准备、授权和 Codex 配置

本阶段只准备连接，不执行真实 LWA 或店铺 API 请求。配置凭据本身不代表同意连接。默认诊断和模拟命令不使用网络；真实测试必须在用户明确确认后进行。项目没有创建 FBA 货件、修改 Listing 或采购单的接口。

## 1. 版本核查与验证边界

核查时间：2026-10-14。已通过 Amazon 官方 GitHub 仓库获取当前模型。模型存在不等于目标卖家账号一定有权调用，也不能证明接口在该账号仍可用。

| 数据 | 官方模型核查结果 | 本项目选择 | 权限准备 |
|---|---|---|---|
| 订单和订单行 | Orders v0 读取操作已标记 `deprecated: true`；发布了 `2026-01-01` | 默认 2026-01-01 `searchOrders`，读取内嵌 orderItems；v0 仅显式兼容 | Inventory and Order Tracking；需按当前 Orders 权限表复核 |
| FBA 库存 | FBA Inventory v1 `getInventorySummaries` 存在，未标记 deprecated | 完整分页库存读取，省略增量日期条件 | Inventory and Order Tracking |
| 入仓货件 | 当前 Inbound v0 模型保留 `getShipments` 和货件商品读取，未标记 deprecated；新版 2024-03-20 也存在 | 保留 v0 只读采集；新版计划、运输、到货窗口适配尚未实现 | Amazon Fulfillment |

当前网络代理阻断了 `developer-docs.amazon.com`，因此**当前网页的角色矩阵、授权 UI 和退役日期没有在此环境在线复核**。官方旧文档仓库已迁移，只能作为角色名称和文档入口线索，不能作为 2026 年的最新授权流程依据。不要根据 deprecated 标记推断具体停止服务日期。本说明中的授权操作需对照下方官方链接及实际 Seller Central 界面确认；后续真实权限只能通过明确确认的诊断验证。

当前客户端使用 LWA access token，不要求配置 AWS Access Key、IAM ARN 或 SigV4。不要照搬旧文档中的历史签名步骤。仅需非 PII 订单字段和数量；不请求 BUYER/RECIPIENT、buyerInfo/address 或 Restricted Data Token。若今后增加受限信息访问，必须单独评估角色和 RDT，不能扩大当前采集范围。

## 2. 美国站与时间口径

- Marketplace ID：`ATVPDKIKX0DER`，不要把本地 `US` 命名空间或 Seller ID 填入此字段。
- Endpoint：`https://sellingpartnerapi-na.amazon.com`，美国站配置欧洲/远东 Endpoint 会被拒绝。
- 本地命名空间：CLI 的 `--marketplace US`，与 Amazon Marketplace ID 是不同概念。
- 销量时区：可以采用 `UTC`，或在首次同步前选择 `America/Los_Angeles` 作为美国站业务日口径。IANA 时区会自动处理夏令时；不要使用固定 UTC-8。商城 ID 本身不能确定企业需要的日统计口径。
- 已有数据库默认/绑定为 UTC 时，不能直接切换到洛杉矶时区混用历史销量。应使用新的数据库/命名空间并重新同步。
- `asof` 是最后一个完整销量日；窗口包含该日及前 29 天。当前日或刚结束不足两分钟的窗口不能认证为完整。聊天时区 Asia/Shanghai 不影响店铺销量分桶。

## 3. 所需变量与 Secrets（只有名称，不含值）

| 名称 | Codex 配置类型 | 说明 |
|---|---|---|
| `SP_API_LWA_CLIENT_ID` | Environment variable | LWA 客户端 ID，不是 SP-API Application ID；从同一个开发者应用取得 |
| `SP_API_LWA_CLIENT_SECRET` | Secret | 同一应用的 LWA 客户端密钥；代理投递目的域名为 `api.amazon.com` |
| `SP_API_REFRESH_TOKEN` | Secret | 目标卖家授权该应用所得的 refresh token；代理投递目的域名为 `api.amazon.com` |
| `SP_API_MARKETPLACE_ID` | Environment variable | 美国站 `ATVPDKIKX0DER` |
| `SP_API_ENDPOINT` | Environment variable | `https://sellingpartnerapi-na.amazon.com` |
| `SP_API_TIMEZONE` | Environment variable | 当前兼容默认 UTC；首次同步前可选择 America/Los_Angeles |
| `SP_API_ORDERS_VERSION` | Environment variable | `2026-01-01`；不要依赖默认 v0 |

不需要手工设置 access token；程序通过 LWA 交换并仅在内存短期缓存。Seller ID 在当前采集接口中不是必需配置。不要把 refresh token、client secret 或 access token 发到聊天，不要放入 README、Git、CLI 参数、测试快照或错误日志。`.env.example` 只说明变量，程序不会自动读取 `.env`。

## 4. Seller Central 授权步骤

推荐本项目用于自己的店铺时采用**私有应用自授权**；第三方店铺使用公开应用 OAuth，不能直接复用自己的 refresh token。

1. 使用美国站店铺主账号或具备开发者及应用授权权限的账号登录 Seller Central。确认卖家账号和目标美国商城正确，准备开发者注册/审核所需信息；通常需专业卖家账号。
2. 从 Apps & Services → Develop Apps（应用和服务 → 开发应用）进入 Amazon 当前开发者管理入口。若被引导至 Solution Provider Portal/开发者控制台，按官方当前入口继续；开发者审批未完成时先完成注册及角色申请。
3. 为此补货工具创建或选择同一个 SP-API 应用。自己的店铺使用私有应用；按当前控制台流程配置应用。申请并确认 **Inventory and Order Tracking** 和 **Amazon Fulfillment** 角色；不要为本工具额外申请 Product Listing 或受限买家数据角色。这些角色可能具有较广权限，程序的 GET 白名单限制不等于 Amazon 发放了只读专用令牌。
4. 在该应用的 LWA credentials 中查看/获取 Client ID 和 Client Secret。保存到安全配置位置，不复制到项目文件。不要将 Application ID 当作 Client ID。
5. 在开发者控制台应用操作中选择 Authorize/自授权，按页面要求选择自己的卖家账号并同意已批准角色，取得 refresh token。批准角色变化后按 Amazon 当前要求重新授权，以免应用已获角色但卖家旧授权尚未涵盖。
6. 若为第三方公开应用，卖家应从 Seller Central Manage Your Apps（管理您的应用）或官方授权 URL 发起 OAuth 同意流程；必须验证 OAuth state，使用已注册的 redirect URI，并在服务器端安全交换授权码。**本项目没有实现公开应用 OAuth 回调服务器**，不能用私有应用步骤假装已连接第三方店铺。
7. 将同一应用、同一卖家授权得到的三个绑定配置到 Codex。密钥轮换、授权撤销或 refresh token 失效后更新绑定；不要重复授权或泄露凭据来排查 HTTP 403。
8. 完成配置后先运行离线诊断。用户明确确认允许本工具连接真实店铺后，再运行只读诊断；分别检查 LWA、库存、订单和入仓结果，不以 LWA 成功代替接口角色验证。

官方页面目前未在本环境在线读取，上述菜单名称可能随界面变化；以 Amazon 当前引导为准。无需为了排查本工具去创建真实货件或修改商品。

## 5. Codex 环境设置

1. 打开本项目的 Codex **环境设置**，在环境变量中配置上表的非敏感字段。
2. 在 **Secrets** 中安全填写 `SP_API_LWA_CLIENT_SECRET` 和 `SP_API_REFRESH_TOKEN`，应用变量目标名保持完全一致。当前云环境的代理 Secrets 投递目的域名设置为 `api.amazon.com`；不要将代理占位字符串当作无效凭据，也不要试图读取代理内部的真实值。
3. 网络保留现有 GitHub/包管理器设置，增加 `api.amazon.com`、`sellingpartnerapi-na.amazon.com`；阅读官方文档还需 `developer-docs.amazon.com`。这些只是域名要求，不表示运行实例已应用或连接成功。
4. Review/保存配置；需要时发布环境。草稿保存不执行脚本、不会创建真实凭据，也不代表运行实例已经注入。新任务中检查绑定名称和就绪状态，禁止输出值。
5. 进入 `/workspace/amazon-fba-replenishment`，使用 `/workspace/amazon-fba-venv/bin/fba-plan` 或激活该虚拟环境，然后执行下列离线命令。

```bash
fba-plan diagnose-spapi
fba-plan diagnose-spapi --mock
fba-plan sync --mock
fba-plan --db output/mock-fba.db plan --output output/mock-replenishment.xlsx
```

离线诊断只打印变量名与 missing/present_unverified，不验证凭据。模拟流程不需要凭据、没有 HTTP 传输，采用独立数据库；模拟数量和货件均带有模拟标识，不能用于真实采购。J001 默认演示整系列停补，J002 用完整的模拟销量演示正数建议，不补造原始 CSV 中缺失的 29 天。

**仅在用户明确同意后**可运行：

```bash
fba-plan diagnose-spapi --live --confirm-read-only
# 首次业务同步前先导入确认过的 SKU 映射，选择真实本地数据库：
fba-plan --db data/fba.db --marketplace US sync --kind all --confirm-read-only
```

只读诊断不会写数据库，仅执行 LWA 认证和每类接口的第一页 GET，允许有限重试。第一页成功不代表完整快照。既有模拟数据库禁止接入真实同步；已有真实数据禁止被模拟同步覆盖。

## 6. 安全错误处理与验收

| 诊断代码/状态 | 含义和处理 |
|---|---|
| `MISSING_CREDENTIALS` / missing | 绑定尚未注入；去环境设置填写，不在聊天中提供值 |
| `CONFIRMATION_REQUIRED` | 尚未明确允许真实请求；不会发起网络连接 |
| `INVALID_CONFIGURATION` / configuration failed | 检查美国商城、区域 Endpoint、IANA 时区、订单版本；不回显错误字段原值 |
| `LWA_INVALID_CLIENT` | 检查同一应用的 Client ID/Secret 与轮换状态 |
| `LWA_INVALID_GRANT` | 检查 refresh token 与应用/卖家匹配、授权撤销；必要时按官方流程重新授权 |
| `SPAPI_HTTP_401` | 令牌/授权问题；程序最多刷新一次再报错 |
| `SPAPI_HTTP_403` | 可能是角色、卖家授权、商城或代理策略；仅凭 403 不能断定需要新 Token |
| `SPAPI_HTTP_429` | 限流重试预算耗尽；不要连续重复请求 |
| `PROXY_ERROR` / `NETWORK_ERROR` | 核对允许域名、代理与网络超时，不能绕过云代理 |
| `TLS_ERROR` | 修复 CA 信任，禁止关闭 TLS 校验 |
| `REDIRECT_BLOCKED` | 为保护 token，认证/API 请求不跟随重定向 |

不记录原始响应体、请求凭据、客户地址/姓名或异常中的 URL。默认诊断退出 0 仅说明配置语法正确，不意味着凭据齐全；必须看 `configuration_complete` 和 `authentication_verified`。失败/阻断诊断退出 1，未确认的真实同步退出 2 且不创建 DB。

停补规则、数据完整性、未知 ETA 和库存新鲜度拦截持续生效。人工恢复仍需 `resume --approved-by`；审批人字段只是操作者声明，尚未实现身份权限认证。

## 7. 官方资料

当前模型已读取；网页链接因代理限制待在线复核。

- [Orders 2026-01-01 官方模型](https://github.com/amzn/selling-partner-api-models/blob/main/models/orders-api-model/orders_2026-01-01.json)
- [Orders v0 官方模型](https://github.com/amzn/selling-partner-api-models/blob/main/models/orders-api-model/ordersV0.json)
- [FBA Inventory v1 官方模型](https://github.com/amzn/selling-partner-api-models/blob/main/models/fba-inventory-api-model/fbaInventory.json)
- [Inbound v0 官方模型](https://github.com/amzn/selling-partner-api-models/blob/main/models/fulfillment-inbound-api-model/fulfillmentInboundV0.json)
- [Inbound 2024-03-20 官方模型](https://github.com/amzn/selling-partner-api-models/blob/main/models/fulfillment-inbound-api-model/fulfillmentInbound_2024-03-20.json)
- [Marketplace IDs](https://developer-docs.amazon.com/sp-api/docs/marketplace-ids)
- [SP-API endpoints](https://developer-docs.amazon.com/sp-api/docs/sp-api-endpoints)
- [Inventory and Order Tracking role](https://developer-docs.amazon.com/sp-api/docs/inventory-and-order-tracking-role)
- [Amazon Fulfillment role](https://developer-docs.amazon.com/sp-api/docs/amazon-fulfillment-role)
- [自授权](https://developer-docs.amazon.com/sp-api/docs/self-authorization)
- [应用授权](https://developer-docs.amazon.com/sp-api/docs/authorizing-selling-partner-api-applications)
- [连接 SP-API](https://developer-docs.amazon.com/sp-api/docs/connecting-to-the-selling-partner-api)
