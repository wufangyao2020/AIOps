# A股海外产业信号雷达

## 目标

把海外产业变量转化为可审计的A股盘前情报：

```text
海外指数 / 商品 / 政策 / 临床 / 公司披露
                ↓
阈值判断与P1/P2/P3分级
                ↓
A股直接度、方向、权重映射
                ↓
22:15晚间初报 + 06:35盘前终报
                ↓
GitHub报告 + P1/P2 Issue提醒
```

本系统不是自动交易程序。它负责发现“开盘前值得复核的变化”，不替代公司公告、财报、估值和仓位纪律。

## 当前覆盖

- 海外信号：37条；
- A股公司：97家；
- 信号—股票映射：184条；
- 最终20只候选：全部进行海外敏感度审计，其中5只明确标为“低敏感度”，不强行套海外叙事；
- 映射直接度：A类67条、B类86条、C类31条。

|信号域|典型数据|主要A股方向|
|---|---|---|
|航运|BDI/BCI/BPI/BSI、BDTI/BCTI、BLNG/BLPG、FBX、BAI00|干散货、油轮、集运、航空货运|
|能源|Brent、WTI、Henry Hub|油气上游、航空、LNG/LPG进口、煤化工相对成本|
|金属|铜、铝、黄金、白银|矿山、冶炼、电解铝、贵金属|
|农业|玉米、大豆、小麦、原糖、棉花、WASDE|饲料、养殖、糖业、棉纺、种植|
|半导体|台积电月营收、SIA全球销售、SOX、云厂商SEC披露|光模块、PCB、服务器、存储、设备|
|生物医药|ClinicalTrials.gov、艾伯维SEC披露|荣昌生物及合作管线|
|政策|HFC、纸浆模塑贸易、电池/EV贸易、输电政策|制冷剂、环保包装、电池、电网设备|
|汽车|ACEA欧洲纯电注册份额|欧洲汽车零部件供应商|
|汇率与利率|USD/CNY、EUR/CNY、美国10年期收益率|出口、进口成本、全球估值|

## 分级规则

- **P1一级**：单日或短期变化可能改变次日板块定价，A股开盘前必须复核；
- **P2二级**：趋势增强、突破关键区间或出现关键事件，进入重点观察；
- **P3三级**：建立基线或记录变化，不提高交易优先级。

单一信号不能直接触发买入。A类映射也至少需要以下一项交叉验证：

1. 公司公告、订单、临床或监管里程碑；
2. 下一季扣非利润与经营现金流；
3. 股价相对行业表现。

## 直接度

- **A类**：公司收入、成本或资产与该变量直接相关，例如BDI—海通发展、铜价—北方铜业；
- **B类**：相关性真实，但会被合同、长协、套保、产品结构或国内价格削弱；
- **C类**：只用于宏观或市场确认，不作为基本面结论。

## 两个运行窗口

- **北京时间22:15，周一至周五**：晚间初报，重点吸收波罗的海、欧洲政策和亚洲收盘数据；
- **北京时间06:35，周一至周六**：盘前终报，纳入美国商品、SEC和夜间数据。周六运行用于下周一准备。

## 主要文件

```text
config/global_signal_catalog.json         信号、来源与阈值
config/global_signal_stock_map.csv        184条A股映射
config/final20_overseas_sensitivity.csv   最终20只敏感度审计
scripts/global_signal_radar.py            抓取、计算、映射、报告和告警
scripts/validate_global_signal_radar.py   配置硬校验
.github/workflows/global-signal-radar.yml 定时运行和Issue提醒

data/global_signals/numeric_history.csv   数值历史
data/global_signals/event_history.jsonl   事件历史
data/global_signals/latest.json           最新结构化结果
data/global_signals/latest_alert.json     最新告警指令
reports/global_signals/latest.md           最新报告
```

## 数据源纪律

- A级：波罗的海交易所、FRED、台积电IR、SIA、ClinicalTrials.gov、Federal Register、SEC、USDA、ACEA；
- B级：公开市场行情接口，用于期货、汇率和SOX价格代理；
- C级：仅用于海外风险偏好与价格确认。

任何数据源抓取失败时必须明确记录，不得用旧数据伪装成最新数据。

## 最终20只的处理原则

海外信号只覆盖真正具有外部敏感度的公司。海正药业、济川药业、亚宝药业等主要依赖国内经营的公司，不会因为“必须覆盖20只”而被强行绑定某个海外指数。

## 重要限制

1. 波罗的海交易所公开网页可获得Headline Data，但完整历史和部分路线数据可能需要会员/API授权。本系统从启用日起在仓库内积累历史；
2. 期货价格不等于上市公司结算价，套保、长协、汇率和产品结构会改变利润弹性；
3. 铝价必须同时看氧化铝和电力，铜价必须看自产矿比例与TC/RC，航运必须看船型和实际TCE；
4. 政策事件可能同时包含利好与利空，映射方向为0时只报警，不自动打多空分；
5. 临床状态更新不等于临床成功，关键仍是数据读出、监管沟通和里程碑付款。

## 手动运行

```bash
python -m pip install pandas requests beautifulsoup4 lxml html5lib
python scripts/validate_global_signal_radar.py
python scripts/global_signal_radar.py
```

也可新建标题为 `[global-signal-radar] run` 的Issue触发一次运行。成功后该Issue会自动关闭。
