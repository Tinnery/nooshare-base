"""测试共享夹具。

集成测试需要可达的 nooshare_dev 数据库（.env 配置）；
单元测试使用合成数据，不依赖数据库。
"""
import os

import pytest

PG_AVAILABLE = bool(os.environ.get("PG_PASSWORD") or os.environ.get("PG_HOST"))


requires_db = pytest.mark.skipif(
    not PG_AVAILABLE, reason="需要数据库连接（.env 未配置）"
)
