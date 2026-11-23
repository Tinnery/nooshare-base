"""监控告警模块 - 基于 data_freshness 表配置 stale 告警"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from base.db import read_sql, execute_sql

log = logging.getLogger(__name__)


def check_stale_tables(
    max_age_days: int = 7,
    namespaces: Optional[List[str]] = None
) -> List[Dict]:
    """检查数据新鲜度，返回超过阈值的表列表
    
    Args:
        max_age_days: 最大允许的数据陈旧天数
        namespaces: 要检查的命名空间列表，None 表示检查所有
    
    Returns:
        陈旧表列表，每项包含 namespace, table_name, last_updated, age_days
    """
    log.info(f"检查数据新鲜度，阈值: {max_age_days} 天")
    
    # 查询 data_freshness 表
    sql = """
        SELECT 
            namespace,
            table_name,
            refreshed_at,
            EXTRACT(DAY FROM NOW() - refreshed_at) as age_days,
            row_count
        FROM public.data_freshness
        WHERE refreshed_at IS NOT NULL
    """
    
    if namespaces:
        ns_list = ", ".join(f"'{ns}'" for ns in namespaces)
        sql += f" AND namespace IN ({ns_list})"
    
    sql += " ORDER BY age_days DESC"
    
    df = read_sql(sql)
    
    # 筛选超过阈值的表
    stale_tables = []
    for _, row in df.iterrows():
        age_days = float(row['age_days'])
        if age_days > max_age_days:
            stale_tables.append({
                'namespace': row['namespace'],
                'table_name': row['table_name'],
                'last_updated': row['last_updated'],
                'age_days': age_days,
                'row_count': int(row['row_count']) if row['row_count'] else 0
            })
    
    if stale_tables:
        log.warning(f"发现 {len(stale_tables)} 张表数据陈旧")
        for table in stale_tables:
            log.warning(
                f"  {table['namespace']}.{table['table_name']}: "
                f"{table['age_days']:.1f} 天未更新"
            )
    else:
        log.info("所有表数据新鲜度正常")
    
    return stale_tables


def send_alert(
    stale_tables: List[Dict],
    alert_type: str = "log"
) -> bool:
    """发送告警
    
    Args:
        stale_tables: 陈旧表列表
        alert_type: 告警类型 (log/email/webhook)
    
    Returns:
        是否发送成功
    """
    if not stale_tables:
        return True
    
    # 构建告警消息
    message = f"⚠️ 数据陈旧告警\n\n"
    message += f"发现 {len(stale_tables)} 张表超过更新阈值:\n\n"
    
    for table in stale_tables:
        message += f"• {table['namespace']}.{table['table_name']}\n"
        message += f"  最后更新: {table['last_updated']}\n"
        message += f"  已过期: {table['age_days']:.1f} 天\n"
        message += f"  行数: {table['row_count']}\n\n"
    
    # 根据告警类型发送
    if alert_type == "log":
        log.warning(message)
        return True
    elif alert_type == "email":
        # TODO: 实现邮件发送
        log.info("邮件告警功能待实现")
        return False
    elif alert_type == "webhook":
        # TODO: 实现 webhook 发送
        log.info("Webhook 告警功能待实现")
        return False
    else:
        log.error(f"未知的告警类型: {alert_type}")
        return False


def run_monitor(
    max_age_days: int = 7,
    alert_type: str = "log",
    namespaces: Optional[List[str]] = None
) -> bool:
    """运行监控检查并发送告警
    
    Args:
        max_age_days: 最大允许的数据陈旧天数
        alert_type: 告警类型
        namespaces: 要检查的命名空间列表
    
    Returns:
        是否所有表都正常
    """
    stale_tables = check_stale_tables(max_age_days, namespaces)
    
    if stale_tables:
        send_alert(stale_tables, alert_type)
        return False
    else:
        log.info("✅ 监控检查通过，所有数据新鲜度正常")
        return True


if __name__ == "__main__":
    import sys
    
    # 从命令行参数读取配置
    max_age = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    alert_type = sys.argv[2] if len(sys.argv) > 2 else "log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    success = run_monitor(max_age_days=max_age, alert_type=alert_type)
    sys.exit(0 if success else 1)
