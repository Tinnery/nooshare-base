# nooshare-base 需求说明书：Point-in-Time 正确的基础数据产品层

> 版本：v1.0（2026-11-12）
> 状态：已确认并交付（P1–P7 已实现，见 README 与 docs/）
> 产出方：软件研发团队 · 需求分析（基于对 nooshare 矩阵 5 个仓库的实代码调研）

---

## 0. 矩阵现状（调研结论）

| 仓库 | 语言 | 职责 | 数据库产出 |
|------|------|------|-----------|
| `nooshare-tong` | Go | TDX 日线采集：Extract → Adjust → Validate，CSV/TSV/RustFS/GitHub Release 交付 | `tong.stock_daily` / `tong.index_daily` / `tong.etf_daily` / `tong.gbbq` / `tong.symbol_meta` |
| `nooshare-indicators` | Python | 技术指标计算（收益/波动/动量/成交量/价格位置 + 截面标准化） | `indicators.daily_*` / `indicators.cross_section_stats` |
| `nooshare-uni` | Python | JQ 元数据解码（jmeta）+ 融资融券 + 财报三表 + 源注册/血缘/原始捕获 | `ju.*`（stocks/trading_day/pub_date/st_period/limit_ratio/valuation/name_history 等）+ `meta.*` + `raw.*` + `capital.*` + `finance.*` |
| `nooshare-annual-semi` | Python | 年报/半年报文本采集（cninfo/cfi/sina/9fzt 四通道）+ PDF/HTML→MD 转换 + 完整性验证 | annual_semi 自有表结构 |
| `nooshare-base`（本仓库） | Python（P1–P7 已实现） | **本需求书定义的对象**：跨 namespace 数据关联，产出 `public.*` | `public.*` |

### 已识别的现有资产（base 层可直接依赖）

- `tong` 仓库已具备：结构契约（contracts/*.go + Validate）、阻断性校验、manifest + SHA256SUMS、幂等写入（ON CONFLICT DO NOTHING）
- `uni` 仓库已具备：`meta.sources` 源注册（源身份是列不是 schema）、`meta.lineage` 血缘、`raw.captures` 不可变捕获（content_hash 去重）、`capital.margin_trading` 已示范 SCD2/PIT 观测元数据列（effective_from/effective_to/is_current/row_hash）
- `ju.pub_date`（~25 万行）已有财报实际披露日期 —— PIT 化的原料已存在，只是无人使用
- `indicators` 已具备：对数收益可加性验证标准（<1e-10）、TA-Lib 验证规划（<1e-6）、涨跌停 is_censored 标记

### 已识别的现有缺陷（base 层要解决的核心问题）

1. **标识体系分裂**：`tong.symbol`（sh600000）vs `ju.stocks.stock_code`（600000），靠 `indicators.data_loader.code_to_symbol()` 硬编码推断，北交所代码段（8/4/9）靠猜
2. **PIT 约束缺失**：`finance.*` 三表只有 `pub_date` 列但无可见性约束；ST/市值用区间快照而非事件投影；indicators 的财报消费无前视偏差防护
3. **无跨源对账**：tong/ju/finance 各源互相"信任输入"，无交叉验证与差异裁决机制
4. **数据新鲜度不可见**：下游无法在分析前得知源表是否 stale
5. **indicators 重复实现了 base 层职责**：日历对齐、pre_close、ST/涨跌停标记都写在 indicators.data_loader 里 —— 这些属于"基础数据准备"，应由 base 层下沉为公共能力

---

## 1. 定位（一句话）

nooshare-base 不是"数据搬运工"，而是整个 nooshare 矩阵的**数据产品发布层**：将分散在各 namespace 的原始数据加工为**分析就绪（analysis-ready）** 的 `public.*` 数据产品，保证任何下游消费者在任意时间点查询的结果 **point-in-time 正确** —— 零前视偏差、零生存偏差、可复现、可审计。

**"更深而非更大"的含义**：不做更多的事，而是保证已做的每一件事在时间维度上正确。

---

## 2. 顶尖专家会做、而现有矩阵没做的事（方法论依据）

### 2.1 双时态模型（Bi-temporal）

每行数据三个时间维度：

| 维度 | 含义 | 例子 |
|------|------|------|
| `effective_date` | 数据经济上属于哪天 | Q1 财报 → 2024-03-31 |
| `pub_date` | 数据何时被外界知道 | 2024-04-15 披露 |
| `observed_at` | 本系统何时抓取到 | 2024-04-15T20:33:00Z |

**核心铁律**：T 日分析只能看到 `pub_date ≤ T` 的数据。这是回测正确性的数学前提，不是可选优化。`uni.capital` 已示范该模式，`finance.*` 与 `public.*` 必须全面推广。

### 2.2 公司行为事件溯源（Event Sourcing）

所有公司行为建模为**不可变事件流**，状态表是事件流的确定性投影：

```
{type: "st_mark",      code: "000001", date: 20230315}
{type: "st_remove",    code: "000001", date: 20240620}
{type: "split",        code: "000001", date: 20230801, ratio: 2.0}
{type: "dividend",     code: "000001", date: 20230710, per_10shares: 5.0}
```

- `tong.gbbq` 已是原始事件（保留）；`ju.st_period`/`ju.valuation` 是区间快照（投影为事件）
- 任何时间点状态可确定性重建；新增数据源只需追加事件，不改模型

### 2.3 规范符号主数据（Canonical Symbol Master）

建 `public.canonical_symbol` 作为全矩阵标识 hub，所有 namespace 通过 `canonical_id` 关联，废除字符串推断：

```
canonical_id | exchange | code   | symbol   | asset_class | list_date  | delist_date
000001       | XSHE     | 000001 | sz000001 | stock       | 1991-04-03 | NULL
```

### 2.4 跨源对账（Cross-source Reconciliation）

对重叠数据交叉验证，差异**记录不修正**，人工裁决：

- `tong.stock_daily` 收盘价 vs 交易所官方日行情公报
- `ju.stocks` 股票列表 vs `tong.symbol_meta` 证券分类
- `ju.trading_day` vs 交易所官方休市安排
- `finance.*` EPS vs annual-semi 采集的年报原文关键数字

### 2.5 数据契约 + SLO

`public.*` 是对外发布的**数据产品**，下游只依赖其契约，禁止直读 `tong.*`/`ju.*`。沿用 tong 仓库的契约文化（contracts/*.go → 本仓库 contracts/*.py 或 YAML）。

### 2.6 幂等可复现管线

同一 `(输入快照, 代码版本)` → 确定性产物。每次运行记录 `public.pipeline_run`（输入哈希、代码 commit、产物行数、耗时），支持任意日期范围增量重算。

### 2.7 数据新鲜度看板

维护 `public.data_freshness`：每张源表的 `max_date / row_count / refreshed_at / status(fresh|stale|missing)`。下游分析前必须先检查新鲜度 —— 用过期数据回测是量化新手最常犯的致命错误。

---

## 3. 顶尖专家使用的原始数据（对账与扩展依据）

| 原始数据 | 来源 | 用途 | 矩阵现状 |
|---------|------|------|---------|
| 交易所官方日线行情公报 | SSE/SZSE 官方接口 | ground truth 校验 tong 数据 | ❌ 无 |
| 除权除息公告原文 | 巨潮资讯/交易所 | 校验 gbbq 正确性 | ❌ 无 |
| 交易所官方休市安排 | SSE/SZSE 每年 12 月发布次年 | 校验 trading_day 完整性 | ❌ 无 |
| 股本变更公告（增发/回购/激励行权） | 巨潮资讯 | 补全 gbbq category=5 语义 | 部分（gbbq 有事件但语义不完整） |
| 指数成分股调整公告 | 中证指数公司/国证指数公司 | 指数回测消除生存偏差 | ❌ 无 |
| 财报原文 PDF | 巨潮资讯 | annual-semi 已采集；提取结构化数字做对账 | 有 PDF，无结构化提取 |
| 行业分类（证监会/申万/中信） | CSRC/申万宏源/中信 | 板块分析、行业中性化 | ❌ 无 |
| 停复牌公告 | 交易所 | 区分"停牌"与"数据缺失" | ❌ 无 |
| 限售股解禁日程 | 中证登/巨潮 | 流通市值准确性 | ❌ 无 |
| 北向/南向资金逐股明细 | 港交所/沪深港通 | 资金流向分析 | ❌ 无（capital.margin_trading 是两融，非北向） |

> 采集这些原始数据**不在 base 层职责内**（Out of Scope），但 base 层的对账框架必须为它们预留接入位。

---

## 4. 专业工具与方法选型

| 领域 | 工具/方法 | 在本仓库的应用 |
|------|---------|--------------|
| 数据质量断言 | Great Expectations / Soda Core（或自研轻量断言，与 tong Validate 风格对齐） | public.* 发布前门禁 |
| 血缘追踪 | OpenLineage 标准（或直接复用 `uni.meta.lineage`） | 每行 public 数据可追溯到源 |
| 事件溯源 | Event Sourcing + CQRS 模式 | corporate_action_event 及其投影 |
| 双时态 | Bi-temporal 表模式（effective + observed） | *_pit 视图族 |
| 数据契约 | Schema Registry 思想 + 契约测试（contracts/ 目录版本化） | 防 schema 漂移破坏下游 |
| 幂等物化 | dbt 风格 incremental materialization（或自研 COPY + 临时表 upsert，沿用矩阵既有模式） | 可重放管线 |
| 对账 | Cross-validation matrix + 差异台账 | reconcile_status |
| 数据版本化 | 输入快照哈希 + 代码 commit → 产物哈希（DVC 思想，轻量实现） | 区分"数据变了"vs"计算变了" |

**技术栈约束**（与矩阵一致）：Python 3.10+ / pandas / numpy / psycopg2 / PostgreSQL 18（nooshare_dev）/ uv 管理 / typer CLI / pytest。

---

## 5. 外部客观验证标准（不依赖自研代码的判定基准）

| # | 验证项 | 客观标准来源 | 判定方法与阈值 |
|---|--------|------------|--------------|
| V1 | 日线价格正确性 | SSE/SZSE 每日行情公报 | 逐股票逐日比对 OHLCV，偏差 = 0 |
| V2 | 复权价格正确性 | 基于 `tong.gbbq` 原始事件手算复权因子（抽样 10 只明确除权除息股票） | 与 tong hfq/qfq 比对，误差 < 1e-4 |
| V3 | 交易日历完整性 | 交易所官方休市安排 | `ju.trading_day` 比对，缺失 = 0 |
| V4 | 股票列表完整性 | 交易所官网上市公司列表（含已退市） | 计数差异 = 0 |
| V5 | 财报数字准确性 | 巨潮年报/半年报原文 PDF 关键数字（营收/净利/EPS） | 与结构化数据逐字段比对 |
| V6 | ST 标记正确性 | 交易所 ST/*ST 公告 | 逐期间比对 `ju.st_period` 投影 |
| V7 | 指标计算正确性 | TA-Lib C 库（业界标准实现） | 逐值比对，max abs error < 1e-6 |
| V8 | 截面完整性 | 每交易日非停牌股票数 = ju.stocks 当日活跃数 | 缺失率 < 0.1% |
| V9 | 对数收益可加性 | 恒等式 Σ log_return[t:t+n] = log(close[t+n]/close[t]) | 误差 < 1e-10 |
| V10 | 零前视偏差 | T 日查询结果集与 pub_date > T 的数据求交集 | 交集 = 空 |
| V11 | 管线幂等性 | 同一输入快照重跑 | 产物哈希一致 |
| V12 | 标识唯一性 | canonical_symbol 无歧义映射 | tong↔ju 映射差异表 = 空（或全部人工裁决归档） |

---

## 6. 重写后的需求定义

### 6.1 核心职责（P1–P7）

**P1 规范符号主数据 `public.canonical_symbol`**
- 全矩阵唯一证券标识 hub，统一 `tong.symbol`（带前缀）与 `ju.stocks.stock_code`（纯代码）
- 记录完整生命周期：上市、更名、ST/摘帽、退市、代码变更
- 所有 `public.*` 表通过 `canonical_id` 关联；映射冲突进差异台账人工裁决
- 验收：V4、V12

**P2 公司行为事件溯源 `public.corporate_action_event`**
- 统一建模：`tong.gbbq`（除权除息/股本变更）、`ju.st_period`（ST 期间端点化）、`ju.valuation`（市值区间端点化）→ 不可变事件流
- ST 状态、复权因子、股本结构 = 事件流的确定性投影
- 验收：V2、V6（投影结果与源区间快照 100% 一致）

**P3 双时态对齐 `public.*_pit` 视图族**
- 财报 PIT：`pub_date ≤ 查询日` 才可见（消费 `ju.pub_date` ~25 万行存量）
- ST PIT：`effective_from ≤ 查询日 AND (effective_to IS NULL OR effective_to > 查询日)`
- 复权 PIT：仅使用 `≤ 查询日` 的 gbbq 事件
- 验收：V10

**P4 跨源对账 `public.reconcile_status`**
- 自动交叉验证（§2.4 清单）；差异记录不修正，输出对账报告
- 验收：每次管线运行产出报告；V1/V3/V5 抽样比对通过

**P5 数据新鲜度看板 `public.data_freshness`**
- 每张源表 `max_date / row_count / refreshed_at / status`
- 下游分析前强制检查依赖源 `status = fresh`
- 验收：看板数据与源表实际状态一致（抽样核查）

**P6 数据契约发布**
- 完整性 SLO：交易日 T 的日线，T+1 09:00 前 100% 就绪
- 准确性 SLO：复权价格与源偏差 < 1e-6
- 时效性 SLO：财报在 pub_date 后 24h 内入库
- 一致性：`public.*` 列结构/类型/语义冻结，变更走版本化契约
- 验收：契约测试套件（对 public.* 的 schema 快照断言）

**P7 幂等可复现管线 `public.pipeline_run`**
- 同一 (输入快照, 代码 commit) → 确定性产物；记录输入哈希/commit/行数/耗时
- 支持任意日期范围增量重算
- 验收：V11

### 6.2 下沉职责（从 indicators 收编）

以下能力从 `indicators.data_loader` 下沉为 `public.*` 公共产出（indicators 未来改为消费 public，删除重复实现）：

- 交易日历对齐（停牌日 ffill + volume=0）
- pre_close / simple_return / log_return 计算（含 V9 可加性自检）
- ST 标记（is_st）、涨跌停标记（is_limit_up/is_limit_down/is_censored，按 ju.limit_ratio 实际比例）

### 6.3 Out of Scope

- ❌ 不采集原始数据（各源仓库职责）
- ❌ 不计算技术指标（indicators 职责）
- ❌ 不存储/转换财报原文（annual-semi 职责）
- ❌ 不做分析/回测（下游消费者职责）
- ❌ 不静默修正任何源数据差异（对账只记录，人工裁决）

### 6.4 总验收门禁

交付前必须全部满足：

1. §5 的 V1–V12 全部通过（或差异已进台账并经人工裁决归档）
2. `public.*` 全部表有契约文件 + 契约测试
3. 幂等重跑验证通过（V11）
4. PIT 抽查：任选 3 个财报披露日，验证披露日前查询不可见该财报（V10）
5. 所有交付物落盘 `deliverables/`，代码落实际目录

---

## 7. 后续阶段建议（进入 SOP Phase 2/3 的输入）

1. **产品设计**（Phase 2）：`public.*` 表清单与字段规格、PIT 视图定义、对账报告格式、新鲜度看板查询接口
2. **技术方案**（Phase 3）：完整 DDL（含索引/分区策略）、事件投影 SQL、契约文件格式（YAML or Python dataclass）、pipeline_run 落库协议 —— **接口与数据模型在此冻结**
3. **功能拆分**（Phase 4）：Batch 0（db 连接/契约框架/公共工具）→ Batch 1（canonical_symbol）→ Batch 2（corporate_action_event + 投影）→ Batch 3（PIT 视图族 + 对账 + 新鲜度）→ Batch 4（pipeline_run + 下沉收编）
