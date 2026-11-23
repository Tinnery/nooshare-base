"""
T2.3 财报版本化迁移脚本

将 finance.* 表的数据迁移到 public.finance_statement_pit，支持 PIT 查询。
"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from base.db import get_db_connection


def migrate_finance_statements():
    """迁移财报数据到 PIT 表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 1. 创建 PIT 表
        print("创建 public.finance_statement_pit 表...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS public.finance_statement_pit (
                pit_id SERIAL PRIMARY KEY,
                code TEXT NOT NULL,
                report_date DATE NOT NULL,
                disclosed_at DATE NOT NULL,
                observed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                statement_type TEXT NOT NULL,
                payload_hash TEXT,
                source_batch TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (code, report_date, disclosed_at, statement_type)
            )
        """)
        
        # 2. 创建索引
        print("创建索引...")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_finance_pit_lookup 
            ON public.finance_statement_pit (code, report_date, disclosed_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_finance_pit_type 
            ON public.finance_statement_pit (statement_type)
        """)
        
        # 3. 迁移 income_statement
        print("迁移 income_statement...")
        cursor.execute("""
            INSERT INTO public.finance_statement_pit 
            (code, report_date, disclosed_at, statement_type, source_batch)
            SELECT 
                code,
                report_date,
                pub_date,
                'income',
                'finance.income_statement_v1'
            FROM finance.income_statement
            WHERE pub_date IS NOT NULL
            ON CONFLICT (code, report_date, disclosed_at, statement_type) 
            DO NOTHING
        """)
        income_count = cursor.rowcount
        print(f"  迁移 {income_count} 行")
        
        # 4. 迁移 balance_sheet
        print("迁移 balance_sheet...")
        cursor.execute("""
            INSERT INTO public.finance_statement_pit 
            (code, report_date, disclosed_at, statement_type, source_batch)
            SELECT 
                code,
                report_date,
                pub_date,
                'balance',
                'finance.balance_sheet_v1'
            FROM finance.balance_sheet
            WHERE pub_date IS NOT NULL
            ON CONFLICT (code, report_date, disclosed_at, statement_type) 
            DO NOTHING
        """)
        balance_count = cursor.rowcount
        print(f"  迁移 {balance_count} 行")
        
        # 5. 迁移 cash_flow
        print("迁移 cash_flow...")
        cursor.execute("""
            INSERT INTO public.finance_statement_pit 
            (code, report_date, disclosed_at, statement_type, source_batch)
            SELECT 
                code,
                report_date,
                pub_date,
                'cash_flow',
                'finance.cash_flow_v1'
            FROM finance.cash_flow
            WHERE pub_date IS NOT NULL
            ON CONFLICT (code, report_date, disclosed_at, statement_type) 
            DO NOTHING
        """)
        cashflow_count = cursor.rowcount
        print(f"  迁移 {cashflow_count} 行")
        
        conn.commit()
        print(f"\n迁移完成！")
        print(f"  income: {income_count} 行")
        print(f"  balance: {balance_count} 行")
        print(f"  cash_flow: {cashflow_count} 行")
        print(f"  总计: {income_count + balance_count + cashflow_count} 行")
        
    except Exception as e:
        conn.rollback()
        print(f"迁移失败: {e}", file=sys.stderr)
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    migrate_finance_statements()
