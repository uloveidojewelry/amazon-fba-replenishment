# Fulfillment Inbound 2024-03-20 只读兼容性评估

评估日期：2026-10-14。此文档区分公开模型检查与真实店铺验证，不把公开模型可用当作店铺接口已经授权。

## 已核实的公开模型

通过继承的网络代理、启用 TLS 验证，读取 Amazon 官方仓库模型：

https://github.com/amzn/selling-partner-api-models/blob/main/models/fulfillment-inbound-api-model/fulfillmentInbound_2024-03-20.json

| 只读操作 | GET 路径 | 关键差异 |
|---|---|---|
| `listInboundPlans` | `/inbound/fba/2024-03-20/inboundPlans` | `pageSize` 为 1–30，默认 10；请求使用 `paginationToken`，响应为顶层 `inboundPlans` 和 `pagination.nextToken` |
| `getInboundPlan` | `/inbound/fba/2024-03-20/inboundPlans/{inboundPlanId}` | `shipments` 属于选定 placement option；未生成/确认货件时可能不存在；不能因缺少此字段断言货件数量为零 |
| `listInboundPlanItems` | `/inbound/fba/2024-03-20/inboundPlans/{inboundPlanId}/items` | 计划级商品数量，不等于实际发运或尚未接收数量 |
| `getShipment` | `/inbound/fba/2024-03-20/inboundPlans/{inboundPlanId}/shipments/{shipmentId}` | 内部 `shipmentId` 与标签上的 `shipmentConfirmationId`（例如 FBA 货件号）不同 |
| `listShipmentItems` | `/inbound/fba/2024-03-20/inboundPlans/{inboundPlanId}/shipments/{shipmentId}/items` | 顶层 `items`；商品使用 `msku`、`fnsku`、`asin`、`quantity`；`pageSize` 上限 1000；没有 v0 的 `QuantityReceived` |

`listInboundPlans` 不接受 marketplace 筛选参数。应检查响应中 `marketplaceIds`，自行筛选美国站；跨站点计划不能导入美国站快照。计划状态筛选值是 `ACTIVE`、`VOIDED`、`SHIPPED`，与货件状态不同。

公开模型只说明此接口列出 inbound plans，没有保证它覆盖 v0 全部历史货件。此次未核实“所有旧版/API/Send to Amazon 货件都在此列表”的命题，也不能反过来断言此接口只包含由新版 API 创建的计划。必须按店铺样本核对覆盖范围，保留 v0 数据源。

官方文档站 `developer-docs.amazon.com` 此次返回 301，跳转至 `developer-docs.amazon`。新域名不在当前已列出的网络策略中，未跟随跳转；因此迁移与历史覆盖规则没有获得文档站的进一步证据。

## 本次店铺只读探针结果

美国站配置下，`GET /inbound/fba/2024-03-20/inboundPlans?pageSize=1` 返回 **HTTP 403**。没有输出或保存响应内容，没有进一步请求计划、货件或商品，也没有调用任何业务写入接口。

此结果归类为访问拒绝；仅凭状态码无法区分新版 API 权限、应用/卖家授权、适用范围或代理策略。v0 的成功不能证明 2024 接口可用。保留 v0，不把此次 2024 探针失败当作零计划或零在途。

## 最小真实只读验证

1. 仅调用 `GET /inbound/fba/2024-03-20/inboundPlans?pageSize=1`，检查 HTTP 状态、结构、计划数和是否仍有分页，不输出或保存原始响应。
2. 模型中的计划列表包含 `sourceAddress`、计划名等字段。采集器只能保留计划 ID、站点、状态和日期；删除地址、姓名、公司、电话、电子邮箱与自由文本名称。
3. 选取一个美国站计划后，读取计划的货件摘要；仅有合法计划 ID/货件 ID 时才能读取对应货件与商品。不存在样本时只能证明列表访问权限，不能证明货件/商品兼容。
4. 通过 `shipmentConfirmationId` 对照 v0 `ShipmentId`，再核对 `msku`/`SellerSKU`、FNSKU、状态与数量。记录覆盖率和不一致项，不把两套数量相加。
5. 所有操作仅允许 GET。不得调用创建、生成、确认、更新、取消计划/货件以及生成或确认配送时间窗的操作。

## 继续保留 v0 的原因

- 2024 商品的 `quantity` 是商品数量，模型没有给出接收数量。不能用它直接替代 v0 `QuantityShipped - QuantityReceived`。
- 新模型增加 `UNCONFIRMED`、`READY_TO_SHIP`、`ABANDONED`、`MIXED` 等货件状态。不能直接套用旧版活动货件枚举，未知状态必须拦截。
- `selectedDeliveryWindow` 是选定的预约/配送时间窗。它不是已经核实的承运人 ETA，不得自动写入现有 ETA 字段并抵减需求。
- `source`、`destination`、`contactInformation`、`trackingDetails` 等货件字段可能含个人信息或运输标识；原始货件响应不得进入日志、文件或 Excel。

本阶段最小方案是保留 v0 入仓主采集流程，新增隔离的 2024 GET 兼容性探针。在覆盖范围、ID 映射、接收数量来源和 ETA 含义验证前，2024 结果只供兼容性审计，不参与在途抵扣。

## 独立数据质量审查发现

本次开始审查时，仓库已存在的数据库仅包含示例 CSV 数据与独立 MOCK 数据。示例映射为 4 个 SKU、2 个系列；示例销售只有 1 天；MOCK 有 30 天合成数据。没有证据表明这些映射和停止补货标记已由店铺真实数据或人工主数据确认。

真实采集不能复制示例 SKU 系列或示例停补标记。发现的真实 SKU 应独立保存，并在系列映射、停补主数据未确认时拦截补货；未知映射不能通过字符串猜测自动批准。

扩量前需验证订单分页与订单行完整性、库存分页与 SKU/FNSKU 映射、入仓商品去重、数量异常及未知 ETA。库存摘要中的入仓 working/shipped/receiving 数量应用于交叉核对，不能与逐货件在途数量双重抵扣。任何完整性尚未证实的来源都不能补零并作为完整 90 天销量。
