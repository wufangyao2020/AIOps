# A 股 2026 年中报全量扫描档案

本仓库归档 **2026 年半年报季 5,550 家 A 股公司母池**，并为后续四模型盲扫提供可复现、可审计的数据入口。

## 当前状态

- 报告期：`2026H1`
- 截止口径：`2026-08-31`
- 当前阶段：`Stage 0 — universe archive completed`
- 主归档数量：**5,550 家**
- GitHub Actions：**抓取、精确分区、维度画像、数量校验、提交全部成功**
- 已完成：母池 CSV / JSONL、交易所拆分、研究状态表、审计排除表、校验和、自动刷新流程
- 未完成：基本面加速评分、新利润池评分、周期拐点评分、资本事件评分、前 100 家公告复核、同行比较、最终 20 只候选

## 核心文件

| 文件 | 用途 |
|---|---|
| [`data/2026H1/a_share_2026_h1_5550_master.csv`](data/2026H1/a_share_2026_h1_5550_master.csv) | 5,550 家公司标准化主表，适合表格分析 |
| [`data/2026H1/a_share_2026_h1_5550_master.jsonl`](data/2026H1/a_share_2026_h1_5550_master.jsonl) | 5,550 家公司 JSONL，适合程序与 Agent 处理 |
| [`status/research_status_2026_h1.csv`](status/research_status_2026_h1.csv) | 每家公司的四模型、公告复核与同行比较状态 |
| [`meta/snapshot_2026_h1.json`](meta/snapshot_2026_h1.json) | 数量口径、来源分布、排除规则与研究阶段元数据 |
| [`meta/checksums_2026_h1.sha256`](meta/checksums_2026_h1.sha256) | 核心文件 SHA-256 校验和 |
| [`docs/research_protocol.md`](docs/research_protocol.md) | 盲扫、反偏见、四模型分类与人工复核协议 |

## 5,550 口径如何得到

公开源一次返回 11,446 条半年报相关记录，其中包括：

- A 股：5,565 条；
- 中国存托凭证：1 条；
- B 股：79 条；
- 三板股：5,801 条。

先按证券类型和沪深北交易市场代码排除 B 股、新三板与老三板，得到 5,566 条 A 股/CDR 记录；再按照 2026 年 8 月 31 日截止口径排除：

- 截止日前尚未上市的 IPO 记录：14 条；
- 中国存托凭证：1 条；
- 截止日后记录：1 条。

最终形成 **5,550 家纯 A 股公司归档**。所有排除项均保留在 `meta/` 审计文件中，没有通过任意截断或人为凑数得到结果。

> 注：当前已上市的 `002731（*ST 萃华）`未在报告源中出现已完成的 2026 年半年报，因此不在这份“已披露半年报公司母池”内。

## 目录

```text
.
├── data/2026H1/
│   ├── a_share_2026_h1_5550_master.csv
│   ├── a_share_2026_h1_5550_master.jsonl
│   ├── a_share_2026_h1_source_current.csv
│   ├── a_share_2026_h1_raw.jsonl
│   ├── excluded_non_a_share_rows.jsonl
│   └── by_exchange/
├── data/current/
├── status/
├── meta/
├── config/research_models.json
├── schema/
├── scripts/
└── docs/research_protocol.md
```

## 数据来源与边界

母池通过东方财富数据中心公开接口抓取；正式财务与法律判断仍以沪深北交易所及上市公司法定披露文件为准。

数据页：`https://data.eastmoney.com/bbsj/202606/yjbb.html`

## 自动更新

GitHub Actions 支持定期刷新，也可以通过标题为 `[archive] run 2026H1` 的 Issue 触发。工作流会：

1. 拉取完整源数据；
2. 按证券类型、交易市场、上市日期和截止日期生成精确母池；
3. 生成 CSV / JSONL、交易所拆分与研究状态文件；
4. 校验数量、重复代码、证券类型和审计分区；
5. 计算校验和并提交快照。

## 研究纪律

- 第一轮候选生成不使用历史聊天频次；
- 熟悉公司不保送，热门题材不自动加分；
- 四套模型独立评分，不为凑数强行平均分配名额；
- 量化初筛后至少逐家复核前 100 家的中报、年报、一季报、重大公告与同行数据；
- 完成同行比较和证伪审查后，才生成最终 20 只候选。
