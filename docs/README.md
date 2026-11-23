# nooshare-base 文档
*自动生成于 2026-11-23 15:26:40*
---

## 文档列表

1. **[数据字典](DATA_DICTIONARY.md)** — 所有表的列定义、约束、索引、数据来源
2. **[API 文档](API_REFERENCE.md)** — CLI 命令参考，包括核心命令和深度演进命令
3. **[使用示例](USAGE_EXAMPLES.md)** — 10 个常见查询场景 + 高级用法

---

## 快速链接

- [public.canonical_symbol](DATA_DICTIONARY.md#public-canonical_symbol) — 符号主数据
- [public.st_status_pit](DATA_DICTIONARY.md#public-st_status_pit) — ST 状态 PIT 视图
- [public.valuation_pit](DATA_DICTIONARY.md#public-valuation_pit) — 估值数据 PIT 视图
- [CLI build 命令](API_REFERENCE.md#build) — 运行完整管线
- [查询示例](USAGE_EXAMPLES.md#场景-1-查询某股票的-st-历史) — ST 历史查询

---

## 核心概念

- **PIT (Point-in-Time)**: 时间点查询，避免前视偏差
- **双时态模型**: effective_date（生效日期）+ pub_date（公告日期）+ observed_at（观测时间）
- **Event Sourcing**: 公司行为事件溯源，保留完整变更历史
- **Canonical Symbol**: 统一证券标识，消除跨源符号歧义

