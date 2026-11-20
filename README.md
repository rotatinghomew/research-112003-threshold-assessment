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
