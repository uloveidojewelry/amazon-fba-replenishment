# 独立 Seller SKU 补货试算

本模式按美国站 Marketplace ID + Seller SKU 统计，不要求系列审核通过。
原系列映射、系列停补和批量审核功能均保留；使用独立数据库，不修改原审核数据库。
本报表暂不执行系列联动，只执行明确的 SKU 停补、禁用和已到生效日的已确认 SKU 停补。
未经人工确认，不会解除任何已有 SKU 停补。

## 采集与恢复

```bash
fba-plan sku-sync-plan --precheck-only --confirm-read-only
fba-plan sku-sync-plan --confirm-read-only
```

默认从 `output/sku-review/review.db` 只读复制既有配置到
`output/sku-independent-90d/sku_plan.db`，后续运行复用独立数据库。
`--db`、`--source-db` 和 `--output-dir` 可指定其他路径，源数据库和目标必须不同。
每次出表还会只读观察原配置当前有效的 SKU 停补，防止首次复制后新增的停补遗漏；
不会把原配置的系列停补带入本次计算，也不会自动恢复独立数据库已有的 SKU 停补。

采集顺序是完整 FBA 库存、小样本订单和入仓读取预检查、90 天订单分片、入仓货件、
本地原子核对及 Excel 输出。预检查硬错误阻止扩量。最近 90 个完整美国业务日按
`America/Los_Angeles` 划分，截止日前一天；不将未完成的当天当成完整一天。

沿用已验证的 Orders 2026-01-01 和 Fulfillment Inbound v0。当前销量口径为
FBA/AFN 的订单件数，取消订单排除；未采集 MFN 自配送、退款退货净销量。
不请求买家或收件人数据。Inbound 2024-03-20 的历史只读探测为 403，未替换 v0。

完整日期分片才写入检查点，分页 Token、LWA Token、授权正文及客户信息不写入文件。
429 和临时服务错误会重试，请求频率依据接口配额调整。失败保留完整分片和上一次
有效本地数据，再执行相同命令即可续传。过期检查点按既有 TTL 重新采集，以核对后来取消的订单；
`--force` 刷新销量及入仓缓存。

相同订单和订单行去重，冲突、缺整单行、数量非法及无法确认的取消数量不能认证完整销量。
有待处理订单且能定位 SKU 时，只让该 SKU 的销量为 UNKNOWN，其余完整 SKU 可计算。
状态 `SUCCESS_WITH_SKU_EXCEPTIONS` 表示所有分页采完但存在单 SKU 例外，并非销量全部完整。
历史订单 SKU 当前没有 FBA 库存时，保留销量，库存缺失作为独立异常；不将缺失库存填零。

## 参数与计算

默认参数为目标覆盖 75 天、运输及入仓 35 天、安全库存 0 天、MOQ 1、箱规 1。
这些是原项目的默认试算值，未获确认时每行显示 `SCENARIO_ONLY`，不能用作正式采购依据。

```bash
fba-plan sku-plan --lead-days 35 --target-days 75 --safety-days 0 \
  --inbound-rule confirmed_eta
```

仅在用户明确确认全部参数后，才使用 `--parameters-confirmed` 标记确认；该标记仍不授权
采购或 Amazon 写入。默认使用最近 30 天日均销量；同时展示 7/30/60/90 天完整量、日均、
已观察量与完整度。仅在完整窗口证明没有销量时显示零；UNKNOWN 数量留空。

```
日均 = 完整 30 天销量 / 30
需求周期 = 运输及入仓天数 + 目标覆盖天数 + 安全库存天数
原始需求 = max(0, ceil(日均 × 需求周期 - 可售库存 - 合格在途))
建议 = 原始需求为正时按 MOQ 和箱规向上取整，否则为零
```

可售库存取 `fulfillableQuantity`；预留取 `totalReservedQuantity`，包括客户订单预留、
FC 转运和处理；不可售取 `totalUnfulfillableQuantity`。预留和不可售单独展示，
不加到可售库存，也不额外重复扣减。

三项 `inboundWorkingQuantity / inboundShippedQuantity / inboundReceivingQuantity`
作为库存 API 摘要与货件明细核对，不能和货件余额再次相加抵扣。
货件余额取发运减已接收，终止及 WORKING 货件不抵扣需求；未知状态或数量异常拦截。

`confirmed_eta` 仅计入有效活动货件且人工确认 ETA 在到货周期内的余额；未知、过期
且未确认抵达的 ETA 拦截。Amazon need-by/arrival 字段不冒充人工确认 ETA。
`exclude` 明确不抵扣在途，未知 ETA 仍列为异常，但单独未知 ETA 不拦截该场景；
数量、状态和摘要对账异常仍拦截。此规则改变需作为试算参数公开显示。

## Excel 和完整率

输出 `output/sku-independent-90d/sku_replenishment_90d.xlsx`，包含 SKU 总表、四窗口销量、
FBA 库存、在途明细、建议、缺货风险、异常、停补、参数、采集审计和统计。
缺货风险基于可售库存和 30 天需求；未知数据单独标注，不能用空白推断安全。

数据采集完整率为同时具备完整 90 日销量、六项库存数量及核对过的入仓数量的 SKU
占统计 SKU 总数的比例；ETA 完整度和异常另外列示。订单日期覆盖率与每 SKU 完整率
分别解释，不以单个同步成功日志冒充 100%。

所有结果仅供人工审核，不创建货件，不修改 Listing、库存或价格，不提交采购，不部署调度。
