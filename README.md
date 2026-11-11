# 时序与关系数据质量治理

本仓库包含两套独立实验。Intel Lab 评估温度、湿度、光照和电压序列，治理时间戳和数值。FDA MAUDE 评估历史主表和 2026 年主表中的厂家报告。

## 八项指标的计算位置

### Intel Lab 时序数据

`N` 为单条序列的输入点数。全量分数按各项指标的分母加权汇总。

| 指标 | 计算公式 | 代码 |
| --- | --- | --- |
| 完整性 | `1 − (缺失采样点数 + 非有限值个数) / (N + 缺失采样点数)` | [完整性计算](quality_governance/timeseries/scoring.py#L104) |
| 一致性 | `1 − 冗余点数 / N` | [一致性计算](quality_governance/timeseries/scoring.py#L105) |
| 时效性 | `1 − 推断延迟点数 / N` | [时效性计算](quality_governance/timeseries/scoring.py#L106) |
| 有效性 | `1 − 四类异常计数之和 / (4 × N)` | [有效性计算](quality_governance/timeseries/scoring.py#L107) |

时效性根据采样时间戳的间隔推断。有效性检查原值、相邻差值、变化速率和相邻速率之差。每一类中，偏离该类中位数超过 `3 × 1.4826 × MAD` 即计为异常，MAD 是这些绝对偏差的中位数。[Apache IoTDB TimeSeriesQuality 源码](https://github.com/apache/iotdb/blob/fc6f68c213230d0bb166420f901f3bc8b00663ba/library-udf/src/main/java/org/apache/iotdb/library/dquality/util/TimeSeriesQuality.java)

### FDA MAUDE 关系数据

| 指标 | 分子 | 分母 | 代码 |
| --- | --- | --- | --- |
| 完整性 | 厂家首次获知日期字段非空的报告数 | 被评估的厂家报告数 | [完整性计算](quality_governance/relational/scoring.py#L30) |
| 一致性 | 五位邮编和四位扩展码均与同条报告的完整邮编匹配的报告数 | 美国联系地址中，完整 ZIP+4 可解析且两个分段字段均非空的报告数 | [一致性计算](quality_governance/relational/scoring.py#L31) |
| 时效性 | FDA 收件日期减厂家首次获知日期在 0 至 30 个自然日内的报告数 | 两日期均可解析且时间差非负的报告数 | [时效性计算](quality_governance/relational/scoring.py#L32) |
| 有效性 | 邮编扩展码恰为四位 ASCII 数字的报告数 | 美国联系地址中，邮编扩展码非空的报告数 | [有效性计算](quality_governance/relational/scoring.py#L33) |

治理时保留原始日期差为 0 至 30 个自然日的普通初始报告，再按同一条报告的完整邮编修正分段字段。

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

### Intel Lab 四变量全量实验

治理前后使用同一批可评分序列。

| 处理阶段 | 完整性 | 一致性 | 时效性 | 有效性 |
| --- | ---: | ---: | ---: | ---: |
| 治理前 | 73.52% | 98.63% | 98.10% | 82.98% |
| 治理后 | 100.00% | 99.89% | 100.00% | 96.54% |

治理后的分数包含估计的采样点。只在原始行和原始时间戳上复算，治理后有效性为 94.48%。

### FDA MAUDE 历史及 2026 年主表厂家报告

| 处理阶段 | 完整性 | 一致性 | 30 天时效性 | 有效性 |
| --- | ---: | ---: | ---: | ---: |
| 治理前，全部厂家报告 | 85.96% | 82.60% | 87.42% | 82.59% |
| 筛选及时报告后，修正邮编前 | 100.00% | 93.1810% | 100.00% | 93.1756% |
| 修正邮编后 | 100.00% | 100.00% | 100.00% | 99.9942% |

筛选保留了全部厂家报告的 58.92%。两列日期沿用原值，完整性与时效性的变化来自保留范围的变化。
