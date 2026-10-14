# Auto SKU Discovery V1.0

发现以 Amazon Marketplace ID + Seller SKU 为唯一键；本地 US 命名空间绑定美国站
`ATVPDKIKX0DER`，不得在同一数据库混用卖家。来源是完整 FBA Inventory v1
响应，涵盖该接口返回的库存 SKU；不宣称覆盖没有 FBA 库存记录的全部 Listing。
首次运行建立完整基线，后续完整成功清单比较新增、现有和疑似未活跃 SKU。
任何失败或部分分页都不能删除历史、更新缺失标记或清空上次有效库存。

## 候选与正式映射

优先保留已人工批准的正式映射，其后使用已批准全匹配命名规则、用户 Excel
的精确 SKU/产品型号、Catalog VARIATION 父子体证据。未经批准的保守命名提示
仅在没有其他证据时展示，绝不按第一个连字符截取。不同来源发生矛盾会保留
正式映射并标记 `FAMILY_CONFLICT`，建议数量为 0，等待人工确认。

用户汇总表只读取 `全部SKU` 中的 `原始SKU`、`产品型号`、`格式检查`；工作簿
其他文字不作为程序指令。上传表是参考资料，不能作为映射批准或停补解除依据。
不得把待审核 Excel 直接通过旧 `import sku` 命令导入正式映射。

映射状态和补货状态分别存储。最终映射状态包含
`AUTO_MATCHED_PENDING_REVIEW`、`APPROVED`、`REVIEW_REQUIRED`、`FAMILY_CONFLICT`。
`DISCOVERED` 是首次发现审计事件；`STOPPED` 是补货状态，`INACTIVE` 必须人工
确认，API 暂未返回只标 `SUSPECTED_INACTIVE`。批准映射不等于销量、库存、ETA
和停补策略已经全部验收通过。

同站点系列中的有效停补标记保护该系列全部子体，包括新增匹配子体。
候选匹配只允许安全停补，不能自动批准或自动补货。停补不会因销量、缺货或
新增子体解除，人工恢复只解除指定触发 SKU 的标记，其余触发标记继续生效。
所有发现、规则批准、映射批准、停补和人工恢复均保存本地审计。

## 命令

首次基线与审核文件：

```bash
fba-plan --db output/sku-review/review.db --marketplace US discover-skus \
  --confirm-read-only --source-xlsx /path/to/SKU-summary.xlsx \
  --catalog-sample-size 20 --output-dir output/sku-review
```

只读 Catalog 探针最多一批 20 个 ASIN；403 记录错误，不猜测父子体关系。
每日库存发现可不重复调用 Catalog：

```bash
scripts/daily_sku_discovery.sh
```

脚本通过 `--daily` 检查站点业务日期，当天已完整成功则不请求 Amazon。
它读取保存的用户参考资料路径，每次实际运行都重新采集全量当前库存，不复用
其他卖家或前一天的快照。实际 `sync` 每次也在完整只读库存上运行发现；发现
与同步快照同一 SQLite 事务提交。失败时上次有效数据和人工状态保留。

每日运行需要外部调度器在同一配置的执行环境中调用该脚本。托管云任务不保证
跨任务存在常驻进程，本项目不写出密钥环境文件、不建立凭据 cron 文件，也未
擅自启动常驻调度服务。调度器必须由平台为每次任务注入绑定、代理及 CA 信任。

只有用户完成审核后，才可显式操作本地映射；以下仅为命令格式，当前真实 SKU
没有执行批准、停补或恢复：

```bash
fba-plan --db output/sku-review/review.db approve-sku SKU --family FAMILY --approved-by REVIEWER
fba-plan --db output/sku-review/review.db approve-naming-rule \
  --name RULE --pattern '^(J[0-9]+)-size[ ]+[0-9]+(?:[.][0-9]+)?$' \
  --family-group 1 --approved-by REVIEWER
fba-plan --db output/sku-review/review.db stop SKU --reason REASON
fba-plan --db output/sku-review/review.db resume SKU --approved-by REVIEWER
```

批准命名规则会使新匹配 SKU 成为待审核候选，并在必要时立即安全停补，仍不
自动批准正式映射。已有人工映射不会被规则或上传表覆盖。

## 输出与数据范围

- `sku_series_mapping_review.xlsx`：SKU、ASIN、候选系列、依据和审核字段。
- `replenishment_stop_list.xlsx`：SKU、正式/候选系列、停补决定、原因、生效日及审核字段。
  未填写停补决定保持未知，不默认“未停补”。
- `replenishment_review_blocked.xlsx`：原补货/库存/审计表及“新发现 SKU”、
  “待审核 SKU”、“SKU 系列映射”、“已停补系列”、“系列映射冲突”。

本轮按用户指示暂停 90 天扩量。20 个小批量订单、24 条订单行已通过字段与
完整库存身份核验，但销量窗口尚未完整，7/30/60/90 天销量不能填零或用于补货。
2024 入仓计划和 Catalog 关系探针均返回 403；保留已验证的入仓 v0，不能据此
确定具体缺少哪个角色。未执行业务 API 写入或实际补货。

日期分片续传、分页限流及脱敏缓存能力已经实现，分页/访问 Token 始终只在
内存中。缓存仅保存白名单业务字段、数量、完整性校验和日期边界。不同卖家必须
使用独立数据库和缓存目录；同市场 ID 不等于同卖家身份。
