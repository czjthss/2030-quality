# 时序与关系数据质量治理

本仓库包含两套独立实验。Intel Lab 按设备和变量评估温度、湿度、光照和电压序列，处理时间戳和数值后重新评分。FDA MAUDE 流式读取历史主表和 2026 年主表中的厂家报告，按原始日期保留可判定及时的报告，再按同条报告中的完整邮编修正分段字段。

## 八项指标的计算位置

### Intel Lab 时序数据

以下公式按单条序列计算，`N` 是该序列的输入点数。完整性、一致性和时效性共用一个时间戳检测窗口。汇总时先累加各序列的错误数和分母，再计算比率。

| 指标 | 计算公式 | 代码 |
| --- | --- | --- |
| 完整性 | `1 − (缺失采样点数 + 非有限值个数) / (N + 缺失采样点数)` | [`time_counts`](quality_governance/timeseries/scoring.py#L24)、[`metric_counts`](quality_governance/timeseries/scoring.py#L99) |
| 一致性 | `1 − 冗余点数 / N` | [`time_counts`](quality_governance/timeseries/scoring.py#L24)、[`metric_counts`](quality_governance/timeseries/scoring.py#L99) |
| 时效性 | `1 − 推断延迟点数 / N` | [`time_counts`](quality_governance/timeseries/scoring.py#L24)、[`metric_counts`](quality_governance/timeseries/scoring.py#L99) |
| 有效性 | `1 − 四类异常计数之和 / (4 × N)` | [`outliers`](quality_governance/timeseries/scoring.py#L90)、[`evaluate`](quality_governance/timeseries/scoring.py#L115)、[`metric_counts`](quality_governance/timeseries/scoring.py#L99) |

`time_counts` 用相邻间隔的中位数作参照，在 10 点窗口中区分过密点、推断延迟点和缺失采样点。时效性反映时间戳的间隔模式。有效性分别检查原值、相邻差值、变化速率和相邻速率之差。在每一类中，观测与该类中位数的绝对差超过 `3 × 1.4826 × MAD` 即计为异常，MAD 为该类绝对差的中位数。`evaluate` 评估一条序列，`pool` 汇总多条序列。实现依据固定版本的 Apache IoTDB。[固定版本的 TimeSeriesQuality](https://github.com/apache/iotdb/blob/fc6f68c213230d0bb166420f901f3bc8b00663ba/library-udf/src/main/java/org/apache/iotdb/library/dquality/util/TimeSeriesQuality.java)

| 处理步骤 | 代码 |
| --- | --- |
| 尝试将疑似延迟点移到邻近空位，补短缺口，拉开过密点 | [`timestamp_repair.py`](quality_governance/timeseries/timestamp_repair.py) 的 `repair_timestamps` |
| 补数值和调整异常读数，按需尝试降噪或趋势拟合 | [`value_repair.py`](quality_governance/timeseries/value_repair.py)、[`smooth_candidate.py`](quality_governance/timeseries/smooth_candidate.py)、[`trend_model.py`](quality_governance/timeseries/trend_model.py) |
| 串联修复并独立计分，按设备和变量跑全量实验 | [`repair.py`](quality_governance/timeseries/repair.py) 的 `repair_series`、[`benchmark.py`](quality_governance/timeseries/benchmark.py) |

### FDA MAUDE 关系数据

关系数据的四项指标都在 `relational/scoring.py`。程序逐条累加计数，再用各自的分母计算比率。分母为零时输出 `null`。[`count_report`](quality_governance/relational/scoring.py#L37) · [`Counts.export`](quality_governance/relational/scoring.py#L26)

| 指标 | 分子 | 分母 | `Counts` 中的分子与分母 |
| --- | --- | --- | --- |
| 完整性 `C` | 厂家首次获知日期字段非空的报告数 | 被评估的厂家报告数 | `manufacturer_date_present` / `records` |
| 一致性 `S` | 五位邮编和四位扩展码均与同条报告的完整邮编匹配的报告数 | 美国联系地址中，完整 ZIP+4 可解析且两个分段字段均非空的报告数 | `consistency_pass` / `consistency_eligible` |
| 时效性 `T_30` | FDA 收件日期减厂家首次获知日期在 0 至 30 个自然日内的报告数 | 两日期均可解析且时间差非负的报告数 | `within_30_days` / `delay_eligible` |
| 有效性 `V` | 邮编扩展码恰为四位 ASCII 数字的报告数 | 美国联系地址中，邮编扩展码非空的报告数 | `validity_pass` / `validity_eligible` |

| 处理步骤 | 代码 |
| --- | --- |
| 从 ZIP 流式读取主表和解析行 | [`reader.py`](quality_governance/relational/reader.py) |
| 判定报告类型和原始日期差 | [`dates.py`](quality_governance/relational/dates.py) 的 `is_ordinary_initial`、`date_status` |
| 根据完整 ZIP+4 修正两个分段字段 | [`postal.py`](quality_governance/relational/postal.py) 的 `correct_contact_zip` |
| 串联筛选、邮编修正、评分和导出 | [`maude.py`](quality_governance/relational/maude.py) 的 `export_subset` |

普通初始报告筛选接受 `Thirty-Day` 或 `Initial` 标签，要求报告阶段含 `I`，并排除有汇总或豁免信号的记录。程序只保留原始日期差 `DATE_RECEIVED − DATE_MANUFACTURER_RECEIVED` 为 0 至 30 个自然日的记录，然后修正邮编并重新评分。完整性和时效性是在保留集合上计算的。一致性与有效性共用邮编修正规则。

## 安装与运行

使用 Python 3.11 或更高版本。从仓库根目录执行。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[trend]'
python -m unittest discover -s tests -v
mkdir -p data runs
```

仅运行 MAUDE 可用 `python -m pip install -e .`。不启用趋势拟合的时序处理只需 `python -m pip install -e '.[timeseries]'`。

**Intel Lab 单条序列。** CSV 至少包含 `timestamp,value` 两列。时间戳为单位一致的整数，至少输入 11 个点，其中至少两个不同时间戳有有限数值。可用 `--nominal-interval` 指定已知采样周期。输出保留来源行号，并标记补点、移时、插补和数值修改。

```bash
python -m quality_governance.timeseries.repair data/series.csv runs/repaired.csv
```

**Intel Lab 全量实验。** 从 [Intel Lab 数据页](https://db.csail.mit.edu/labdata/labdata.html) 下载 [原始日志](https://db.csail.mit.edu/labdata/data.txt.gz)，保存为 `data/intel.txt.gz`。数据由 Peter Bodik、Wei Hong、Carlos Guestrin、Sam Madden、Mark Paskin 和 Romain Thibaux 等人采集。结果 JSON 包含逐序列与按变量汇总的分数，也会列出无法评分的序列。

```bash
python -m quality_governance.timeseries.benchmark \
  --input data/intel.txt.gz --report runs/intel.json --trend-model
```

可用 `--device 12 --variable temperature --export runs/intel12.csv` 单独运行并导出设备 12 的温度序列。

**FDA MAUDE 全量输入。** 从 [FDA MDR Data Files](https://www.fda.gov/medical-devices/medical-device-reporting-mdr-how-report-medical-device-problems/mdr-data-files) 下载 `mdrfoithru2025.zip` 和 `mdrfoi.zip`。程序将保留记录写入两份带表头的 gzip 压缩竖线分隔文件，并输出 `maude_timely_subset_scores.json`。输出目录须为新目录。

```bash
python -m quality_governance.relational.maude \
  --archive data/mdrfoithru2025.zip --current data/mdrfoi.zip --output runs/maude
```

FDA 文件会更新。若持有实验所用的两份冻结快照，可加 `--verify-frozen` 核验输入版本和保留条数。下载到更新版文件时，运行结果随输入而变。

## 实验结果

| 评分范围 | 完整性 | 一致性 | 时效性 | 有效性 |
| --- | ---: | ---: | ---: | ---: |
| Intel Lab 治理后，四变量 224 条可评分序列汇总 | 100.00% | 99.89% | 100.00% | 96.54% |
| FDA MAUDE 治理后，筛选保留的 15,019,320 条厂家报告 | 100.00% | 100.00% | 100.00% | 99.9942% |

Intel Lab 表中分数包含估计的采样点。只在原始行和原始时间戳复算，治理后有效性为 94.48%。MAUDE 从 25,492,863 条厂家报告中保留 58.92%，在保留集合中修正了 117,934 条报告的邮编字段。两列原始日期直接沿用。
