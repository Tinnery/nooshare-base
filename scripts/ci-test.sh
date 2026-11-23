#!/bin/bash
# CI 本地测试脚本
# 模拟 GitHub Actions 的 integration-pipeline 完整流程。
#
# 用法：在隔离的 PostgreSQL 上运行（不要指向生产库）。
#   export PG_HOST=localhost PG_PORT=5432 PG_USER=postgres \
#          PG_PASSWORD=... PG_DATABASE=nooshare_ci PG_SSLMODE=prefer
#   bash scripts/ci-test.sh

set -e  # 遇到错误立即退出

echo "=========================================="
echo "Data Pipeline CI - Local Test"
echo "=========================================="
echo ""

# 检查 uv 是否安装
if ! command -v uv &> /dev/null; then
    echo "❌ uv 未安装，请先安装: pip install uv"
    exit 1
fi

# 检查数据库连接（应用读取 PG_* 分量，不读取 PG_DSN）
echo "🔍 检查数据库连接..."
if ! uv run python -c "from base.db import get_connection; get_connection()" 2>/dev/null; then
    echo "❌ 无法连接到数据库，请检查 PG_HOST/PG_PORT/PG_USER/PG_PASSWORD/PG_DATABASE 环境变量"
    exit 1
fi
echo "✅ 数据库连接正常"
echo ""

# Step 1: 装载合成 fixture（仅在隔离库运行！）
echo "📦 Step 1/5: 装载合成 CI fixture (ju/tong/finance)..."
uv run python scripts/ci_fixture.py
echo "✅ 合成 fixture 就绪"
echo ""

# Step 2: 初始化数据库
echo "🏗️  Step 2/5: 初始化数据库 schema..."
uv run base init-db
echo "✅ 数据库初始化完成"
echo ""

# Step 3: 运行数据管线（两次，为 V11 幂等门禁提供比对对）
echo "🔁 Step 3/5: 运行数据管线 (base build) 第 1 次..."
uv run base build
echo "🔁 Step 3/5: 运行数据管线 (base build) 第 2 次（幂等对）..."
uv run base build
echo "✅ 数据管线运行完成"
echo ""

# Step 4: 运行验证套件
echo "🧪 Step 4/5: 运行验证套件 (base validate)..."
uv run base validate
echo "✅ 验证套件通过"
echo ""

# Step 5: 数据新鲜度报告（非阻断）
echo "📊 Step 5/5: 数据新鲜度报告..."
uv run python -c "
from base.db import read_sql
freshness = read_sql('SELECT * FROM public.data_freshness ORDER BY status DESC, namespace')
print(freshness.to_string())
missing = freshness[freshness['status'] == 'missing']
if not missing.empty:
    print(f'\n⚠️  {len(missing)} 张表状态为 missing（合成 fixture 未覆盖，属预期）')
"
echo ""

# 显示最近管线运行
echo "=========================================="
echo "📋 最近 5 次管线运行记录"
echo "=========================================="
uv run python -c "
from base.db import read_sql
runs = read_sql('''
    SELECT run_id, status, started_at, completed_at,
           products->>'canonical_symbol' as canonical_symbol_count,
           products->>'corporate_action_event' as event_count
    FROM public.pipeline_run
    ORDER BY started_at DESC
    LIMIT 5
''')
print(runs.to_string())
"
echo ""

echo "=========================================="
echo "✅ CI 本地测试全部通过！"
echo "=========================================="
