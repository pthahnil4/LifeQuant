import sys
import os
import datetime
import time
import json
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 日志文件路径
LOG_PATH = os.path.join(BASE_DIR, 'strategy.log')

def log(msg):
    """写入日志文件，同时打印到控制台，确保任何环境下都能看到"""
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[日志写入失败] {LOG_PATH} | {e}", flush=True)

# 策略模块路径（strategy/ 目录）
STRATEGY_DIR = os.path.join(BASE_DIR, "strategy")
log(f"BASE_DIR={BASE_DIR}")
log(f"STRATEGY_DIR={STRATEGY_DIR}")
log(f"STRATEGY_DIR exists={os.path.isdir(STRATEGY_DIR)}")
log(f"sys.path 前3项={sys.path[:3]}")
if STRATEGY_DIR not in sys.path:
    sys.path.insert(0, STRATEGY_DIR)
    log(f"已将 STRATEGY_DIR 加入 sys.path")

# 从配置文件读取币种列表
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# 币种自选配置已迁移 MySQL（迁移批次7a，kv_store key='coin_selection'）：
# DB 不可用时回退本地 config.json，语义与 JSON 版一致。
# 全币种行情已迁移 MySQL（迁移批次7b，crypto_coins 表）：
# DB 不可用时回退本地 crypto_coins.csv。
# 双模式导入：Flask 包内（cryptoTrade 根在 sys.path）/ 独立脚本（仅 crypto 目录）
try:
    from crypto.database import session_scope as _db_session_scope
    from crypto import config_store_repo as _config_store_repo
    from crypto import market_data_repo as _market_repo
except ImportError:
    _db_session_scope = None
    _config_store_repo = None
    _market_repo = None

# 策略计算全局串行锁（pro3 引擎的模块级全局是共享可变状态）。
# 这里不能像上面那样「导不到就置 None」：退化成没有锁等于铺锁是假象，
# 所以两条路径都必须拿到真函数（锁对象本身进程唯一，见 strategy_gate 文档）。
try:
    from .task.strategy_gate import pro3_locked
except ImportError:
    _TASK_DIR = os.path.join(BASE_DIR, 'task')
    if _TASK_DIR not in sys.path:
        sys.path.insert(0, _TASK_DIR)
    from strategy_gate import pro3_locked

def load_config():
    """加载币种自选配置（带 TTL 缓存的 DB 优先，失败回退本地文件）

    页面加载时 get_all_coins/get_selected_coins/get_starred_coins 等会
    多次调用本函数，缓存后 TTL 内无 DB 往返；save_config 写入时
    写穿透失效缓存，即时生效。
    """
    if _db_session_scope is not None and _config_store_repo is not None:
        try:
            cfg = _config_store_repo.load_json_config_cached(
                _config_store_repo.KEY_COIN_SELECTION)
            if cfg is not None:
                # 确保新字段存在
                if 'starred_coins' not in cfg:
                    cfg['starred_coins'] = []
                # 浮动币种：从趋势扫描页临时添加的监控币种，与固定的 all_coins 区分
                if 'floating_coins' not in cfg:
                    cfg['floating_coins'] = []
                return cfg
        except Exception:
            pass
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            # 确保新字段存在
            if 'starred_coins' not in cfg:
                cfg['starred_coins'] = []
            # 浮动币种：从趋势扫描页临时添加的监控币种，与固定的 all_coins 区分
            if 'floating_coins' not in cfg:
                cfg['floating_coins'] = []
            return cfg
    except Exception:
        return {
            "all_coins": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "NEAR-USDT-SWAP"],
            "default_selected": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "NEAR-USDT-SWAP"],
            "starred_coins": [],
            "floating_coins": []
        }

# 币种自选配置的写库结果（线程本地）。load_config 是 DB 优先，所以
# 「DB 写失败 + 文件写成功」等于改动根本没生效 —— 这个结果必须能传到
# HTTP 层如实提示。用线程本地状态而不是给 7 个业务函数各加一个返回值，
# 是为了不改动 add/remove/clear 这些已有返回契约的函数签名。
_save_ctx = threading.local()


def last_save_db_ok() -> bool:
    """取本线程最近一次 save_config 的 DB 主存写入结果，取完即清。

    做成一次性消费：避免上一个请求的失败残留到本请求（本轮根本没保存过
    的接口，如 removed=0 的清空浮动币种）被误报成写库失败。没保存过时
    返回 True —— 不编造失败。
    """
    ok = getattr(_save_ctx, 'db_ok', True)
    try:
        del _save_ctx.db_ok
    except AttributeError:
        pass
    return ok


def save_config(config):
    """保存币种自选配置（DB 主存 + 本地文件双写，文件保留为兜底数据源）

    返回 DB 主存是否写入成功，同时记入 last_save_db_ok()。

    这里历史实现是 `except Exception: pass`：DB 写失败时本地文件照写、接口
    照回 200，而所有读取都是 DB 优先 —— 用户以为改好了，页面刷回旧值，
    监控/分析继续按旧币种跑。双写的意义是兜底，不是把失败吃掉，所以改为
    留痕 + 把结果透出给调用方。文件写入失败仍然抛异常（保持原行为）。
    """
    db_ok = False
    if _db_session_scope is not None and _config_store_repo is not None:
        try:
            with _db_session_scope() as _s:
                _config_store_repo.save_json_config(
                    _s, _config_store_repo.KEY_COIN_SELECTION, config)
            db_ok = True
        except Exception as e:
            log(f"【配置主存写入失败】币种自选配置写 DB 未成功，DB 内仍是旧值，"
                f"本次改动不会被后续读取看到: {e!r}")
    else:
        log("【配置主存不可用】DB 层未导入成功，币种自选配置仅写入本地文件")
    _save_ctx.db_ok = db_ok
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    return db_ok

def _sort_fixed_by_market_cap(coins, config):
    """固定币种按市值排名降序（排名数字升序）稳定排序。

    排名依据 config['market_ranks']（inst_id → CoinGecko 市值排名，
    由「更新市值列表」刷新写入）；无排名数据的币（手动提升/未入榜）
    保持原有相对顺序排在最后；从未刷新过时（无 market_ranks）列表原样返回。
    """
    ranks = config.get("market_ranks") or {}
    if not ranks:
        return coins
    return sorted(coins, key=lambda c: ranks.get(c, 10 ** 9))


def get_all_coins():
    """获取所有可用币种（固定币种按市值降序 + 浮动币种排最后）"""
    config = load_config()
    fixed = _sort_fixed_by_market_cap(config.get("all_coins", []), config)
    floating = [c for c in config.get("floating_coins", []) if c not in fixed]
    return fixed + floating


def get_fixed_coins():
    """获取固定币种（按 CoinGecko 实时市值降序，不可随意删改）"""
    config = load_config()
    return _sort_fixed_by_market_cap(config.get("all_coins", []), config)


def get_floating_coins():
    """获取浮动币种（从趋势扫描页临时添加，可灵活增删）"""
    return load_config().get("floating_coins", [])


def add_floating_coins(coins):
    """批量添加浮动币种。

    只追加尚不存在于固定币种和现有浮动币种中的项，返回 (更新后的浮动列表, 新增数)。
    """
    config = load_config()
    fixed = set(config.get("all_coins", []))
    floating = config.get("floating_coins", [])
    seen = set(floating) | fixed
    added = 0
    for c in coins:
        c = (c or "").strip()
        if c and c not in seen:
            floating.append(c)
            seen.add(c)
            added += 1
    config["floating_coins"] = floating
    save_config(config)
    return floating, added


def remove_floating_coin(coin):
    """删除单个浮动币种，同时从选中列表中移除，返回更新后的浮动列表。"""
    config = load_config()
    floating = [c for c in config.get("floating_coins", []) if c != coin]
    config["floating_coins"] = floating
    # 同步从已选中列表移除，避免残留监控卡片
    selected = [c for c in config.get("default_selected", []) if c != coin]
    config["default_selected"] = selected
    save_config(config)
    return floating


def clear_floating_coins():
    """清空所有浮动币种，同时从选中列表中移除，返回被清空的数量。"""
    config = load_config()
    floating = config.get("floating_coins", [])
    removed_count = len(floating)
    if removed_count:
        floating_set = set(floating)
        config["floating_coins"] = []
        # 同步从已选中列表移除，避免残留监控卡片
        selected = [c for c in config.get("default_selected", []) if c not in floating_set]
        config["default_selected"] = selected
        save_config(config)
    return removed_count

def get_selected_coins():
    """获取当前选中的币种"""
    return load_config().get("default_selected", [])

def set_selected_coins(coins):
    """设置当前选中的币种"""
    config = load_config()
    config["default_selected"] = coins
    save_config(config)


def get_csv_coins():
    """动态获取所有币种列表（迁移批次7b 起 DB 优先，失败回退 CSV 文件）"""
    if _db_session_scope is not None and _market_repo is not None:
        try:
            with _db_session_scope() as _s:
                coins = _market_repo.coin_inst_ids(_s)
            if coins:
                return coins
        except Exception as e:
            log(f"从DB读取币种列表失败，回退CSV文件: {e}")
    import csv
    csv_path = os.path.join(BASE_DIR, 'crypto_coins.csv')
    coins = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                inst_id = row.get('inst_id', '').strip()
                if inst_id:
                    coins.append(inst_id)
    except Exception as e:
        log(f"读取CSV币种列表失败: {e}")
    return coins


def sync_coin_list():
    """将CSV中的币种同步到all_coins（保留已存在的手动添加项，追加CSV新增项）"""
    csv_coins = get_csv_coins()
    config = load_config()
    existing = set(config.get("all_coins", []))
    # 以CSV为准构建all_coins（保持CSV顺序），但保留可能的手动添加项
    merged = []
    seen = set()
    for c in csv_coins:
        if c not in seen:
            merged.append(c)
            seen.add(c)
    # 追加config中独有但CSV中没有的（手动添加的）
    for c in config.get("all_coins", []):
        if c not in seen:
            merged.append(c)
            seen.add(c)
    config["all_coins"] = merged
    save_config(config)
    return merged


# ================================================================
#  crypto_coins「宇宙」维护（DB 表 + CSV 文件双写）
# ================================================================
# 宇宙 = 批量趋势分析(batch_trend_updater)的输入清单，也是 get_csv_coins()
# /「CSV同步固定」的数据源。关键不变量：batch_trend_updater 只处理宇宙里
# 「已有」的币（读宇宙→分析→原样写回），不会去交易所重新发现新币种。因此
# 从宇宙删除的币不会自动复活 —— 「移除选中」必须同时删宇宙，否则一按
# 「CSV同步固定」就被打回原形（这正是此前删了又冒出来的根因）。
# DB 为主存、CSV 为兜底；两者列数可能不同（DB=48列含15m，旧CSV=37列），
# 故 CSV 写回严格保留其自身表头，避免格式漂移。
_UNIVERSE_CSV = os.path.join(BASE_DIR, 'crypto_coins.csv')


def _read_universe_csv():
    """读 CSV 宇宙，返回 (fieldnames, rows)；文件缺失/损坏返回 ([], [])。"""
    import csv
    try:
        with open(_UNIVERSE_CSV, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = [dict(r) for r in reader]
        return fieldnames, rows
    except Exception as e:
        log(f"【宇宙】读取 CSV 失败: {e!r}")
        return [], []


def _write_universe_csv(fieldnames, rows):
    """按给定表头写回 CSV 宇宙（保留原列结构，避免格式漂移）。"""
    import csv
    if not fieldnames:
        return
    try:
        with open(_UNIVERSE_CSV, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        log(f"【宇宙】写回 CSV 失败: {e!r}")


def _universe_remove_coins(targets):
    """从宇宙（DB 表 + CSV 文件）删除 inst_id ∈ targets 的行，返回实际删除的集合。

    删除后 batch_trend_updater 不再处理它们，也不会复活。
    安全护栏：若删除会导致宇宙被清空，则放弃该存储的删除（记日志），避免误清全表。
    """
    targets = {(c or '').strip() for c in targets if (c or '').strip()}
    if not targets:
        return set()
    removed = set()

    # --- DB 主存 ---
    if _db_session_scope is not None and _market_repo is not None:
        try:
            with _db_session_scope() as _s:
                rows = _market_repo.load_coin_rows(_s)
            hit = {(r.get('inst_id') or '').strip() for r in rows} & targets
            kept = [r for r in rows if (r.get('inst_id') or '').strip() not in targets]
            if hit and kept:
                with _db_session_scope() as _s:
                    _market_repo.save_coin_rows(_s, kept)
                removed |= hit
            elif hit and not kept:
                log(f"【宇宙删行】DB 删除会导致宇宙清空，已放弃: {sorted(hit)}")
        except Exception as e:
            log(f"【宇宙删行】DB 操作失败: {e!r}")

    # --- CSV 兜底（保留原表头）---
    fieldnames, crows = _read_universe_csv()
    if fieldnames:
        hit = {(r.get('inst_id') or '').strip() for r in crows} & targets
        kept = [r for r in crows if (r.get('inst_id') or '').strip() not in targets]
        if hit and kept:
            _write_universe_csv(fieldnames, kept)
            removed |= hit
        elif hit and not kept:
            log(f"【宇宙删行】CSV 删除会导致宇宙清空，已放弃: {sorted(hit)}")

    return removed


def _universe_add_coins(targets):
    """把 inst_id ∈ targets 加入宇宙（DB 表 + CSV 文件），已存在则跳过。

    新增行只填身份列（rank/symbol/inst_id），指标列留空，由 batch_trend_updater
    下次运行时自动补全。返回实际新增的 inst_id 列表（保持传入顺序、去重）。
    """
    req = []
    seen = set()
    for c in targets:
        c = (c or '').strip()
        if c and c not in seen:
            req.append(c)
            seen.add(c)
    if not req:
        return []

    def _minimal(inst_id, rank, keys):
        row = {k: '' for k in keys} if keys else {}
        row['rank'] = str(rank)
        row['symbol'] = inst_id.split('-')[0]
        row['inst_id'] = inst_id
        row['name_cn'] = ''
        return row

    def _max_rank(rows):
        m = 0
        for r in rows:
            rk = (r.get('rank') or '').strip()
            if rk.isdigit():
                m = max(m, int(rk))
        return m

    added = set()

    # --- DB 主存 ---
    if _db_session_scope is not None and _market_repo is not None:
        try:
            with _db_session_scope() as _s:
                rows = _market_repo.load_coin_rows(_s)
            existing = {(r.get('inst_id') or '').strip() for r in rows}
            keys = list(rows[0].keys()) if rows else ['rank', 'symbol', 'inst_id', 'name_cn']
            rank = _max_rank(rows)
            new_rows = list(rows)
            for c in req:
                if c in existing:
                    continue
                rank += 1
                new_rows.append(_minimal(c, rank, keys))
                existing.add(c)
                added.add(c)
            if len(new_rows) != len(rows):
                with _db_session_scope() as _s:
                    _market_repo.save_coin_rows(_s, new_rows)
        except Exception as e:
            log(f"【宇宙加行】DB 操作失败: {e!r}")

    # --- CSV 兜底（保留原表头）---
    fieldnames, crows = _read_universe_csv()
    if fieldnames:
        existing = {(r.get('inst_id') or '').strip() for r in crows}
        rank = _max_rank(crows)
        new_rows = list(crows)
        for c in req:
            if c in existing:
                continue
            rank += 1
            new_rows.append(_minimal(c, rank, fieldnames))
            existing.add(c)
            added.add(c)
        if len(new_rows) != len(crows):
            _write_universe_csv(fieldnames, new_rows)

    return [c for c in req if c in added]


def remove_fixed_coins(coins):
    """从固定列表移除选中币种，并从 crypto_coins 宇宙彻底删除（下架币清理）。

    - 至少保留一个固定币种：若移除后固定列表为空则抛 ValueError（HTTP 层转 400）。
    - 同步从 default_selected / starred_coins / floating_coins 剔除，避免残留。
    - 关键：同时删「宇宙」(DB表+CSV)，否则「CSV同步固定」会把下架币复活；
      batch_trend_updater 只处理宇宙内已有币，删除后不会自动加回（永久生效）。
    - 返回实际被移除的固定币种列表（保持原固定列表顺序）。
    """
    config = load_config()
    fixed = config.get("all_coins", [])
    remove_set = {(c or "").strip() for c in coins if (c or "").strip()}
    new_fixed = [c for c in fixed if c not in remove_set]
    if not new_fixed:
        raise ValueError("至少保留一个固定币种")
    removed = [c for c in fixed if c in remove_set]
    removed_set = set(removed)
    config["all_coins"] = new_fixed
    # 被移除的币种不再监控：从选中/星标/浮动列表一并剔除，避免残留
    config["default_selected"] = [c for c in config.get("default_selected", [])
                                  if c not in removed_set]
    config["starred_coins"] = [c for c in config.get("starred_coins", [])
                               if c not in removed_set]
    config["floating_coins"] = [c for c in config.get("floating_coins", [])
                                if c not in removed_set]
    # 从宇宙彻底删除，确保「CSV同步固定」不会把下架币复活
    universe_removed = _universe_remove_coins(removed_set)
    log(f"【移除固定】config 移除 {removed} | 宇宙删除 {sorted(universe_removed)}")
    save_config(config)
    return removed


def promote_floating_to_fixed(coins):
    """将浮动币种提升为固定：加入 all_coins、从 floating_coins 剔除，并写入宇宙。

    - 关键：同步写入 crypto_coins 宇宙(DB表+CSV)，否则「CSV同步固定」会因该币
      不在宇宙里而把它从固定列表丢弃（这正是此前"提升不成功"的根因）。
    - 修正"既是固定又是浮动"的重复态：无论是否新加入固定，都从浮动列表剔除。
    - 提升后币种保持原有选中/星标状态；宇宙新增行的指标由批量分析下次补全。
    - 返回本次被提升为固定的币种列表（保持传入顺序、去重）。
    """
    config = load_config()
    fixed = config.get("all_coins", [])
    floating = config.get("floating_coins", [])
    fixed_set = set(fixed)
    req = []
    seen = set()
    for c in coins:
        c = (c or "").strip()
        if c and c not in seen:
            req.append(c)
            seen.add(c)
    for c in req:
        if c not in fixed_set:
            fixed.append(c)
            fixed_set.add(c)
    config["all_coins"] = fixed
    # 无论是否新加入固定，都从浮动列表剔除，消除"既固定又浮动"的重复态
    config["floating_coins"] = [c for c in floating if c not in seen]
    # 写入宇宙，避免「CSV同步固定」把刚提升的币丢弃
    universe_added = _universe_add_coins(req)
    log(f"【提升固定】提升 {req} | 宇宙新增 {universe_added}")
    save_config(config)
    return req


def sync_fixed_from_csv():
    """以 crypto_coins 宇宙为准，把固定列表(all_coins)对齐到宇宙（安全语义）。

    自「移除选中/提升为固定」改为同步维护宇宙后，宇宙即固定列表的唯一真相：
    - 已下架并被「移除选中」删掉的币不再在宇宙里 → 同步不会复活它们；
    - 被「提升为固定」的币已写入宇宙 → 同步不会丢弃它们。
    因此这里的"覆盖"是安全的：结果 == 当前宇宙 == 用户维护后的固定列表。
    与 sync_coin_list（合并、保留手动添加项）不同，这里是整表对齐：
    保持宇宙顺序、去重；浮动币种保持不变（剔除已进入固定列表者）；
    已选中列表剔除对齐后不再存在的币种。返回对齐后的固定币种列表。
    """
    csv_coins = get_csv_coins()
    new_fixed = []
    seen = set()
    for c in csv_coins:
        c = (c or "").strip()
        if c and c not in seen:
            new_fixed.append(c)
            seen.add(c)
    if not new_fixed:
        raise ValueError("CSV 中未读取到任何币种，已取消同步")
    config = load_config()
    config["all_coins"] = new_fixed
    # 浮动币种保持不变，但剔除已进入固定列表的项，避免同一币种既是固定又是浮动
    floating = [c for c in config.get("floating_coins", []) if c not in seen]
    config["floating_coins"] = floating
    # 已选中列表剔除同步后不再存在的币种
    valid_all = seen | set(floating)
    config["default_selected"] = [c for c in config.get("default_selected", []) if c in valid_all]
    save_config(config)
    return new_fixed


def get_starred_coins():
    """获取星标币种列表"""
    return load_config().get("starred_coins", [])


def set_starred_coins(coins):
    """设置星标币种列表"""
    config = load_config()
    config["starred_coins"] = list(coins)
    save_config(config)


def clear_starred_coins():
    """清除所有星标"""
    config = load_config()
    config["starred_coins"] = []
    save_config(config)


def clear_all_selections():
    """清空所有选择（取消选中所有币种）"""
    config = load_config()
    config["default_selected"] = []
    save_config(config)

COIN_LIST = get_all_coins()

def determine_market_state(adx, adxr, adx_prev, plus_di, minus_di, plus_di_prev, minus_di_prev):
    """
    根据 ADX、ADXR、+DI、-DI 判断12种市场状态
    """
    # ADX 趋势方向
    adx_change = adx - adx_prev
    if adx_change > 0.5:
        adx_trend = 'rising'
    elif adx_change < -0.5:
        adx_trend = 'falling'
    else:
        adx_trend = 'flat'

    # DI 差距变化（判断是否扩大/缩小）
    di_gap = plus_di - minus_di
    di_gap_prev = plus_di_prev - minus_di_prev
    gap_expanding = abs(di_gap) > abs(di_gap_prev) + 0.5

    # DI 交叉检测
    di_cross_up = plus_di_prev <= minus_di_prev and plus_di > minus_di
    di_cross_down = plus_di_prev >= minus_di_prev and plus_di < minus_di

    # 1. 震荡无趋势 ADX<20，+DI≈-DI，反复交叉
    if adx < 20:
        return "震荡无趋势"

    # 12. 趋势过热预警 ADX>50，价格加速但 ADX 开始拐头
    if adx > 50 and adx_trend == 'falling':
        return "趋势过热预警"

    # 10/11. 强趋势转震荡 ADX 从 > 25 快速回落至 < 25
    if adx_prev > 25 and adx < 25:
        if plus_di > minus_di:
            return "强上涨转震荡"
        else:
            return "强下跌转震荡"

    # 2/3. 震荡转趋势初期 ADX 20-25 抬头，DI 交叉
    if 20 <= adx <= 25 and adx_trend == 'rising':
        if di_cross_up:
            return "震荡转上涨初期"
        if di_cross_down:
            return "震荡转下跌初期"

    # 4-9. ADX > 25 区域
    if adx > 25:
        if plus_di > minus_di:
            if gap_expanding and adx_trend == 'rising':
                return "上涨趋势增强"
            elif adx_trend == 'flat':
                return "上涨趋势稳定"
            else:
                return "上涨趋势减弱"
        else:
            if gap_expanding and adx_trend == 'rising':
                return "下跌趋势增强"
            elif adx_trend == 'flat':
                return "下跌趋势稳定"
            else:
                return "下跌趋势减弱"

    # 兜底
    if plus_di > minus_di:
        return "上涨趋势稳定"
    else:
        return "下跌趋势稳定"


@pro3_locked(default_timeout=60.0, on_busy='raise')
def calculate_single_coin_data(instId, bar="1H", max_retries=2):
    """
    计算单个币种的数据，用于前端逐个异步加载，防止长时间阻塞。
    内置重试机制，遇到 IO/连接错误自动重试；失败时直接抛异常。

    本函数会改写 pro3 引擎全局（FAST_MODE / PRINT_* / CURRENT_INSTID），
    且函数体本身不做恢复，因此必须在策略计算独占权下执行；等不到锁时
    抛 StrategyBusy（而不是返回 None），由调用方的 except 分支如实记成
    「该币种本轮未取到」。

    参数:
        instId: 币种ID
        bar:    K线周期，如 "15m", "1H", "4H", "1D"（直接透传给 get_latest_data，OKX 支持的周期均可）
    """
    try:
        import pro3_singletimeframe as strategy_module
        strategy_module.PRINT_MARKET = 0
        strategy_module.PRINT_TRADE_OPS = 0
        strategy_module.PRINT_TRADE_RECORDS = 0
        strategy_module.FAST_MODE = True
        has_real_strategy = True
    except ImportError as e:
        import traceback
        log(f"【策略模块导入失败】{e}")
        log(traceback.format_exc())
        has_real_strategy = False

    for attempt in range(max_retries + 1):
        try:
            if not has_real_strategy:
                raise Exception("未找到策略模块")

            from pro3_singletimeframe import get_latest_data

            latest_data, last_trade_data, atr = get_latest_data(instId, bar)

            timestamp = latest_data.name.strftime("%Y-%m-%d %H:%M:%S")
            price = latest_data.get('close', 0.0)
            macd_line = latest_data.get('DIF', 0.0)       # DIF 快线
            histogram = latest_data.get('MACD', 0.0)       # MACD 柱 = 2*(DIF-DEA)
            adx_val = latest_data.get('ADX', 0.0)
            adxr_val = latest_data.get('ADXR', 0.0)
            plus_di = latest_data.get('+DI', 0.0)
            minus_di = latest_data.get('-DI', 0.0)
            # 前一K线值，用于判断趋势方向
            adx_prev = latest_data.get('ADX_PREV', adx_val)
            plus_di_prev = latest_data.get('+DI_PREV', plus_di)
            minus_di_prev = latest_data.get('-DI_PREV', minus_di)
            modify_flag = latest_data.get('MODIFY_FLAG', 'wait')

            last_trade_price = 0.0
            current_profit = 0.0
            trade_time = ""
            hold_time = ""
            if last_trade_data is not None:
                last_trade_price = last_trade_data.get('TRADE_PRICE', 0.0)
                if last_trade_price == 0.0:
                    last_trade_price = last_trade_data.get('PRICE', 0.0)
                for key in ['TRADE_DATE', 'DATE', 'TRADE_TIME']:
                    if key in last_trade_data:
                        trade_time = last_trade_data[key]
                        break
                # 无论是否有交易价格，只要有交易时间就计算持仓时长
                if trade_time != "" and trade_time is not None:
                    try:
                        # trade_time 可能是 datetime 对象（backtrader返回）或字符串
                        if isinstance(trade_time, datetime.datetime):
                            trade_dt = trade_time
                        elif isinstance(trade_time, str):
                            trade_dt = datetime.datetime.strptime(trade_time, "%Y-%m-%d %H:%M:%S")
                        else:
                            trade_dt = None
                        if trade_dt is not None:
                            now = datetime.datetime.now()
                            delta = now - trade_dt
                            days = delta.days
                            hours = delta.seconds // 3600
                            mins = (delta.seconds % 3600) // 60
                            if days > 0:
                                hold_time = f"{days}天{hours}小时{mins}分"
                            else:
                                hold_time = f"{hours}小时{mins}分"
                            # 格式化交易时间: 2026-5-8 18:18:16
                            trade_time = trade_dt.strftime("%Y-%#m-%#d %H:%M:%S")
                    except Exception:
                        hold_time = ""
                if last_trade_price > 0:
                    if modify_flag == 'rise':
                        current_profit = ((price / last_trade_price) - 1) * 100 * 10.0
                    elif modify_flag == 'fall':
                        current_profit = (1 - (price / last_trade_price)) * 100 * 10.0

            log(f"{instId:15} | 现价: {price:<8.4f} | 交易价: {last_trade_price:<8.4f} | 盈亏: {current_profit:+.2f}%")

            # 趋势方向：直接用 modify_flag
            if modify_flag == 'rise':
                action_signal = "上涨"
            elif modify_flag == 'fall':
                action_signal = "下跌"
            else:
                action_signal = "观望"

            # 12种市场状态
            market_state = determine_market_state(
                adx_val, adxr_val, adx_prev,
                plus_di, minus_di, plus_di_prev, minus_di_prev
            )

            calc_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            return {
                "timestamp": calc_time,
                "symbol": instId,
                "price": round(price, 4),
                "indicators": {
                    "macd": {
                        "histogram": round(histogram, 4),
                        "dif": round(macd_line, 4)
                    },
                    "adx": {
                        "adx": round(adx_val, 4),
                        "adxr": round(adxr_val, 4),
                        "+di": round(plus_di, 4),
                        "-di": round(minus_di, 4)
                    },
                    "atr": round(atr, 4) if atr is not None else 0
                },
                "analysis": {
                    "market_state": market_state,
                    "action_signal": action_signal,
                    "modify_flag": modify_flag
                },
                "trade_info": {
                    "last_trade_price": round(last_trade_price, 4),
                    "current_profit": round(current_profit, 2),
                    "trade_time": trade_time,
                    "hold_time": hold_time
                }
            }

        except Exception as e:
            error_msg = str(e)
            is_io_error = any(k in error_msg.lower() for k in ["closed file", "10035", "connection", "timeout", "ssl", "i/o"])

            if is_io_error and attempt < max_retries:
                wait_sec = 0.5 * (attempt + 1)
                log(f"{instId:15} | IO错误，{wait_sec}s后第{attempt + 2}次重试...")
                time.sleep(wait_sec)
                continue

            log(f"{instId:15} | 获取失败: {error_msg}")
            raise

    return None

def calculate_multi_period_data(instId, bars=None):
    """
    获取单个币种在多个周期的趋势方向数据，用于多周期总览页。

    参数:
        instId: 币种ID
        bars:   周期列表，如 ["5m", "15m", "1H", "2H", "6H", "1D", "1W"]

    返回:
        dict: { "5m": {...}, "15m": {...}, ... }
    """
    if bars is None:
        bars = ["3m", "5m", "15m", "1H", "2H", "6H", "1D", "1W"]

    result = {}
    for bar in bars:
        try:
            data = calculate_single_coin_data(instId, bar=bar, max_retries=1)
            if data:
                result[bar] = {
                    "price": data["price"],
                    "action_signal": data["analysis"]["action_signal"],
                    "modify_flag": data["analysis"]["modify_flag"],
                    "market_state": data["analysis"]["market_state"],
                    "macd_histogram": data["indicators"]["macd"]["histogram"],
                    "adx": data["indicators"]["adx"]["adx"],
                    "timestamp": data["timestamp"]
                }
            else:
                result[bar] = {"error": "数据获取失败", "action_signal": "--", "modify_flag": "wait"}
        except Exception as e:
            result[bar] = {"error": str(e), "action_signal": "--", "modify_flag": "wait"}

    return result

@pro3_locked(default_timeout=60.0, on_busy='raise')
def calculate_dual_period_data(instId, short_bar="1H", long_bar="1D", max_retries=2):
    """
    计算单个币种的双周期策略数据。
    短周期生成交易信号，长周期确认趋势方向。

    与 calculate_single_coin_data 同理：改写引擎全局，整段要在独占权下跑。

    参数:
        instId:    币种ID
        short_bar: 短周期（信号周期），如 "15m", "1H"
        long_bar:  长周期（趋势周期），如 "4H", "1D"
    """
    try:
        import pro3_singletimeframe as strategy_module
        strategy_module.PRINT_MARKET = 0
        strategy_module.PRINT_TRADE_OPS = 0
        strategy_module.PRINT_TRADE_RECORDS = 0
        strategy_module.FAST_MODE = True
        has_real_strategy = True
    except ImportError as e:
        log(f"【策略模块导入失败】{e}")
        has_real_strategy = False

    for attempt in range(max_retries + 1):
        try:
            if not has_real_strategy:
                raise Exception("未找到策略模块")

            from pro3_dualtimeframe import get_latest_data_dual

            latest_data, last_trade_data, atr, long_direction, df_short, df_long = \
                get_latest_data_dual(instId, short_bar, long_bar)

            timestamp = latest_data.name.strftime("%Y-%m-%d %H:%M:%S")
            price = latest_data.get('close', 0.0)
            macd_line = latest_data.get('DIF', 0.0)
            histogram = latest_data.get('MACD', 0.0)
            adx_val = latest_data.get('ADX', 0.0)
            adxr_val = latest_data.get('ADXR', 0.0)
            plus_di = latest_data.get('+DI', 0.0)
            minus_di = latest_data.get('-DI', 0.0)
            adx_prev = latest_data.get('ADX_PREV', adx_val)
            plus_di_prev = latest_data.get('+DI_PREV', plus_di)
            minus_di_prev = latest_data.get('-DI_PREV', minus_di)
            modify_flag = latest_data.get('MODIFY_FLAG', 'wait')

            last_trade_price = 0.0
            current_profit = 0.0
            trade_time = ""
            hold_time = ""
            if last_trade_data is not None:
                last_trade_price = last_trade_data.get('TRADE_PRICE', 0.0)
                if last_trade_price == 0.0:
                    last_trade_price = last_trade_data.get('PRICE', 0.0)
                for key in ['TRADE_DATE', 'DATE', 'TRADE_TIME']:
                    if key in last_trade_data:
                        trade_time = last_trade_data[key]
                        break
                if trade_time != "" and trade_time is not None:
                    try:
                        if isinstance(trade_time, datetime.datetime):
                            trade_dt = trade_time
                        elif isinstance(trade_time, str):
                            trade_dt = datetime.datetime.strptime(trade_time, "%Y-%m-%d %H:%M:%S")
                        else:
                            trade_dt = None
                        if trade_dt is not None:
                            now = datetime.datetime.now()
                            delta = now - trade_dt
                            days = delta.days
                            hours = delta.seconds // 3600
                            mins = (delta.seconds % 3600) // 60
                            if days > 0:
                                hold_time = f"{days}天{hours}小时{mins}分"
                            else:
                                hold_time = f"{hours}小时{mins}分"
                            trade_time = trade_dt.strftime("%Y-%#m-%#d %H:%M:%S")
                    except Exception:
                        hold_time = ""
                if last_trade_price > 0:
                    if modify_flag == 'rise':
                        current_profit = ((price / last_trade_price) - 1) * 100 * 10.0
                    elif modify_flag == 'fall':
                        current_profit = (1 - (price / last_trade_price)) * 100 * 10.0

            log(f"{instId:15} | 双周期({short_bar}/{long_bar}) | 现价: {price:<8.4f} | 长周期方向: {long_direction}")

            if modify_flag == 'rise':
                action_signal = "上涨"
            elif modify_flag == 'fall':
                action_signal = "下跌"
            else:
                action_signal = "观望"

            market_state = determine_market_state(
                adx_val, adxr_val, adx_prev,
                plus_di, minus_di, plus_di_prev, minus_di_prev
            )

            calc_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            return {
                "timestamp": calc_time,
                "symbol": instId,
                "price": round(price, 4),
                "indicators": {
                    "macd": {
                        "histogram": round(histogram, 4),
                        "dif": round(macd_line, 4)
                    },
                    "adx": {
                        "adx": round(adx_val, 4),
                        "adxr": round(adxr_val, 4),
                        "+di": round(plus_di, 4),
                        "-di": round(minus_di, 4)
                    },
                    "atr": round(atr, 4) if atr is not None else 0
                },
                "analysis": {
                    "market_state": market_state,
                    "action_signal": action_signal,
                    "modify_flag": modify_flag,
                    "long_direction": _normalize_dir_str(long_direction)
                },
                "trade_info": {
                    "last_trade_price": round(last_trade_price, 4),
                    "current_profit": round(current_profit, 2),
                    "trade_time": trade_time,
                    "hold_time": hold_time
                },
                "dual_period": {
                    "short_bar": short_bar,
                    "long_bar": long_bar,
                    "long_direction": _normalize_dir_str(long_direction)
                }
            }

        except Exception as e:
            error_msg = str(e)
            is_io_error = any(k in error_msg.lower() for k in ["closed file", "10035", "connection", "timeout", "ssl", "i/o"])
            if is_io_error and attempt < max_retries:
                wait_sec = 0.5 * (attempt + 1)
                log(f"{instId:15} | IO错误，{wait_sec}s后第{attempt + 2}次重试...")
                time.sleep(wait_sec)
                continue
            log(f"{instId:15} | 双周期获取失败: {error_msg}")
            raise

    return None


def _normalize_dir_str(v):
    """将方向值标准化为中文显示"""
    if v is None:
        return "--"
    s = str(v)
    if s == 'rise':
        return "上涨"
    if s == 'fall':
        return "下跌"
    return s


def calculate_strategy_data(bar="1H"):
    """
    遍历多个币种，调用真实的策略文件计算数据。

    注：本入口有意不套策略计算锁——它一次持锁可达几十秒（整轮所有币），
    会把实盘信号推后同样时长；逐币异步加载已取代它。取舍记录在
    task/strategy_gate.py 的「有意未覆盖的入口」一节。

    参数:
        bar: K线周期，如 "1H", "4H", "1D"
    """
    try:
        import pro3_singletimeframe as strategy_module
        strategy_module.PRINT_MARKET = 0
        strategy_module.PRINT_TRADE_OPS = 0
        strategy_module.PRINT_TRADE_RECORDS = 0
        strategy_module.FAST_MODE = True
        has_real_strategy = True
    except ImportError:
        has_real_strategy = False

    results = []
    up_count = 0
    down_count = 0
    wait_count = 0

    selected_coins = get_selected_coins()
    if not selected_coins:
        selected_coins = COIN_LIST[:3]

    log(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行策略扫描...")
    log(f"当前监控币种: {', '.join(selected_coins)}")
    log("-" * 70)

    for instId in selected_coins:
        coin_data = None

        for attempt in range(3):
            try:
                if not has_real_strategy:
                    raise Exception("未找到策略模块")

                from pro3_singletimeframe import get_latest_data

                latest_data, last_trade_data, atr = get_latest_data(instId, bar)

                price = latest_data.get('close', 0.0)
                macd_line = latest_data.get('DIF', 0.0)       # DIF 快线
                histogram = latest_data.get('MACD', 0.0)       # MACD 柱
                adx_val = latest_data.get('ADX', 0.0)
                adxr_val = latest_data.get('ADXR', 0.0)
                plus_di = latest_data.get('+DI', 0.0)
                minus_di = latest_data.get('-DI', 0.0)
                adx_prev = latest_data.get('ADX_PREV', adx_val)
                plus_di_prev = latest_data.get('+DI_PREV', plus_di)
                minus_di_prev = latest_data.get('-DI_PREV', minus_di)
                modify_flag = latest_data.get('MODIFY_FLAG', 'wait')

                last_trade_price = 0.0
                current_profit = 0.0
                trade_time = ""
                hold_time = ""
                if last_trade_data is not None:
                    last_trade_price = last_trade_data.get('TRADE_PRICE', 0.0)
                    if last_trade_price == 0.0:
                        last_trade_price = last_trade_data.get('PRICE', 0.0)
                    for key in ['TRADE_DATE', 'DATE', 'TRADE_TIME']:
                        if key in last_trade_data:
                            trade_time = last_trade_data[key]
                            break
                    # 无论是否有交易价格，只要有交易时间就计算持仓时长
                    if trade_time != "" and trade_time is not None:
                        try:
                            if isinstance(trade_time, datetime.datetime):
                                trade_dt = trade_time
                            elif isinstance(trade_time, str):
                                trade_dt = datetime.datetime.strptime(trade_time, "%Y-%m-%d %H:%M:%S")
                            else:
                                trade_dt = None
                            if trade_dt is not None:
                                now = datetime.datetime.now()
                                delta = now - trade_dt
                                days = delta.days
                                hours = delta.seconds // 3600
                                mins = (delta.seconds % 3600) // 60
                                if days > 0:
                                    hold_time = f"{days}天{hours}小时{mins}分"
                                else:
                                    hold_time = f"{hours}小时{mins}分"
                                trade_time = trade_dt.strftime("%Y-%#m-%#d %H:%M:%S")
                        except Exception:
                            hold_time = ""
                    if last_trade_price > 0:
                        if modify_flag == 'rise':
                            current_profit = ((price / last_trade_price) - 1) * 100 * 10.0
                        elif modify_flag == 'fall':
                            current_profit = (1 - (price / last_trade_price)) * 100 * 10.0

                log(f"{instId:15} | 现价: {price:<8.4f} | 交易价: {last_trade_price:<8.4f} | 盈亏: {current_profit:+.2f}%")

                if modify_flag == 'rise':
                    up_count += 1
                    action_signal = "上涨"
                elif modify_flag == 'fall':
                    down_count += 1
                    action_signal = "下跌"
                else:
                    wait_count += 1
                    action_signal = "观望"

                market_state = determine_market_state(
                    adx_val, adxr_val, adx_prev,
                    plus_di, minus_di, plus_di_prev, minus_di_prev
                )

                calc_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                coin_data = {
                    "timestamp": calc_time,
                    "symbol": instId,
                    "price": round(price, 4),
                    "indicators": {
                        "macd": {
                            "histogram": round(histogram, 4),
                            "dif": round(macd_line, 4)
                        },
                        "adx": {
                            "adx": round(adx_val, 4),
                            "adxr": round(adxr_val, 4),
                            "+di": round(plus_di, 4),
                            "-di": round(minus_di, 4)
                        }
                    },
                    "analysis": {
                        "market_state": market_state,
                        "action_signal": action_signal,
                        "modify_flag": modify_flag
                    },
                    "trade_info": {
                        "last_trade_price": round(last_trade_price, 4),
                        "current_profit": round(current_profit, 2),
                        "trade_time": trade_time,
                        "hold_time": hold_time
                    }
                }
                break  # 成功，跳出重试循环

            except Exception as e:
                error_msg = str(e)
                is_io_error = any(k in error_msg.lower() for k in ["closed file", "10035", "connection", "timeout", "ssl", "i/o"])
                if is_io_error and attempt < 2:
                    wait_sec = 0.5 * (attempt + 1)
                    log(f"{instId:15} | IO错误，{wait_sec}s后第{attempt + 2}次重试...")
                    time.sleep(wait_sec)
                    continue
                log(f"{instId:15} | 获取失败: {error_msg}")
                raise

        results.append(coin_data)

    # 计算综合行情
    total = len(selected_coins)
    if total == 0:
        total = 1
    if up_count > total * 0.6:
        overall_market = f"大盘整体向上 (多头: {up_count}, 空头: {down_count}, 观望: {wait_count})"
    elif down_count > total * 0.6:
        overall_market = f"大盘整体向下 (多头: {up_count}, 空头: {down_count}, 观望: {wait_count})"
    else:
        overall_market = f"大盘走势分化 / 震荡 (多头: {up_count}, 空头: {down_count}, 观望: {wait_count})"

    log("-" * 70)
    log(overall_market)
    log("=" * 70)

    return {
        "overall_market": overall_market,
        "coins": results
    }