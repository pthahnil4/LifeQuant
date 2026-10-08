# -*- coding: utf-8 -*-
"""诊断脚本：排查 plan 表的锁等待来源（谁持有事务 / 连接状态 / 行数）

用法：python data/_diag_plan_lock.py
"""
import sys
import time

sys.path.insert(0, r'd:\python\cryptoTrade')

from sqlalchemy import create_engine, text

URL = open(r'd:\python\cryptoTrade\data\db_url.txt', encoding='utf-8').read().strip()


def main():
    eng = create_engine(URL, pool_pre_ping=True, connect_args={'connect_timeout': 20})
    t0 = time.time()
    with eng.connect() as c:
        print(f'connect {round(time.time() - t0, 1)}s  server={c.execute(text("SELECT VERSION()")).scalar()}')

        print('\n--- INNODB_TRX（打开的事务；无 PROCESS 权限时跳过） ---')
        try:
            rows = c.execute(text(
                'SELECT trx_mysql_thread_id AS tid, trx_state, trx_started, '
                'trx_rows_locked, trx_rows_modified, '
                "LEFT(IFNULL(trx_query, ''), 120) AS q "
                'FROM information_schema.INNODB_TRX ORDER BY trx_started'
            )).fetchall()
            if not rows:
                print('(无打开的事务)')
            for r in rows:
                print(r)
        except Exception as exc:
            print(f'(不可查: {type(exc).__name__} {getattr(exc, "orig", exc)})')

        print('\n--- 连接状态（SHOW PROCESSLIST；无权限时跳过） ---')
        try:
            rows = c.execute(text(
                'SELECT id, user, host, db, command, time, state, '
                "LEFT(IFNULL(info, ''), 100) AS q "
                'FROM information_schema.PROCESSLIST ORDER BY time DESC LIMIT 20'
            )).fetchall()
            for r in rows:
                print(r)
        except Exception as exc:
            print(f'(不可查: {type(exc).__name__})')
            # 退化方案：只查自己的连接数
            try:
                n = c.execute(text('SELECT COUNT(*) FROM information_schema.PROCESSLIST '
                                   'WHERE db IS NOT NULL')).scalar()
                print('  连接数（可见部分）=', n)
            except Exception:
                pass

        print('\n--- 行数 ---')
        for tb in ('plan_plans', 'plan_cards', 'plan_slots'):
            n = c.execute(text(f'SELECT COUNT(*) FROM {tb}')).scalar()
            extra = ''
            if tb == 'plan_cards':
                null_tasks = c.execute(text(
                    'SELECT COUNT(*) FROM plan_cards WHERE tasks IS NULL')).scalar()
                migrated = c.execute(text(
                    'SELECT COUNT(*) FROM plan_cards WHERE tasks IS NOT NULL')).scalar()
                empty_arr = c.execute(text(
                    "SELECT COUNT(*) FROM plan_cards WHERE tasks = '[]'")).scalar()
                extra = f' | tasks NULL={null_tasks} 已迁移={migrated} 空数组={empty_arr}'
            elif tb == 'plan_slots':
                with_links = c.execute(text(
                    'SELECT COUNT(*) FROM plan_slots WHERE task_links IS NOT NULL '
                    "AND task_links NOT IN ('[]', '')")).scalar()
                extra = f' | 带 task_links={with_links}'
            print(f'  {tb} = {n}{extra}')

        print('\n--- 卡片状态分布 ---')
        rows = c.execute(text(
            'SELECT status, COUNT(*) FROM plan_cards GROUP BY status')).fetchall()
        for r in rows:
            print(' ', r[0], r[1])


if __name__ == '__main__':
    main()
