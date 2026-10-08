# -*- coding: utf-8 -*-
"""
CSV 存储模块
============

负责 K 线数据在本地 CSV 文件中的所有读写操作：

  - 表头初始化            write_header_if_needed
  - 快速读取末尾时间戳    tail_last_timestamp
  - 加载全部已有时间戳    load_existing_timestamps
  - 按 timestamp 去重追加 append_rows_dedup
  - 无去重直接追加        append_rows（百万行回填专用，靠游标单调保证不重）
  - 分块统计行数/首根      csv_stats（避免 csv.reader 逐行解析大文件）
  - 两个有序文件流式归并    merge_ordered_csv（回填分段合回主文件，O(1) 内存）
  - 只读文件末尾 N 行     read_tail_rows（大文件展示用，避免整读）
  - 排序去重重写         sort_and_dedup_csv（历史补齐后恢复时间单调）
  - 从 CSV 读取完整行     load_rows

CSV 列结构固定为：
    timestamp, datetime, open, high, low, close, volume, exchange
其中 timestamp 为毫秒级 UTC 时间戳，作为唯一去重键。
"""

import csv
import datetime
import os
from typing import Dict, List, Optional, Set, Tuple

# CSV 表头（所有存储文件统一使用）
CSV_HEADER = [
    "timestamp", "datetime", "open", "high", "low", "close", "volume", "exchange"
]


def write_header_if_needed(path: str) -> None:
    """文件不存在或为空时写入统一表头。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)


def tail_last_timestamp(path: str) -> int:
    """
    读取文件末尾一段，快速定位「最后一行的 timestamp」（毫秒）。

    仅用于轻量续传判断；不做全量去重（全量请用 load_existing_timestamps）。
    文件不存在或无有效数据行时返回 None。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            offset = max(end - 8192, 0)
            f.seek(offset)
            chunk = f.read().decode("utf-8", errors="ignore")
            lines = [ln for ln in chunk.splitlines() if ln.strip()]
            for line in reversed(lines):
                parts = line.split(",")
                if parts:
                    try:
                        return int(parts[0])
                    except Exception:
                        continue
    except FileNotFoundError:
        return None
    return None


def load_existing_timestamps(path: str) -> Tuple[Set[int], List[int]]:
    """
    全量加载 CSV 中的 timestamp。

    返回:
        seen    : set，用于 O(1) 去重判断
        ordered : list，升序排列的全部 timestamp，用于缺口检测
    """
    seen: Set[int] = set()
    ordered: List[int] = []
    if not os.path.exists(path):
        return seen, ordered
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过表头
        for row in reader:
            if not row:
                continue
            try:
                ts = int(row[0])
            except Exception:
                continue
            if ts not in seen:
                seen.add(ts)
                ordered.append(ts)
    ordered.sort()
    return seen, ordered


def append_rows_dedup(
    path: str, exchange_id: str, rows: List[List], seen_ts: Set[int]
) -> int:
    """
    追加写入 K 线行，按 timestamp 去重（已存在则跳过）。

    参数:
        rows      : 每个元素为 ccxt 原始结构 [ts, open, high, low, close, volume]
        seen_ts   : 已存在时间戳集合，写入后会同步更新，供后续批次去重
    返回实际追加写入的行数。
    """
    appended = 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            ts = r[0]
            if ts in seen_ts:
                continue
            dt = datetime.datetime.fromtimestamp(
                ts / 1000, datetime.timezone.utc
            ).isoformat()
            w.writerow([ts, dt, r[1], r[2], r[3], r[4], r[5], exchange_id])
            seen_ts.add(ts)
            appended += 1
    return appended


def read_tail_rows(path: str, limit: int = 100) -> List[Dict]:
    """
    只读文件末尾一段，解析出最后 limit 行 K 线（按 timestamp 升序）。

    供「展示最新 N 根」这类只读场景使用：不做整文件解析，因此对
    百万行级 CSV 也只有一个常数级的读放大（每次多读 256 KiB 起）。
    行数不足 limit 时按实际行数返回；文件不存在/为空返回 []。
    """
    limit = max(1, int(limit))
    if not os.path.exists(path):
        return []
    block = 256 * 1024
    tail = b""
    remaining = 0
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            remaining = f.tell()
            # 反向按块读，直到拿满 limit 行换行分隔的数据（或读到文件开头）
            while remaining > 0 and tail.count(b"\n") <= limit:
                step = min(block, remaining)
                remaining -= step
                f.seek(remaining)
                tail = f.read(step) + tail
    except OSError:
        return []

    lines = tail.decode("utf-8", errors="ignore").splitlines()
    if remaining > 0 and lines:
        lines = lines[1:]      # 未读到文件开头时，首行是被截断的半行

    rows: List[Dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line or line.startswith("timestamp,"):
            continue
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            ts = int(parts[0])
        except ValueError:
            continue
        rows.append({
            "timestamp": ts,
            "datetime": parts[1],
            "open": _to_float(parts[2]),
            "high": _to_float(parts[3]),
            "low": _to_float(parts[4]),
            "close": _to_float(parts[5]),
            "volume": _to_float(parts[6]),
            "exchange": parts[7] if len(parts) > 7 else "",
        })
        if len(rows) >= limit:
            break
    rows.reverse()
    return rows


def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def append_rows(path: str, exchange_id: str, rows: List[List]) -> int:
    """
    直接追加写入（不做去重），返回写入行数。

    百万行级历史回填专用：append_rows_dedup 需要把一个 seen 集合挂在内存里，
    450 万根就是几百 MB；回填靠「游标只向前推进」天然不产生重复，因此这里
    不建集合。写入的 rows 必须自身时间升序，否则归并/续传锚点会错乱。
    """
    appended = 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            ts = r[0]
            dt = datetime.datetime.fromtimestamp(
                ts / 1000, datetime.timezone.utc
            ).isoformat()
            w.writerow([ts, dt, r[1], r[2], r[3], r[4], r[5], exchange_id])
            appended += 1
    return appended


def csv_stats(path: str) -> Tuple[int, Optional[int], float]:
    """
    分块扫描 CSV，返回 (数据行数, 首根 timestamp, 平均每行字节数)。

    不用 csv.reader 逐行解析：几百 MB 的文件下那个成本高一个量级，而展示
    进度只需要行数与首根。avg_bytes 供调用方做「只追加后估算行数」，避免
    大文件每轮整读。
    """
    if not os.path.exists(path):
        return 0, None, 0.0
    rows = 0
    first_ts = None
    total_bytes = 0
    block = 1024 * 1024
    with open(path, "rb") as f:
        prev_tail = b""
        while True:
            chunk = f.read(block)
            if not chunk:
                break
            total_bytes += len(chunk)
            buf = prev_tail + chunk
            lines = buf.split(b"\n")
            prev_tail = lines.pop()          # 末尾可能是半行，留到下一块拼接
            rows += len(lines)
            if first_ts is None:
                for ln in lines:
                    ln = ln.strip()
                    if not ln or ln.startswith(b"timestamp,"):
                        continue
                    try:
                        first_ts = int(ln.split(b",")[0])
                    except (ValueError, IndexError):
                        first_ts = None
                    break
        if prev_tail.strip():
            rows += 1                        # 文件末尾无换行符也算一行
    # 表头行计入 rows，减掉
    data_rows = max(0, rows - 1)
    avg = (total_bytes / rows) if rows else 0.0
    return data_rows, first_ts, avg


def merge_ordered_csv(primary: str, extra: str, remove_extra: bool = True) -> int:
    """
    把两个「时间升序」的 CSV 流式归并成一份（按 timestamp 去重，保留 primary 行）。

    为什么不用 sort_and_dedup_csv：那个函数把全文件读成 dict 再排序，450 万行
    要几 GB 内存；归并两个已有序的流只需 O(1) 内存。回填写入的是独立分段
    文件（自身递增），完成后用本函数合回主文件。

    写采「临时文件 + 原子替换」，中途异常不损坏主文件。返回合并后的行数。
    """
    if not os.path.exists(extra) or os.path.getsize(extra) == 0:
        return csv_stats(primary)[0]
    header = None
    out_tmp = primary + ".merging"
    written = 0
    last_ts = None

    def _iter_rows(path):
        with open(path, "r", newline="", encoding="utf-8") as fp:
            reader = csv.reader(fp)
            for row in reader:
                if not row:
                    continue
                try:
                    yield int(row[0]), row
                except ValueError:
                    # 表头行首列是 'timestamp'，转整数失败自然跳过；
                    # 不能无条件 next() 丢首行——没有表头的文件会少掉最早一根数据
                    continue

    with open(out_tmp, "w", newline="", encoding="utf-8") as fo:
        w = csv.writer(fo)
        a, b = _iter_rows(primary), _iter_rows(extra)
        ta, ra = next(a, (None, None))
        tb, rb = next(b, (None, None))
        while ta is not None or tb is not None:
            if tb is None or (ta is not None and ta < tb):
                ts, row = ta, ra
                ta, ra = next(a, (None, None))
            elif ta is None or tb < ta:
                ts, row = tb, rb
                tb, rb = next(b, (None, None))
            else:                        # 时间戳相等 → 保留主文件那份（已收盘口径一致）
                ts, row = ta, ra
                ta, ra = next(a, (None, None))
                tb, rb = next(b, (None, None))
            if header is None:
                header = CSV_HEADER
                w.writerow(header)
            if ts == last_ts:
                continue
            w.writerow(row)
            last_ts = ts
            written += 1
    os.replace(out_tmp, primary)
    if remove_extra:
        try:
            os.remove(extra)
        except OSError:
            pass
    return written


def sort_and_dedup_csv(path: str) -> int:
    """
    按 timestamp 升序去重重写 CSV，返回保留下来的行数。

    背景：「历史补齐」会把比现有首根更早的 K 线追写到文件末尾，此时
    tail_last_timestamp 读到的「末行」不再是全局最新一根，续传锚点会错乱。
    因此补齐后必须恢复时间单调；同时修正旧版去重逻辑可能留下的重复行。
    写采「临时文件 + 原子替换」，中途异常不会把原文件洗成半成品。

    注意：本函数把全文件读进 dict，只适合万行级的小文件整理；百万行级
    的历史回填请用 merge_ordered_csv（两段各自有序 → 流式归并）。
    """
    if not os.path.exists(path):
        return 0
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = {}
        for row in reader:
            if not row:
                continue
            try:
                ts = int(row[0])
            except ValueError:
                continue
            rows[ts] = row
    ordered = [rows[ts] for ts in sorted(rows)]

    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header or CSV_HEADER)
        w.writerows(ordered)
    os.replace(tmp, path)
    return len(ordered)


def scan_continuity(path: str, step_ms: int) -> Dict:
    """
    流式体检一个升序 CSV 的连续性，返回 {rows, first_ts, last_ts, gaps, dup_rows,
    missing_rows}。内存 O(1)，百万行级也能跑（区别于 load_existing_timestamps，
    那个会把全部时间戳读进 set + list）。

    前提：文件按 timestamp 升序（回填完成后均经过归并，满足该前提）。乱序
    时相邻时间差为负，会被计入 missing_rows（按 0 处理），不会报一堆假缺口。
    """
    out = {"rows": 0, "first_ts": None, "last_ts": None, "gaps": 0,
           "dup_rows": 0, "missing_rows": 0}
    if not os.path.exists(path):
        return out
    prev = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        header = next(f, None)
        if header and not header.startswith("timestamp"):
            f.seek(0)                 # 无表头的文件：第一行也是数据
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ts = int(line.split(",", 1)[0])
            except ValueError:
                continue
            out["rows"] += 1
            if out["first_ts"] is None:
                out["first_ts"] = ts
            out["last_ts"] = ts
            if prev is not None:
                delta = ts - prev
                if delta == 0:
                    out["dup_rows"] += 1
                elif delta > step_ms:
                    out["gaps"] += 1
                    out["missing_rows"] += int(delta // step_ms) - 1
            prev = ts
    return out


def load_rows(path: str) -> List[Dict]:
    """读取整个 CSV 为字典列表（供测试/校验使用）。"""
    rows: List[Dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows
