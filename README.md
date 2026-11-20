# 风险分阈值规则的适用性评估

各表为本地合成业务记录，身份与机构代号不对应真实对象。

## 资料格式

资料为 UTF-8 编码、首行为列名的 CSV。空字段表示没有记录，不表示零。连续日号采用同一份内部日历。

| 文件 | 列 | 登记口径 |
| --- | --- | --- |
| `rules.csv` | `version`、`cutoff`、`scale`、`live_from`、`live_until` | 规则分数及不可互换的量表；live_until 包含当天。 |
| `orders.csv` | `order`、`merchant`、`version`、`day`、`raw_score`、`price_band`、`first_purchase` | 交易时的不可变规则、分数与下单前特征；price_band 为 1–3 档。 |
| `receipts.csv` | `order`、`revision`、`known`、`version`、`shown`、`reason` | 同一展示回执可订正；版本不符不等同于没有展示。 |
| `outcomes.csv` | `order`、`revision`、`known`、`followup_days`、`state` | 售后观察覆盖，clear 表示给定窗口完整随访且无争议。 |
| `score_reviews.csv` | `order`、`known`、`updated_score` | 事后复核分数，仅供后续业务处置。 |
| `plans.csv` | `plan`、`asof`、`version`、`cutoff`、`bandwidths`、`min_per_side`、`max_unknown`、`min_gain`、`min_jump`、`horizon`、`active_on`、`status` | 计划所能接受的分数半带宽、成熟订单每侧数、未结上限、效果幅度与提醒展示跳变要求。 |

## 检查资料

使用 Python 3.11 和标准库。在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
```

这项检查核对资料格式与引用，不生成业务结论。

## 阈值审议引擎（`src/review`）

在资料契约之上实现直播购买确认页"额外提醒"阈值的局部（断点邻域）审议，
仅依赖 Python 3.11 标准库。隔离模式运行，数据目录只以参数传入：

```sh
python3 -I src/run_review.py --data data --outdir reports
```

输出 `reports/review.md`（人读审计报告）与 `reports/review.json`（机器可读）。
审议测试与资料契约测试一起由上面的 `unittest` 命令执行。

### 固定口径（与业务约定逐条对应）

| 约定 | 引擎处理 |
| --- | --- |
| 自然对照取旧规则在阈值两侧的订单 | 只按下单时不可变 `version` + `raw_score` 相对规则 `cutoff` 划分资格侧 |
| 资料按各自时间到达、回执/售后可订正 | 每个计划在"提交 asof"与"订正后当前 asof"两个视图分别重判，修订按 `revision` 取最新 |
| 复核分数改处置不改既得展示、不移动阈值侧 | `score_reviews` 显式列示并排除，不参与资格与窗口划分 |
| 一订单一观察；未结不当无争议、不删单 | 未结（非 clear/dispute）计入未结比例闸口，仅从标签率分母剔除，两本账同时呈现 |
| 同店订单不独立 | 差值检验使用按 `merchant` 聚类的 Liang–Zeger 稳健 SE |
| 不同量表不能混比 | r1(raw-v1) 与 r2(composite-v2) 绝不进入同一局部比较；请求 cutoff 与规则 cutoff 不一致即退回 |
| 资格效果 vs 实际展示差异 | ITT（规则资格）用于裁决；实际 shown/not-shown 差异仅作描述性对照，报告明确非因果 |
| 多带宽或未知标签翻转即不切换 | 每带宽过数值闸口后再做未结极端赋值敏感性，任一翻转即 hold |
| 已执行动作保留、失效证据不能支撑新切换 | `executed` 计划裁决 `retained`；`pending` 需双视图一致支持且无推翻性订正才 `switch` |
| 可回看"什么挡住了哪个切换" | 每个被挡带宽记录闸口名、门槛值与实际值，附完整审计轨迹 |

裁决取值：`switch`（双视图全部带宽过闸且不翻转）、`hold`（证据不足或翻转，维持旧规则）、
`return_scope`（请求阈值与规则实际阈值无对应邻域，退回缩小范围或补证）、
`retained`（已执行计划的旧规则动作保留，重算仅留痕）。

### 本批数据的裁决摘要

- **p1（r1，cutoff=50，pending）→ hold**：bw=4 的 ITT 增益方向为负（约 -0.06）；
  bw=8/12 数值过闸但未结极端赋值增益跌破 `min_gain`（0.023/0.028 < 0.03），
  未知标签可翻转建议；各带宽聚类稳健检验均不显著（p≈0.10–0.35）。双视图一致不支持切换。
- **p2（r2，目标 cutoff=60，pending）→ return_scope**：r2 规则实际 cutoff=56，
  全量证据下 56 处展示率跳变约 0.60–0.70，而请求的 60 分处仅 0.31–0.51，
  60 分邻域没有规则制造的自然对照；且 r2 分数仅取偶数、阈值附近稀疏，
  提交 asof=54 时无成熟贴标签样本。退回缩小范围或补证，不得以"无显著差异"批准扩张。
- **p0（r1，cutoff=50，executed）→ retained**：已执行的旧规则动作保留；
  重算显示 bw=8 单独过闸而 bw=4 不过（带宽翻转），仅作证据留痕，不产生切换或回滚。
- 两处真订正：`o0088` 回执 rev1 错标 r2、rev2 订正回 r1（shown 始终为 1，
  按"版本不符≠无展示"保留，不改估计）；`o0029` 售后 appeal→dispute 改判（改变估计口径，
  已纳入重判）。其余 rev1 晚到记录由 asof 视图与成熟度窗口正常处理。
