"""P5: 数据新鲜度看板。

职责：扫描全部源 namespace 表 → public.data_freshness（max_date/row_count/refreshed_at/status）。
下游分析前必须检查依赖源 status = fresh。
"""
