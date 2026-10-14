# SKU 按系列批量审核

此功能仅修改本地映射、停补策略和审核审计。生成审核队列不调用 Amazon；
批量批准、拒绝和停补导入也没有 Amazon 写入接口。本轮使用已保存的真实库存
和有限订单样本，不扩展 90 天销量、不部署每日调度、不替用户批准任何 SKU。

## 审核入口与明确集合

```bash
fba-plan --db output/sku-review/review.db prepare-batch-review \
  --output-dir output/batch-review \
  --sales-sample output/live-acceptance-2026-10-13/sample.json --asof 2026-10-13
```

输出 `sku_batch_review.xlsx` 和 `confirmed_stop_list_import_template.xlsx`。
优先查看“系列优先审核”“数据异常”“精确匹配优先”，再在“SKU明确集合”
核实每组全部成员。不同来源的同名候选分开成组，避免将未核实命名候选混入
852 个精确匹配的高优先队列。

每组都保存明确的 Marketplace API ID + SKU 成员、ASIN、原始状态、固定集合
校验值。工作簿不是通配规则；批准某组只批准那次枚举的 SKU。未来同前缀、
同型号新增 SKU 不会自动获得映射批准。未知命名格式桶只用于分流，不能一键
把全部成员当成同一系列。

## 候选及业务证据

人工已批准映射优先保留。精确参考表匹配仍待审核；格式异常和系列冲突分开
处理。命名候选必须整串匹配：明确型号+枚举颜色、型号+尺寸、配对尺寸，或
合法日期+中段明确型号+变体。不会简单按第一个连字符截系列。日期、LCS 物流
前缀不是系列；所有这些新语法只生成候选，不批准命名规则或映射。

Listing 状态没有可信只读证据时一律 `UNKNOWN`。当前 FBA 库存/在途正数量
证明有库存数量，不证明在售。完整且新鲜的六类库存全零只表示该快照的零值，
不等于下架。预留、不可售和入仓数量均展示，入仓详细字段独立检查采集时间，
过期或缺失保持未知。

近期销量只有正向样本证据时标 `POSITIVE_SAMPLE_ONLY`，记录观测发运量及
来源；样本未出现的 SKU 保持 `UNKNOWN`，不能填零。取消订单和已执行取消的
行不提供销量优先级证据。只有 7 天逐日总数完整时才可声称完整窗口零销量。
没有充分历史/下架证据时历史状态保持未知；API 暂未返回不删除记录。

优先级依据公开显示：P0 为系列/格式异常，以及观测到销售但可售为零/未知；
精确匹配和现存库存提升优先级；证据不足保留未知。它只是人工审核排序，
不推算完整日销量、销售速度或自动补货数量。

## 填写与导入

“批量审核决定”默认 `PENDING`。核实明确成员后，只填写需要操作的行：

- `decision`：`APPROVED` 或 `REJECTED`。
- `approved_family`：批准时填写已核实系列，必须与该冻结组目标一致。
- `reviewer`：可追溯审核人；与导入命令的审核人一致。
- `reason`：审核依据或拒绝原因。

不要修改 snapshot/group/manifest 标识、校验值、候选值或 SKU 成员表。
成员增删、ASIN 改变、原映射/停补状态变动、跨站点或过期集合会使整批失败。
所有选定组在同一事务里应用，之后仅一次刷新系列安全状态；后续组失败会
共同回滚。完整原始值、变更、人、时间及原因保存在 SQLite 审核审计中。

用户确实审核之后才执行：

```bash
fba-plan --db output/sku-review/review.db --marketplace US import-batch-review \
  output/batch-review/sku_batch_review.xlsx --reviewer REVIEWER --confirm-reviewed
```

默认不覆盖已有人工正式系列。确认改系列或解决冲突需对应显式标志
`--allow-remap`、`--resolve-conflicts`，身份缺失/ASIN 矛盾仍必须先解决。
如果原候选不对或未知组需要拆分，先重新定义精确成员及正确系列：

```bash
fba-plan --db output/sku-review/review.db create-review-scope \
  --family CONFIRMED_FAMILY --sku EXACT_SKU_1 --sku EXACT_SKU_2 --created-by REVIEWER
# 核实返回的固定集合和校验值以后，才显式批准：
fba-plan --db output/sku-review/review.db approve-batch \
  --manifest-id MANIFEST_ID --member-digest SHA256 --family CONFIRMED_FAMILY \
  --reviewer REVIEWER --reason 'Confirmed these exact members' --confirm-scope
```

创建集合不批准映射；拒绝记录不解除任何人工停补。批准映射也不代表销量、
库存、ETA 等计算条件已经验收，其他安全拦截继续生效。
从发现系统进入的 SKU 若没有验证通过的入仓采集，计算会继续以
`INCOMPLETE_INBOUND` 拦截；空货件表不能自动解释为没有在途或没有未知 ETA。

## 停补名单

在“停补导入”填明确 SKU 或已确认系列、`stop=TRUE`、原因、生效日期
`YYYY-MM-DD`、`review_status=APPROVED`、审核人。待审核候选不能作为正式
系列停补导入依据。只有已批准的正式系列内，才应用同站点全部子体联动。

```bash
fba-plan --db output/sku-review/review.db --marketplace US import-stop-list \
  output/batch-review/confirmed_stop_list_import_template.xlsx --reviewer REVIEWER
```

空白或 FALSE 只表示不作变更，不恢复补货；恢复继续使用明确人工批准的
`resume --approved-by`。未来停补按站点业务时区生效（US 当前为
America/Los_Angeles），不提前应用。不启动调度器；计划计算时检查到期政策。
映射/ASIN/停补状态在生效前改变，会转为 `CONFLICT_PENDING` 等待再次确认，
不擅自把停补迁到新系列。同一个旧文件重复导入幂等，人工恢复后也不会因
重导旧文件自动重新停补。

候选只可使待审 SKU 安全拦截，不能写成永久系列停补去影响其他已批准系列。
本轮修复了这一候选污染问题，人工正式系列拥有明确优先级。

## 本轮工作量口径

15,932 个 SKU 中，823 个无格式异常的精确匹配归为 307 组，29 个格式异常
单独处理；852 个优先 SKU 理论由 852 次逐 SKU 决定变为 336 次，减少
516 次（60.56%）。另为 6,489 个 SKU 生成待审命名候选；8,591 个仍未知。

全队列 1,726 个可批量组覆盖 7,312 个 SKU，未知和异常仍按逐 SKU 决定计。
因此理论 15,932 次可变为 10,346 次，减少 5,586 次（35.06%）。这是假定用户
检查每组全部成员之后一次作出决定的次数估算，不等于实际耗时；审查错误
候选、拆组或补充证据会增加工作量。
