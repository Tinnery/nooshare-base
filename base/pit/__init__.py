"""P3: 双时态（point-in-time）对齐。

职责：维护 public.*_pit 视图族（DDL 在 migrations/002），
并提供 as-of 查询辅助函数。核心铁律：T 日分析只能看到 pub_date <= T 的数据。
"""
