#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/db.py  →  现位置: crypto/kline_training/store.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
SQLite 持久化层
================
用 stdlib ``sqlite3`` 实现单文件持久化（零额外服务，契合自托管小范围部署），
库文件路径取自根级 ``config.SQLITE_PATH``。

表结构
------
- ``users``   ：账号（口令哈希由鉴权层用 werkzeug 生成后写入）
- ``sessions``：训练会话/成绩（含操作流水、权益曲线，可归属账号，也可匿名）
- ``matches`` ：异步同题对战（对战码 + 确定性 seed + 生命周期状态）
- ``results`` ：对战成绩（每 user 每 match 一条，含排名）

并发：单连接 + ``threading.RLock`` 串行化访问（低流量自托管足够）；启用 WAL
提升读写并发、``foreign_keys`` 保证引用完整性。所有写操作走 ``_write`` 上下文，
自动提交/回滚。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import random
import sqlite3
import string
import sys
import threading
import time
from contextlib import contextmanager


from crypto.kline_training import settings as config

DB_PATH = config.SQLITE_PATH

_conn = None
_lock = threading.RLock()
_initialized = False


# ====================================================================
#  连接与初始化
# ====================================================================

def _now() -> int:
    return int(time.time())


def get_conn() -> sqlite3.Connection:
    """惰性创建并返回共享连接（线程安全，需配合 _lock 使用）"""
    global _conn
    if _conn is None:
        d = os.path.dirname(DB_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
        _conn.row_factory = sqlite3.Row
        _conn.execute('PRAGMA journal_mode=WAL')
        _conn.execute('PRAGMA foreign_keys=ON')
    return _conn


@contextmanager
def _write():
    """写操作上下文：串行化 + 自动提交/回滚"""
    with _lock:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def _read():
    """读操作上下文：串行化访问共享连接"""
    with _lock:
        yield get_conn()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    UNIQUE NOT NULL,
    phone         TEXT    UNIQUE,
    nickname      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_seen     INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

-- 邮箱验证码：每个 (email, purpose) 只保留一条最新记录（重发即覆盖）
CREATE TABLE IF NOT EXISTS email_verify_codes (
    email      TEXT    NOT NULL,
    code       TEXT    NOT NULL,
    purpose    TEXT    NOT NULL DEFAULT 'register',
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (email, purpose)
);

-- 用户备注：owner 给 target 起的私人称呼（仅影响 owner 自己视角）
CREATE TABLE IF NOT EXISTS user_notes (
    owner_id   INTEGER NOT NULL,
    target_id  INTEGER NOT NULL,
    note       TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, target_id),
    FOREIGN KEY(owner_id)  REFERENCES users(id),
    FOREIGN KEY(target_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    category          TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    period            TEXT    NOT NULL,
    mode              TEXT    NOT NULL DEFAULT 'free',
    seed              TEXT,
    start_ts          INTEGER,
    bar_count         INTEGER,
    final_balance     REAL,
    return_pct        REAL,
    win_rate          REAL,
    max_drawdown_pct  REAL,
    profit_loss_ratio REAL,
    total_trades      INTEGER,
    sharpe            REAL,
    stats_json        TEXT,
    trades_json       TEXT,
    equity_json       TEXT,
    created_at        INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS matches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT    UNIQUE NOT NULL,
    host_id            INTEGER,
    category           TEXT    NOT NULL,
    symbol             TEXT    NOT NULL,
    period             TEXT    NOT NULL,
    seed               TEXT    NOT NULL,
    start_ts           INTEGER,
    end_ts             INTEGER,
    bar_count          INTEGER,
    engine_config_json TEXT,
    blind              INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT 'open',
    pvp_mode           TEXT    NOT NULL DEFAULT 'code',
    auto_sync          INTEGER NOT NULL DEFAULT 0,
    target_sec         INTEGER,
    speed_ms           INTEGER,
    start_at           INTEGER,
    paused_at          INTEGER,
    pause_ms           INTEGER NOT NULL DEFAULT 0,
    end_at             INTEGER,
    created_at         INTEGER NOT NULL,
    FOREIGN KEY(host_id) REFERENCES users(id)
);

-- 自由组队成员与邀请状态：invited→accepted→ready（declined 为拒绝）
CREATE TABLE IF NOT EXISTS match_players (
    match_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    role     TEXT    NOT NULL DEFAULT 'player',
    state    TEXT    NOT NULL DEFAULT 'invited',
    ts       INTEGER NOT NULL,
    PRIMARY KEY (match_id, user_id),
    FOREIGN KEY(match_id) REFERENCES matches(id),
    FOREIGN KEY(user_id)  REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    session_id        INTEGER,
    return_pct        REAL,
    max_drawdown_pct  REAL,
    win_rate          REAL,
    profit_loss_ratio REAL,
    final_balance     REAL,
    total_trades      INTEGER,
    rank              INTEGER,
    submitted_at      INTEGER NOT NULL,
    UNIQUE(match_id, user_id),
    FOREIGN KEY(match_id) REFERENCES matches(id),
    FOREIGN KEY(user_id)  REFERENCES users(id),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS blind_sessions (
    id         TEXT    PRIMARY KEY,
    category   TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    name       TEXT,
    period     TEXT    NOT NULL,
    start_ts   INTEGER,
    end_ts     INTEGER,
    bar_count  INTEGER,
    seed       TEXT,
    revealed   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_results_match ON results(match_id, rank);
CREATE INDEX IF NOT EXISTS idx_players_match ON match_players(match_id, state);
CREATE INDEX IF NOT EXISTS idx_players_user ON match_players(user_id, state);
"""


def init_db():
    """建表（幂等）+ 轻量列迁移。应用启动时调用一次。"""
    global _initialized
    with _lock:
        conn = get_conn()
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
        _initialized = True
    return DB_PATH


def _table_cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table).fetchall()}


def _migrate(conn):
    """为已存在的旧表补齐新增列（CREATE IF NOT EXISTS 不会 ALTER 已存在表）。

    仅做 additive 增列（带默认值），安全幂等。"""
    add = []
    ucols = _table_cols(conn, 'users')
    if 'last_seen' not in ucols:
        add.append("ALTER TABLE users ADD COLUMN last_seen INTEGER NOT NULL DEFAULT 0")
    mcols = _table_cols(conn, 'matches')
    for name, ddl in (
        ('pvp_mode', "ALTER TABLE matches ADD COLUMN pvp_mode TEXT NOT NULL DEFAULT 'code'"),
        ('auto_sync', "ALTER TABLE matches ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 0"),
        ('target_sec', 'ALTER TABLE matches ADD COLUMN target_sec INTEGER'),
        ('speed_ms', 'ALTER TABLE matches ADD COLUMN speed_ms INTEGER'),
        ('start_at', 'ALTER TABLE matches ADD COLUMN start_at INTEGER'),
        ('paused_at', 'ALTER TABLE matches ADD COLUMN paused_at INTEGER'),
        ('pause_ms', "ALTER TABLE matches ADD COLUMN pause_ms INTEGER NOT NULL DEFAULT 0"),
        ('end_at', 'ALTER TABLE matches ADD COLUMN end_at INTEGER'),
    ):
        if name not in mcols:
            add.append(ddl)
    for sql in add:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass   # 并发/重复下已存在则忽略


def _ensure_init():
    if not _initialized:
        init_db()


def _row_to_dict(row):
    return dict(row) if row is not None else None


# ====================================================================
#  users
# ====================================================================

def create_user(email, password_hash, nickname, phone=None):
    """创建用户（邮箱 + 昵称 + 口令哈希，手机号可选），返回 user_id。

    邮箱/昵称/手机号重复抛 sqlite3.IntegrityError。
    """
    _ensure_init()
    with _write() as conn:
        cur = conn.execute(
            'INSERT INTO users(email, phone, nickname, password_hash, created_at)'
            ' VALUES(?,?,?,?,?)',
            ((email or '').strip().lower(), (phone or '').strip() or None,
             (nickname or '').strip(), password_hash, _now()))
        return cur.lastrowid


def get_user_by_email(email):
    _ensure_init()
    with _read() as conn:
        row = conn.execute('SELECT * FROM users WHERE email=?',
                           ((email or '').strip().lower(),)).fetchone()
    return _row_to_dict(row)


def get_user_by_login(identifier):
    """按「邮箱 / 手机号 / 昵称」任一登录标识查用户（邮箱、昵称大小写不敏感）。"""
    _ensure_init()
    ident = (identifier or '').strip()
    if not ident:
        return None
    with _read() as conn:
        row = conn.execute(
            'SELECT * FROM users WHERE LOWER(email)=LOWER(?) OR phone=? '
            'OR LOWER(nickname)=LOWER(?)',
            (ident, ident, ident)).fetchone()
    return _row_to_dict(row)


def update_password(user_id, password_hash):
    """用户改密码：更新口令哈希。返回是否更新到行。"""
    _ensure_init()
    with _write() as conn:
        cur = conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                           (password_hash, user_id))
        return cur.rowcount > 0


def _user_taken(conn, field, value, exclude_id):
    """检查某字段值是否已被其他用户占用（email/nickname 大小写不敏感）。"""
    col = {'email': 'LOWER(email)=LOWER(?)', 'nickname': 'LOWER(nickname)=LOWER(?)',
           'phone': 'phone=?'}[field]
    row = conn.execute(
        f'SELECT id FROM users WHERE {col} AND id<>?', (value, exclude_id)).fetchone()
    return bool(row)


def update_nickname(user_id, nickname):
    """改昵称（非空、最长 24、全局唯一）。冲突抛 ValueError。返回是否更新。"""
    _ensure_init()
    nickname = (nickname or '').strip()
    if not nickname:
        raise ValueError('昵称不能为空')
    if len(nickname) > 24:
        raise ValueError('昵称过长（最多 24 字）')
    with _write() as conn:
        if _user_taken(conn, 'nickname', nickname, user_id):
            raise ValueError('该昵称已被占用')
        cur = conn.execute('UPDATE users SET nickname=? WHERE id=?',
                           (nickname, user_id))
        return cur.rowcount > 0


def update_phone(user_id, phone):
    """改手机号（可为空；非空则全局唯一）。冲突抛 ValueError。返回是否更新。"""
    _ensure_init()
    phone = (phone or '').strip() or None
    with _write() as conn:
        if phone and _user_taken(conn, 'phone', phone, user_id):
            raise ValueError('该手机号已被占用')
        cur = conn.execute('UPDATE users SET phone=? WHERE id=?',
                           (phone, user_id))
        return cur.rowcount > 0


def reset_password_by_admin(admin_email, target_email_or_identifier, new_password):
    """管理员重置任意用户密码。

    以管理员邮箱白名单（config.ADMIN_EMAILS）校验身份；目标可用邮箱/手机号/
    昵称任一标识。成功返回目标用户 dict；失败抛 ValueError/PermissionError。"""
    _ensure_init()
    from crypto.kline_training import settings as config
    from werkzeug.security import generate_password_hash
    adm = (admin_email or '').strip().lower()
    if adm not in config.ADMIN_EMAILS:
        raise PermissionError('无管理员权限')
    new_password = (new_password or '').strip()
    if len(new_password) < 4:
        raise ValueError('新密码至少 4 位')
    target = get_user_by_login(target_email_or_identifier)
    if not target:
        raise ValueError('目标用户不存在')
    ok = update_password(target['id'], generate_password_hash(new_password))
    if not ok:
        raise ValueError('重置失败')
    return {'id': target['id'], 'email': target['email'],
            'nickname': target['nickname']}


def get_user_by_id(user_id):
    _ensure_init()
    with _read() as conn:
        row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    return _row_to_dict(row)


def list_users(limit=200):
    _ensure_init()
    with _read() as conn:
        rows = conn.execute(
            'SELECT id, email, phone, nickname, last_seen, created_at FROM users'
            ' ORDER BY id LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


def touch_user_last_seen(user_id):
    """心跳：刷新用户最后活跃时间（秒级 unix）。"""
    _ensure_init()
    with _write() as conn:
        conn.execute('UPDATE users SET last_seen=? WHERE id=?', (_now(), user_id))
    return True


# ====================================================================
#  email_verify_codes（邮箱验证码）
# ====================================================================

def save_verify_code(email, code, purpose='register', ttl=300):
    """保存/覆盖一条邮箱验证码（同一 email+purpose 唯一）。"""
    _ensure_init()
    now = _now()
    email = (email or '').strip().lower()
    with _write() as conn:
        conn.execute(
            '''INSERT INTO email_verify_codes(
                   email, code, purpose, expires_at, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(email, purpose) DO UPDATE SET
                code=excluded.code, expires_at=excluded.expires_at,
                created_at=excluded.created_at''',
            (email, code, purpose, now + int(ttl), now))
    return email


def get_verify_code(email, purpose='register'):
    """取未过期的验证码记录；无或已过期返回 None。"""
    _ensure_init()
    email = (email or '').strip().lower()
    with _read() as conn:
        row = conn.execute(
            'SELECT * FROM email_verify_codes WHERE email=? AND purpose=?',
            (email, purpose)).fetchone()
    d = _row_to_dict(row)
    if d and d['expires_at'] < _now():
        return None
    return d


def delete_verify_code(email, purpose='register'):
    _ensure_init()
    email = (email or '').strip().lower()
    with _write() as conn:
        conn.execute('DELETE FROM email_verify_codes WHERE email=? AND purpose=?',
                     (email, purpose))


# ====================================================================
#  user_notes（用户备注：owner 给 target 的私人称呼）
# ====================================================================

def set_user_note(owner_id, target_id, note):
    """设置/更新 owner 对 target 的备注；note 为空则删除备注。返回最终备注文本。"""
    _ensure_init()
    note = (note or '').strip()[:40]
    with _write() as conn:
        if not note:
            conn.execute('DELETE FROM user_notes WHERE owner_id=? AND target_id=?',
                         (owner_id, target_id))
            return ''
        conn.execute(
            '''INSERT INTO user_notes(owner_id, target_id, note, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(owner_id, target_id) DO UPDATE SET
                note=excluded.note, updated_at=excluded.updated_at''',
            (owner_id, target_id, note, _now()))
    return note


def get_notes_map(owner_id):
    """返回 owner 设过的所有备注 {target_id: note}，用于批量填充展示名。"""
    _ensure_init()
    with _read() as conn:
        rows = conn.execute(
            'SELECT target_id, note FROM user_notes WHERE owner_id=?',
            (owner_id,)).fetchall()
    return {r['target_id']: r['note'] for r in rows}


# ====================================================================
#  sessions（训练成绩）
# ====================================================================

def save_session(category, symbol, period, mode='free', user_id=None, seed=None,
                 start_ts=None, bar_count=None, stats=None, trades=None,
                 equity=None):
    """保存一次训练会话/成绩。

    ``stats`` 为 TradeEngine.getStatistics() 字典（同时抽取常用字段建列，
    完整原样存 stats_json）；``trades``/``equity`` 为操作流水/权益曲线列表。
    返回 session_id。
    """
    import json
    _ensure_init()
    stats = stats or {}
    with _write() as conn:
        cur = conn.execute(
            '''INSERT INTO sessions(
                user_id, category, symbol, period, mode, seed, start_ts, bar_count,
                final_balance, return_pct, win_rate, max_drawdown_pct,
                profit_loss_ratio, total_trades, sharpe,
                stats_json, trades_json, equity_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (user_id, category, symbol, period, mode, seed, start_ts, bar_count,
             stats.get('finalBalance'), stats.get('returnPct'), stats.get('winRate'),
             stats.get('maxDrawdownPct'), stats.get('profitLossRatio'),
             stats.get('totalTrades'), stats.get('sharpe'),
             json.dumps(stats, ensure_ascii=False),
             json.dumps(trades or [], ensure_ascii=False),
             json.dumps(equity or [], ensure_ascii=False),
             _now()))
        return cur.lastrowid


def get_session(session_id):
    import json
    _ensure_init()
    with _read() as conn:
        row = conn.execute('SELECT * FROM sessions WHERE id=?',
                           (session_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ('stats_json', 'trades_json', 'equity_json'):
        try:
            d[k.replace('_json', '')] = json.loads(d.get(k) or 'null')
        except Exception:
            d[k.replace('_json', '')] = None
    return d


def list_sessions(user_id=None, limit=50):
    _ensure_init()
    with _read() as conn:
        if user_id is None:
            rows = conn.execute(
                'SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?',
                (limit,)).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT ?',
                (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ====================================================================
#  matches（异步同题对战）
# ====================================================================

def _gen_match_code(conn, length=6):
    """生成唯一对战码（去除易混淆字符）"""
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(50):
        code = ''.join(random.choice(alphabet) for _ in range(length))
        exists = conn.execute('SELECT 1 FROM matches WHERE code=?',
                              (code,)).fetchone()
        if not exists:
            return code
    # 极端情况下退化为带时间戳的码
    return 'M' + str(_now())[-6:]


def create_match(category, symbol, period, seed, host_id=None, start_ts=None,
                 end_ts=None, bar_count=None, engine_config=None, blind=0,
                 pvp_mode='code', auto_sync=0, target_sec=None, speed_ms=None,
                 status='open'):
    """创建对战，返回 (match_id, code)。

    pvp_mode：'code'（邀请码，异步）| 'team'（自由组队）。
    auto_sync：1 为自动同步推进（需 target_sec/speed_ms）。
    """
    import json
    _ensure_init()
    with _write() as conn:
        code = _gen_match_code(conn)
        cur = conn.execute(
            '''INSERT INTO matches(
                code, host_id, category, symbol, period, seed, start_ts, end_ts,
                bar_count, engine_config_json, blind, status, created_at,
                pvp_mode, auto_sync, target_sec, speed_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (code, host_id, category, symbol, period, seed, start_ts, end_ts,
             bar_count, json.dumps(engine_config or {}, ensure_ascii=False),
             int(bool(blind)), status, _now(),
             pvp_mode, int(bool(auto_sync)), target_sec, speed_ms))
        return cur.lastrowid, code


def get_match(match_id):
    import json
    _ensure_init()
    with _read() as conn:
        row = conn.execute('SELECT * FROM matches WHERE id=?', (match_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d['engine_config'] = json.loads(d.get('engine_config_json') or '{}')
    except Exception:
        d['engine_config'] = {}
    return d


def get_match_by_code(code):
    _ensure_init()
    with _read() as conn:
        row = conn.execute('SELECT * FROM matches WHERE code=?',
                           ((code or '').strip().upper(),)).fetchone()
    if not row:
        return None
    return get_match(row['id'])


def update_match_status(match_id, status):
    _ensure_init()
    with _write() as conn:
        conn.execute('UPDATE matches SET status=? WHERE id=?', (status, match_id))


def update_match(match_id, **cols):
    """通用更新 matches 白名单列（start_at/paused_at/pause_ms/end_at/status 等）。"""
    allowed = {'status', 'start_at', 'paused_at', 'pause_ms', 'end_at',
               'target_sec', 'speed_ms', 'auto_sync'}
    sets = {k: v for k, v in cols.items() if k in allowed}
    if not sets:
        return False
    args = list(sets.values()) + [match_id]
    with _write() as conn:
        conn.execute(
            'UPDATE matches SET %s WHERE id=?'
            % ', '.join('%s=?' % k for k in sets), args)
    return True


# ---- match_players（自由组队成员/邀请状态）----

def upsert_player(match_id, user_id, role='player', state='invited'):
    """插入或更新成员行（主键 match_id+user_id）。"""
    _ensure_init()
    with _write() as conn:
        conn.execute(
            '''INSERT INTO match_players(match_id, user_id, role, state, ts)
               VALUES(?,?,?,?,?)
               ON CONFLICT(match_id, user_id) DO UPDATE SET
                role=excluded.role, state=excluded.state, ts=excluded.ts''',
            (match_id, user_id, role, state, _now()))
    return True


def set_player_state(match_id, user_id, state):
    """更新某成员状态（invited/accepted/ready/declined）。返回是否更新到。"""
    _ensure_init()
    with _write() as conn:
        cur = conn.execute(
            'UPDATE match_players SET state=?, ts=? WHERE match_id=? AND user_id=?',
            (state, _now(), match_id, user_id))
        return cur.rowcount > 0


def get_player(match_id, user_id):
    _ensure_init()
    with _read() as conn:
        row = conn.execute(
            'SELECT * FROM match_players WHERE match_id=? AND user_id=?',
            (match_id, user_id)).fetchone()
    return _row_to_dict(row)


def get_players(match_id):
    """某房间全部成员行（按角色/时间排序）。"""
    _ensure_init()
    with _read() as conn:
        rows = conn.execute(
            '''SELECT p.*, u.nickname, u.email, u.last_seen
               FROM match_players p LEFT JOIN users u ON u.id = p.user_id
               WHERE p.match_id=?
               ORDER BY (p.role='host') DESC, p.ts ASC''',
            (match_id,)).fetchall()
    return [dict(r) for r in rows]


def count_team_members(match_id):
    """已接受加入的成员数（state ∈ accepted|ready）。"""
    _ensure_init()
    with _read() as conn:
        c = conn.execute(
            "SELECT COUNT(*) AS c FROM match_players WHERE match_id=?"
            " AND state IN ('accepted','ready')", (match_id,)).fetchone()['c']
    return int(c)


def all_members_ready(match_id):
    """所有已接受成员是否都已点「就绪」（ready）。无成员时返回 False。"""
    _ensure_init()
    with _read() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM match_players WHERE match_id=?"
            " AND state IN ('accepted','ready')", (match_id,)).fetchone()['c']
        ready = conn.execute(
            "SELECT COUNT(*) AS c FROM match_players WHERE match_id=?"
            " AND state='ready'", (match_id,)).fetchone()['c']
    return total > 0 and total == ready


def list_my_invites(user_id, limit=50):
    """我收到的待接受组队邀请（state=invited），附带房间与房主信息。"""
    _ensure_init()
    with _read() as conn:
        rows = conn.execute(
            '''SELECT p.match_id, p.ts AS invited_at, h.nickname AS host_nickname,
                      h.email AS host_email, m.code, m.category, m.period,
                      m.bar_count, m.blind, m.status, m.auto_sync, m.pvp_mode
               FROM match_players p
               JOIN matches m ON m.id = p.match_id
               LEFT JOIN users h ON h.id = m.host_id
               WHERE p.user_id=? AND p.state='invited' AND m.status NOT IN ('finished','canceled')
               ORDER BY p.ts DESC LIMIT ?''',
            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def list_matches(host_id=None, status=None, limit=50):
    _ensure_init()
    sql = 'SELECT * FROM matches'
    conds, args = [], []
    if host_id is not None:
        conds.append('host_id=?'); args.append(host_id)
    if status:
        conds.append('status=?'); args.append(status)
    if conds:
        sql += ' WHERE ' + ' AND '.join(conds)
    sql += ' ORDER BY created_at DESC LIMIT ?'; args.append(limit)
    with _read() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


# ====================================================================
#  results（对战成绩 + 排名）
# ====================================================================

# 计分口径：主排序=收益率%（高者优），次序=最大回撤（小者优）、胜率（高者优）、
# 盈亏比（高者优）。写入 results 表并驱动排行榜。
_RANK_ORDER = ('return_pct DESC', 'max_drawdown_pct ASC',
               'win_rate DESC', 'profit_loss_ratio DESC')


def submit_result(match_id, user_id, return_pct=None, max_drawdown_pct=None,
                  win_rate=None, profit_loss_ratio=None, final_balance=None,
                  total_trades=None, session_id=None):
    """提交（或更新）某用户在某对战的成绩，并重算该对战排名。返回 result_id。"""
    _ensure_init()
    with _write() as conn:
        conn.execute(
            '''INSERT INTO results(
                match_id, user_id, session_id, return_pct, max_drawdown_pct,
                win_rate, profit_loss_ratio, final_balance, total_trades,
                rank, submitted_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(match_id, user_id) DO UPDATE SET
                session_id=excluded.session_id,
                return_pct=excluded.return_pct,
                max_drawdown_pct=excluded.max_drawdown_pct,
                win_rate=excluded.win_rate,
                profit_loss_ratio=excluded.profit_loss_ratio,
                final_balance=excluded.final_balance,
                total_trades=excluded.total_trades,
                submitted_at=excluded.submitted_at''',
            (match_id, user_id, session_id, return_pct, max_drawdown_pct,
             win_rate, profit_loss_ratio, final_balance, total_trades,
             None, _now()))
        result_id = conn.execute(
            'SELECT id FROM results WHERE match_id=? AND user_id=?',
            (match_id, user_id)).fetchone()['id']
        _recompute_ranks(conn, match_id)
        return result_id


def _recompute_ranks(conn, match_id):
    """按计分口径重算某对战内所有成绩的 rank（1 起）"""
    rows = conn.execute(
        'SELECT id FROM results WHERE match_id=? ORDER BY %s' % ', '.join(_RANK_ORDER),
        (match_id,)).fetchall()
    for i, r in enumerate(rows, start=1):
        conn.execute('UPDATE results SET rank=? WHERE id=?', (i, r['id']))


def get_results(match_id):
    """某对战的成绩榜（按 rank 升序），附带用户名"""
    _ensure_init()
    with _read() as conn:
        rows = conn.execute(
            '''SELECT r.*, u.nickname, u.email
               FROM results r LEFT JOIN users u ON u.id = r.user_id
               WHERE r.match_id=?
               ORDER BY r.rank IS NULL, r.rank ASC, r.submitted_at ASC''',
            (match_id,)).fetchall()
    return [dict(r) for r in rows]


def get_result(match_id, user_id):
    """取某用户在某对战的成绩行（未参与返回 None）。"""
    _ensure_init()
    with _read() as conn:
        row = conn.execute(
            'SELECT * FROM results WHERE match_id=? AND user_id=?',
            (match_id, user_id)).fetchone()
    return _row_to_dict(row)


def match_completion(match_id):
    """统计对战完成度：返回 (participants, completed)。

    participants = 已加入（有占位/真实成绩行）的人数；
    completed    = 已提交真实成绩（return_pct 非空）的人数。
    """
    _ensure_init()
    with _read() as conn:
        participants = conn.execute(
            'SELECT COUNT(*) AS c FROM results WHERE match_id=?',
            (match_id,)).fetchone()['c']
        completed = conn.execute(
            'SELECT COUNT(*) AS c FROM results WHERE match_id=?'
            ' AND return_pct IS NOT NULL', (match_id,)).fetchone()['c']
    return int(participants), int(completed)


def list_matches_by_user(user_id, limit=50):
    """与某用户相关的对战：其为房主、有成绩记录、或在 match_players 中。倒序去重。"""
    _ensure_init()
    with _read() as conn:
        rows = conn.execute(
            '''SELECT DISTINCT m.* FROM matches m
               LEFT JOIN results r ON r.match_id = m.id
               LEFT JOIN match_players p ON p.match_id = m.id AND p.user_id = ?
               WHERE m.host_id = ? OR r.user_id = ? OR p.user_id IS NOT NULL
               ORDER BY m.created_at DESC LIMIT ?''',
            (user_id, user_id, user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def leaderboard(category=None, period=None, limit=50):
    """全局排行榜：取每个用户在各对战中的最佳收益率成绩聚合排名。

    可按品类/周期过滤（经 matches 表）。返回按 return_pct 降序的列表。
    """
    _ensure_init()
    sql = '''SELECT r.user_id, u.nickname, u.email,
                    r.match_id, m.category, m.period, m.symbol,
                    r.return_pct, r.max_drawdown_pct, r.win_rate,
                    r.profit_loss_ratio, r.final_balance, r.total_trades,
                    r.submitted_at
             FROM results r
             JOIN matches m ON m.id = r.match_id
             LEFT JOIN users u ON u.id = r.user_id'''
    conds, args = ['r.return_pct IS NOT NULL'], []   # 榜单只展示已提交真实成绩
    if category:
        conds.append('m.category=?'); args.append(category)
    if period:
        conds.append('m.period=?'); args.append(period)
    if conds:
        sql += ' WHERE ' + ' AND '.join(conds)
    sql += ' ORDER BY r.return_pct DESC LIMIT ?'; args.append(limit)
    with _read() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


# ====================================================================
#  blind_sessions（盲选答案：按 training_id 存，揭晓前保密）
# ====================================================================

def create_blind(blind_id, category, symbol, name, period, start_ts=None,
                 end_ts=None, bar_count=None, seed=None):
    """存一条盲选答案（training_id 唯一）。返回 blind_id。"""
    _ensure_init()
    with _write() as conn:
        conn.execute(
            '''INSERT INTO blind_sessions(
                id, category, symbol, name, period, start_ts, end_ts,
                bar_count, seed, revealed, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,0,?)''',
            (blind_id, category, symbol, name, period, start_ts, end_ts,
             bar_count, seed, _now()))
    return blind_id


def get_blind(blind_id):
    _ensure_init()
    with _read() as conn:
        row = conn.execute('SELECT * FROM blind_sessions WHERE id=?',
                           (blind_id,)).fetchone()
    return _row_to_dict(row)


def mark_blind_revealed(blind_id):
    _ensure_init()
    with _write() as conn:
        conn.execute('UPDATE blind_sessions SET revealed=1 WHERE id=?',
                     (blind_id,))
