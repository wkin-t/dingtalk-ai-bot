# -*- coding: utf-8 -*-
"""一次性回填 conversation_history 中 NULL bot_id 为当前 BOT_ID。

仅处理最近 30 天 assistant 消息，避免破坏更早的混合历史。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="回填 conversation_history 中最近 N 天 assistant 消息的 NULL bot_id。"
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计不修改")
    parser.add_argument("--days", type=int, default=30, help="只处理最近多少天的数据")
    parser.add_argument("--limit", type=int, default=10000, help="单次最多更新记录数")
    args = parser.parse_args()

    try:
        import pymysql
    except ImportError:
        print("pymysql 未安装，无法连接 MySQL")
        return 1

    from app.config import BOT_ID
    from app.database import MYSQL_DATABASE, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER

    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
    )

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            count_sql = """
                SELECT COUNT(*) AS c FROM conversation_history
                WHERE role = 'assistant' AND bot_id IS NULL
                  AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """
            cur.execute(count_sql, (args.days,))
            total = cur.fetchone()["c"]
            print(f"待回填: {total} 条 assistant 消息（{args.days} 天内 bot_id 为 NULL）")

            if args.dry_run:
                print("dry-run 模式，不做修改")
                return 0

            if total == 0:
                print("无需回填")
                return 0

            update_sql = """
                UPDATE conversation_history
                SET bot_id = %s
                WHERE role = 'assistant' AND bot_id IS NULL
                  AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                LIMIT %s
            """
            cur.execute(update_sql, (BOT_ID, args.days, args.limit))
            affected = cur.rowcount
            conn.commit()
            print(f"已回填 {affected} 条记录（bot_id={BOT_ID}）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
