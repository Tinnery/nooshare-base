# nooshare-base 数据字典
*自动生成于 2026-11-23 15:26:40*
---
## 目录
### public.* 表（标准化产品层）
- [public.adjustment_factor_pit](#public-adjustment_factor_pit) — 复权因子物化 PIT 视图（P3）
- [public.canonical_symbol](#public-canonical_symbol) — 全矩阵唯一证券标识 hub（P1）
- [public.corporate_action_event](#public-corporate_action_event) — 公司行为事件溯源表（P2）
- [public.data_freshness](#public-data_freshness) — 数据新鲜度监控表（P5）
- [public.hfq_daily](#public-hfq_daily) — 后复权价格序列（P3 扩展），由 tong.gbbq category=1 事件累乘因子生成
- [public.pipeline_run](#public-pipeline_run) — 幂等管线运行记录表（P7）
- [public.reconcile_status](#public-reconcile_status) — 跨源对账状态表（P4）
- [public.st_status_pit](#public-st_status_pit) — ST 状态物化 PIT 视图（P3）
- [public.valuation_pit](#public-valuation_pit) — 估值数据物化 PIT 视图（P3）

---
## public.* 表详细定义
### public.adjustment_factor_pit
**版本**: 1  
**描述**: 复权因子物化 PIT 视图（P3）

**类型**: 物化视图（Materialized View）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `canonical_id` | TEXT | ✗ | 关联 canonical_symbol.canonical_id |
| `symbol` | TEXT | ✗ | 带交易所前缀（如 sz000001） |
| `effective_date` | DATE | ✗ | 除权除息日 |
| `event_type` | TEXT | ✗ | dividend（分红）/ split（拆股） |
| `payload` | JSONB | ✓ | 复权详情：{per_10shares, bonus_ratio} 或 {ratio} |
| `observed_at` | TIMESTAMPTZ | ✗ | 观测时间 |

#### 索引

- idx_adjustment_factor_pit_canonical ON (canonical_id, effective_date)
- idx_adjustment_factor_pit_symbol ON (symbol, effective_date)

#### PIT 查询语义

```sql
物化视图暴露全部历史事件，下游需自行做 as-of 过滤：
WHERE effective_date <= :as_of
累计复权因子需由下游按时间顺序累乘计算。
```

#### 数据来源

- `public.corporate_action_event`

#### 下游消费者

- `downstream indicators module`
- `pit/views.py query_adjustment_factors()`

---

### public.canonical_symbol
**版本**: 2  
**描述**: 全矩阵唯一证券标识 hub（P1）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `canonical_id` | TEXT | ✗ | 全矩阵唯一标识（纯代码，如 000001） |
| `exchange` | TEXT | ✗ | 交易所：sh / sz / bj（ju.stocks.exchange 小写值） |
| `code` | TEXT | ✗ | 纯数字代码（ju 风格） |
| `symbol` | TEXT | ✗ | 带交易所前缀（tong 风格，如 sz000001） |
| `asset_class` | TEXT | ✓ | 证券分类（tong.symbol_meta.class） |
| `list_date` | DATE | ✓ | 上市日 |
| `delist_date` | DATE | ✓ | 退市日（在市为 NULL） |
| `created_at` | TIMESTAMPTZ | ✗ | 创建时间（DEFAULT now()） |
| `updated_at` | TIMESTAMPTZ | ✗ | 更新时间（DEFAULT now()） |

#### 约束

- PRIMARY KEY (canonical_id)
- UNIQUE (exchange, code) — 通过 canonical_id = code 保证
- canonical_id ↔ (tong.symbol_meta, ju.stocks) 映射无歧义（V12）

#### 索引

- idx_canonical_symbol_symbol ON (symbol)

#### 数据来源

- `ju.stocks`
- `tong.symbol_meta`

#### 下游消费者

- `downstream indicators module`
- `all public.* views`

---

### public.corporate_action_event
**版本**: 1  
**描述**: 公司行为事件溯源表（P2）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `event_id` | TEXT | ✗ | 事件唯一标识（UUID） |
| `canonical_id` | TEXT | ✗ | 关联 canonical_symbol.canonical_id |
| `event_type` | TEXT | ✗ | 事件类型：st_mark / st_remove / dividend / split / shares_change |
| `effective_date` | DATE | ✗ | 生效日期（除权除息日、ST 变更日等） |
| `pub_date` | DATE | ✓ | 公告日期（当前源数据缺失，全部为 NULL） |
| `observed_at` | TIMESTAMPTZ | ✗ | 观测时间（DEFAULT now()） |
| `payload` | JSONB | ✓ | 事件详情（JSON，结构因 event_type 而异） |
| `source_id` | TEXT | ✓ | 来源标识 |
| `created_at` | TIMESTAMPTZ | ✗ | 创建时间（DEFAULT now()） |

#### 约束

- PRIMARY KEY (event_id)
- FOREIGN KEY (canonical_id) REFERENCES canonical_symbol(canonical_id)
- UNIQUE (canonical_id, event_type, effective_date, source_id) — 幂等键

#### 索引

- idx_corporate_action_event_canonical ON (canonical_id, effective_date)

#### Payload Schema

```json
st_mark: {st_type: 'ST' | '*ST' | 'S*ST'}
st_remove: {st_type: 'ST' | '*ST' | 'S*ST'}
dividend: {per_10shares: float, bonus_ratio: float}
split: {ratio: float}
shares_change: {total_shares: int, circulating_shares: int}
```

#### 数据来源

- `ju.st_period`
- `tong.gbbq`

#### 下游消费者

- `public.st_status_pit`
- `public.adjustment_factor_pit`
- `downstream indicators module`

---

### public.data_freshness
**版本**: 1  
**描述**: 数据新鲜度监控表（P5）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `namespace` | TEXT | ✗ | 命名空间（如 ju / tong / finance / public） |
| `table_name` | TEXT | ✗ | 表名 |
| `max_date` | DATE | ✓ | 表中最大日期（无日期列为 NULL） |
| `row_count` | BIGINT | ✓ | 行数 |
| `refreshed_at` | TIMESTAMPTZ | ✗ | 刷新时间（DEFAULT now()） |
| `status` | TEXT | ✓ | fresh / stale / missing（按表配置阈值判断） |

#### 约束

- PRIMARY KEY (namespace, table_name)

#### 数据来源

- `ju.*`
- `tong.*`
- `finance.*`
- `public.*`

#### 下游消费者

- `pipeline runner`
- `data quality monitoring`
- `alerting`

---

### public.hfq_daily
**版本**: 1  
**描述**: 后复权价格序列（P3 扩展），由 tong.gbbq category=1 事件累乘因子生成

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `symbol` | TEXT | ✗ | 带交易所前缀（sz000001），与 tong.stock_daily 对齐 |
| `date` | INTEGER | ✗ | 交易日 YYYYMMDD |
| `open` | DOUBLE PRECISION | ✓ | 原始开价 |
| `high` | DOUBLE PRECISION | ✓ | 原始高价 |
| `low` | DOUBLE PRECISION | ✓ | 原始低价 |
| `close` | DOUBLE PRECISION | ✓ | 原始收盘价 |
| `open_hfq` | DOUBLE PRECISION | ✓ | 后复权开价 = open * factor_cum |
| `high_hfq` | DOUBLE PRECISION | ✓ | 后复权高价 |
| `low_hfq` | DOUBLE PRECISION | ✓ | 后复权低价 |
| `close_hfq` | DOUBLE PRECISION | ✓ | 后复权收盘价 |
| `factor_cum` | DOUBLE PRECISION | ✗ | 累计复权因子（上市起累乘，上市日=1） |
| `hfq_return` | DOUBLE PRECISION | ✓ | hfq 日收益率 = close_hfq/close_hfq[-1] - 1（复权口径，含除权日修正） |
| `created_at` | TIMESTAMPTZ | ✗ | 写入时间 |

#### 索引

- idx_hfq_daily_date ON (date)

#### 下游消费者

- `downstream strategy module`

---

### public.pipeline_run
**版本**: 1  
**描述**: 幂等管线运行记录表（P7）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `run_id` | TEXT | ✗ | 运行唯一标识（UUID） |
| `started_at` | TIMESTAMPTZ | ✗ | 运行开始时间 |
| `completed_at` | TIMESTAMPTZ | ✓ | 运行完成时间 |
| `code_commit` | TEXT | ✓ | 代码版本（git commit hash） |
| `input_hash` | TEXT | ✓ | 输入快照哈希（源表行数 + max_date） |
| `output_hash` | TEXT | ✓ | 输出快照哈希（当前未使用） |
| `status` | TEXT | ✓ | running / ok / failed |
| `products` | JSONB | ✓ | 产出摘要：{canonical_symbol: N, corporate_action_event: N, ...} |

#### 约束

- PRIMARY KEY (run_id)

#### 索引

- idx_pipeline_run_started ON (started_at DESC)

#### 数据来源

- `ju.stocks`
- `ju.st_period`
- `ju.valuation`
- `tong.symbol_meta`
- `tong.gbbq`

#### 下游消费者

- `pipeline runner`
- `validate/suite.py V11`

---

### public.reconcile_status
**版本**: 1  
**描述**: 跨源对账状态表（P4）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `namespace` | TEXT | ✗ | 命名空间（如 ju / tong / finance） |
| `table_name` | TEXT | ✗ | 表名 |
| `check_type` | TEXT | ✗ | 检查类型：stocks_vs_symbol_meta / calendar_completeness / event_anomalies / symbol_coverage |
| `check_date` | DATE | ✗ | 检查日期 |
| `status` | TEXT | ✗ | pass / warn / fail |
| `details` | JSONB | ✓ | 检查详情（JSON） |
| `checked_at` | TIMESTAMPTZ | ✗ | 检查时间（DEFAULT now()） |

#### 约束

- PRIMARY KEY (namespace, table_name, check_type, check_date)

#### 数据来源

- `ju.stocks`
- `tong.symbol_meta`
- `public.corporate_action_event`

#### 下游消费者

- `pipeline runner`
- `data quality monitoring`

---

### public.st_status_pit
**版本**: 1  
**描述**: ST 状态物化 PIT 视图（P3）

**类型**: 物化视图（Materialized View）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `canonical_id` | TEXT | ✗ | 关联 canonical_symbol.canonical_id |
| `symbol` | TEXT | ✗ | 带交易所前缀（如 sz000001） |
| `effective_from` | DATE | ✗ | ST 状态开始日期 |
| `effective_to` | DATE | ✓ | ST 状态结束日期（NULL 表示当前仍为 ST） |
| `pub_date` | DATE | ✓ | 公告日期（当前为 NULL） |
| `st_type` | TEXT | ✓ | ST 类型：ST / *ST / S*ST |
| `observed_at` | TIMESTAMPTZ | ✗ | 观测时间 |

#### 索引

- idx_st_status_pit_canonical ON (canonical_id, effective_from)
- idx_st_status_pit_symbol ON (symbol, effective_from)

#### PIT 查询语义

```sql
物化视图暴露全部历史区间，下游需自行做 as-of 过滤：
WHERE effective_from <= :as_of AND (effective_to IS NULL OR effective_to > :as_of)
```

#### 数据来源

- `public.corporate_action_event`

#### 下游消费者

- `downstream indicators module`
- `pit/views.py query_st_status()`

---

### public.valuation_pit
**版本**: 1  
**描述**: 估值数据物化 PIT 视图（P3）

**类型**: 物化视图（Materialized View）

#### 列定义

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `canonical_id` | TEXT | ✗ | 关联 canonical_symbol.canonical_id |
| `symbol` | TEXT | ✗ | 带交易所前缀（如 sz000001） |
| `effective_from` | DATE | ✗ | 估值区间开始日期 |
| `effective_to` | DATE | ✗ | 估值区间结束日期 |
| `capitalization` | NUMERIC | ✓ | 总市值（万元） |
| `circulating_cap` | NUMERIC | ✓ | 流通市值（万元） |
| `observed_at` | TIMESTAMPTZ | ✗ | 观测时间（ju.valuation.refreshed_at） |

#### 索引

- idx_valuation_pit_canonical ON (canonical_id, effective_from)
- idx_valuation_pit_symbol ON (symbol, effective_from)

#### PIT 查询语义

```sql
物化视图暴露全部历史区间，下游需自行做 as-of 过滤：
WHERE effective_from <= :as_of AND effective_to >= :as_of
```

#### 数据来源

- `ju.valuation`

#### 下游消费者

- `downstream indicators module`

---

