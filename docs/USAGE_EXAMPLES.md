# nooshare-base 使用示例
*自动生成于 2026-11-23 15:26:40*
---

## 快速开始

### 1. 初始化数据库

```bash
# 创建 public schema 和所有表
base init-db
```

### 2. 运行完整管线

```bash
# 完整管线：P1-P7
base build
```

### 3. 验证数据质量

```bash
# 运行验证门禁
base validate
```

---

## 常见查询场景

### 场景 1: 查询某股票的 ST 历史

**描述**: 查询某只股票在指定日期的 ST 状态（Point-in-Time）

**SQL 查询**:

```sql
SELECT symbol, st_type, effective_from, effective_to
FROM public.st_status_pit
WHERE symbol = 'sz000001'
  AND effective_from <= '2020-01-01'
  AND (effective_to IS NULL OR effective_to > '2020-01-01');
```

---

### 场景 2: 查询全市场某日的估值数据

**描述**: 查询 2020-01-01 当天所有股票的市值（避免前视偏差）

**SQL 查询**:

```sql
SELECT symbol, capitalization, circulating_cap
FROM public.valuation_pit
WHERE effective_from <= '2020-01-01'
  AND effective_to >= '2020-01-01';
```

---

### 场景 3: 查询某股票的复权因子历史

**描述**: 查询某只股票的所有分红送股事件

**SQL 查询**:

```sql
SELECT effective_date, event_type, payload
FROM public.adjustment_factor_pit
WHERE symbol = 'sz000001'
  AND effective_date <= '2020-12-31'
ORDER BY effective_date;
```

---

### 场景 4: 查询某股票的财务报表（利润表）

**描述**: 查询某只股票的历史财务数据（按公告日期，避免前视偏差）

**SQL 查询**:

```sql
SELECT report_date, pub_date, revenue, net_profit, eps
FROM public.income_statement_pit
WHERE symbol = 'sz000001'
  AND pub_date <= '2020-12-31'
ORDER BY report_date DESC
LIMIT 10;
```

---

### 场景 5: 统计当前 ST 股票数量

**描述**: 查询当前（最新日期）所有 ST 股票

**SQL 查询**:

```sql
SELECT COUNT(DISTINCT symbol) AS st_count
FROM public.st_status_pit
WHERE effective_to IS NULL;
```

---

### 场景 6: 查询某日的全市场交易日线

**描述**: 查询 2020-01-02 所有股票的日线数据

**SQL 查询**:

```sql
SELECT symbol, open, high, low, close, volume, amount
FROM tong.stock_daily
WHERE trade_date = '2020-01-02';
```

---

### 场景 7: 查询某股票的股本变动历史

**描述**: 查询某只股票的总股本和流通股本变化

**SQL 查询**:

```sql
SELECT effective_date, payload->>'total_shares' AS total_shares,
                      payload->>'circulating_shares' AS circulating_shares
FROM public.corporate_action_event
WHERE canonical_id = '000001'
  AND event_type = 'shares_change'
ORDER BY effective_date;
```

---

### 场景 8: 检查数据新鲜度

**描述**: 查看所有表的最新数据日期

**SQL 查询**:

```sql
SELECT namespace, table_name, max_date, row_count, refreshed_at
FROM public.data_freshness
ORDER BY namespace, table_name;
```

---

### 场景 9: 查看管线运行历史

**描述**: 查看最近的管线运行记录

**SQL 查询**:

```sql
SELECT run_id, started_at, completed_at, status, products
FROM public.pipeline_run
ORDER BY started_at DESC
LIMIT 10;
```

---

### 场景 10: 检查跨源对账状态

**描述**: 查看最近的对账结果

**SQL 查询**:

```sql
SELECT namespace, table_name, check_type, status, checked_at
FROM public.reconcile_status
ORDER BY checked_at DESC;
```

---

## 高级用法

### 前视偏差回测

验证 PIT 视图是否零前视偏差（金融数据质量黄金标准）。

```bash
# 随机抽取 100 个历史日期进行回测
base backtest --sample-size 100 --output backtest_report.json
```

### 契约测试

验证数据是否符合 YAML 契约定义。

```bash
# 验证所有 public.* 表
base contract-test --output contract_test_report.json
```

### 性能基准测试

测量典型查询的 P50/P95/P99 延迟。

```bash
# 运行基准测试
base benchmark --iterations 100 --output benchmarks/report.json
```

### 数据血缘追踪

追溯 public.* 数据的来源。

```bash
# 构建血缘关系
base lineage build

# 追溯某行数据的来源
base lineage trace --table public.valuation_pit --row-id 'sz000001:2020-01-01'
```

### SLA 监控

定义和检查数据质量 SLA。

```bash
# 定义 SLA：估值数据每天 9:30 前就绪
base sla define --table ju.valuation --time 09:30

# 检查 SLA 违约
base sla check

# 生成达成率报告
base sla report --days 30
```

