# nooshare-base CLI API 文档
*自动生成于 2026-11-23 15:26:40*
---

## 概述

nooshare-base 提供统一的 CLI 入口，所有数据操作通过命令行完成。

```bash
base --help
```

---

## 核心命令

### init-db

**描述**: 初始化 public schema 和表结构

**用法**:
```bash
base init-db
```

**详情**:

创建 public.canonical_symbol、public.corporate_action_event 等基础表，以及 6 个物化 PIT 视图。

---

### build

**描述**: 运行完整管线（P1-P7）

**用法**:
```bash
base build [--start-date YYYYMMDD]
```

**详情**:

执行完整的数据管线：
- P1: 构建 canonical_symbol（符号主数据）
- P2: 事件溯源（公司行为事件提取）
- P3: 刷新物化 PIT 视图
- P4: 跨源对账
- P5: 数据新鲜度监控
- P7: 记录 pipeline_run

支持增量重算：`--start-date 20200101` 只重算指定日期之后的数据。

---

### reconcile

**描述**: 只运行跨源对账（P4）

**用法**:
```bash
base reconcile
```

**详情**:

对比 ju.stocks 与 tong.symbol_meta，检查交易日历完整性，检测结果写入 public.reconcile_status。

---

### freshness

**描述**: 只刷新数据新鲜度看板（P5）

**用法**:
```bash
base freshness
```

**详情**:

扫描所有 namespace 和 public.* 表，更新 public.data_freshness 表的 max_date、row_count、refreshed_at。

---

### validate

**描述**: 运行验证门禁（V9-V12 必跑，V1-V8 按数据源执行）

**用法**:
```bash
base validate
```

**详情**:

验证门禁包括：
- V9: 内部自检（PIT 视图行数 > 0）
- V10: 物化视图完整性
- V11: 幂等性验证（相同输入产出相同）
- V12: canonical_id 映射无歧义

---

### monitor

**描述**: 监控数据新鲜度，超过阈值时发送告警

**用法**:
```bash
base monitor [--max-age DAYS] [--alert-type log|email|webhook]
```

**详情**:

检查 public.data_freshness 表，如果 refreshed_at 超过 max-age 天，触发告警。

---

## 深度演进命令

### backtest

**描述**: 前视偏差回测验证（金融数据质量黄金标准）

**用法**:
```bash
base backtest [--sample-size N] [--seed SEED] [--output PATH]
```

**详情**:

随机抽取 N 个历史日期，对比 PIT 视图查询结果与 namespace 源头数据。

**验收标准**: 前视偏差率 = 0%

**输出**: JSON 格式回测报告，包含每个日期的比对结果。

---

### contract-test

**描述**: 契约测试自动化：验证数据是否符合 YAML 契约

**用法**:
```bash
base contract-test [--table TABLE] [--sample-size N] [--output PATH]
```

**详情**:

从 YAML 契约生成 JSON Schema，自动验证数据是否符合契约（类型/非空/范围）。

**验收标准**: 契约测试覆盖率 = 100%

**示例**:
```bash
# 验证所有 public.* 表
base contract-test

# 只验证单个表
base contract-test --table public.st_status_pit
```

---

### benchmark

**描述**: 查询性能基准测试：测量 P50/P95/P99 延迟

**用法**:
```bash
base benchmark [--iterations N] [--output PATH]
```

**详情**:

定义 10 个典型查询，实测每个查询的 P50/P95/P99 延迟，并生成优化建议报告。

**验收标准**: 单股票1年日线 P95 < 100ms

**输出**:
- `benchmarks/report.json`: 详细基准测试结果
- `benchmarks/OPTIMIZATION_REPORT.md`: 优化建议报告

---

### lineage

**描述**: 数据血缘追踪：追溯数据来源

**用法**:
```bash
base lineage {build|trace|stats} [--table TABLE] [--row-id ID]
```

**详情**:

追踪 public.* 数据的来源，支持正向/反向/全链路追踪。

**操作**:
- `build`: 构建血缘关系（写入 public.data_lineage）
- `trace`: 追溯指定行的来源
- `stats`: 查看血缘统计信息

**验收标准**: 血缘覆盖率 = 100%

**示例**:
```bash
# 构建血缘关系
base lineage build

# 追溯某行数据的来源
base lineage trace --table public.valuation_pit --row-id "sz000001:2020-01-01"

# 查看统计信息
base lineage stats
```

---

### sla

**描述**: 数据质量 SLA 监控：检查数据是否按时就绪

**用法**:
```bash
base sla {check|report|list|define} [OPTIONS]
```

**详情**:

定义每个表的 SLA（如"估值数据每天 9:30 前就绪"），违约时自动告警。

**操作**:
- `check`: 检查当前 SLA 违约情况
- `report`: 生成 SLA 达成率报告
- `list`: 列出所有已定义的 SLA
- `define`: 定义新的 SLA

**验收标准**: SLA 达成率 > 99%

**示例**:
```bash
# 检查 SLA 违约
base sla check

# 生成过去 30 天的达成率报告
base sla report --days 30

# 定义新的 SLA
base sla define --table ju.valuation --time 09:30
```

---

