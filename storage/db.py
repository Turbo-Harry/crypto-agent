"""
storage 层 — 全仓数据统一落库（SQLite，标准库，零新依赖）。

此前数据散落在多个 JSON 文件（trade_journal / experience_scored /
threshold_state / watchlist / positions_snapshot / arb_positions /
position_ownership），读写口径不一致、易漂移（见 pitfalls.md：
legacy 单位错位、状态文件被测试污染）。现统一收进一个数据库文件
`crypto_agent.db`，各业务模块只保留原接口，底层换库。

设计原则：
  - 每次操作独立短连接（WAL + busy_timeout），天然线程安全，无需全局锁。
  - 表结构与原 JSON dict 字段一一对应，模块内部零逻辑改动。
  - 首次启动自动把旧 JSON 导入（migrate_from_json），只导一次。
"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = "crypto_agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal TEXT, reason TEXT,
    entry_price REAL, stop_loss REAL, take_profit REAL,
    size REAL, size_unit TEXT DEFAULT 'base',
    direction TEXT DEFAULT 'long', venue TEXT DEFAULT 'swap',
    score REAL,
    adopted_lesson_ids TEXT DEFAULT '[]',
    atr_value REAL, signal_price REAL,
    notional_usdt REAL, risk_usdt REAL,
    entry_time REAL,
    exit_price REAL, exit_time REAL, exit_reason TEXT,
    pnl REAL, status TEXT DEFAULT 'open',
    review TEXT, review_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, category TEXT, content TEXT,
    score REAL DEFAULT 50, adoptions INTEGER DEFAULT 0,
    good INTEGER DEFAULT 0, bad INTEGER DEFAULT 0,
    status TEXT DEFAULT 'unverified',
    source_trade TEXT,
    ts REAL, last_update REAL
);
CREATE INDEX IF NOT EXISTS idx_lessons_symbol ON lessons(symbol);
CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status);

CREATE TABLE IF NOT EXISTS thresholds (
    key TEXT PRIMARY KEY,          -- "dir" / "arb"
    threshold REAL, records TEXT,  -- records: JSON 数组（score→pnl 样本）
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS watchlist (
    date TEXT, base TEXT, inst_id TEXT,
    dir INTEGER, score REAL, trend_dev REAL, atr_pct REAL,
    price REAL, is_stock INTEGER DEFAULT 0,
    PRIMARY KEY (date, base)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, inst_id TEXT, side TEXT,
    contracts REAL, base_qty REAL, avg_px REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON position_snapshots(ts);

CREATE TABLE IF NOT EXISTS arb_positions (
    base TEXT PRIMARY KEY, rec TEXT, updated_at REAL
);

CREATE TABLE IF NOT EXISTS ownership (
    key TEXT PRIMARY KEY, qty REAL, notional REAL,
    strategies TEXT, updated_at REAL
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, kind TEXT,            -- halt / recovery
    reason TEXT, equity REAL,
    open_trades INTEGER
);
CREATE INDEX IF NOT EXISTS idx_risk_ts ON risk_events(ts);

CREATE TABLE IF NOT EXISTS scan_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, base TEXT, venue TEXT,
    has_signal INTEGER DEFAULT 0,      -- 是否出回踩确认信号
    direction TEXT,                    -- long/short/None
    threshold REAL, decision TEXT,     -- open / hold / cooldown / budget / reject
    reason TEXT                        -- 拒绝/放行原因
);
CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_decisions(ts);

CREATE TABLE IF NOT EXISTS engine_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, engine TEXT, error TEXT, traceback TEXT
);
CREATE INDEX IF NOT EXISTS idx_errors_ts ON engine_errors(ts);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, kind TEXT,                -- daily / manual
    report TEXT,                       -- JSON 报告
    issues TEXT                        -- JSON 感知到的问题列表
);
CREATE INDEX IF NOT EXISTS idx_analyses_ts ON analyses(ts);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT, updated_at REAL
);
"""

_lock = threading.Lock()          # 只保护建表/迁移（连接本身每次操作独立）
_initialized = False


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=None):
    """建表 + 一次性 JSON 迁移（幂等）。"""
    global _initialized
    with _lock:
        conn = _connect(db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        if not _initialized:
            _initialized = True
            migrate_from_json(db_path)
    return db_path or DB_PATH


def migrate_from_json(db_path=None):
    """把旧 JSON 文件一次性导入（表空才导；kv 表存 paper_state/factor_top）。"""
    conn = _connect(db_path)
    try:
        # 1. trade_journal.json → trades + 旧 lessons 归入 kv 保留
        if os.path.exists("trade_journal.json") and not conn.execute(
                "SELECT COUNT(*) c FROM trades").fetchone()["c"]:
            with open("trade_journal.json") as f:
                d = json.load(f)
            for t in d.get("trades", []):
                cols = [k for k in t if k in _TRADE_COLS]
                conn.execute(
                    f"INSERT OR REPLACE INTO trades ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [t[k] for k in cols])
            if d.get("lessons"):
                conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)",
                             ("legacy_journal_lessons", json.dumps(d["lessons"]), time.time()))
            conn.commit()
            print(f"  迁移: trade_journal.json → trades 表（{len(d.get('trades', []))} 笔）")
        # 2. experience_scored.json → lessons
        if os.path.exists("experience_scored.json") and not conn.execute(
                "SELECT COUNT(*) c FROM lessons").fetchone()["c"]:
            with open("experience_scored.json") as f:
                rows = json.load(f)
            for l in rows:
                conn.execute(
                    "INSERT INTO lessons (id,symbol,category,content,score,adoptions,"
                    "good,bad,status,source_trade,ts,last_update) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [l.get("id"), l.get("symbol"), l.get("category"), l.get("content"),
                     l.get("score", 50), l.get("adoptions", 0), l.get("good", 0),
                     l.get("bad", 0), l.get("status", "unverified"), l.get("source_trade"),
                     l.get("ts"), l.get("last_update")])
            conn.commit()
            print(f"  迁移: experience_scored.json → lessons 表（{len(rows)} 条）")
        # 3. watchlist.json → watchlist
        if os.path.exists("watchlist.json") and not conn.execute(
                "SELECT COUNT(*) c FROM watchlist").fetchone()["c"]:
            with open("watchlist.json") as f:
                d = json.load(f)
            for c in d.get("candidates", []):
                conn.execute(
                    "INSERT OR REPLACE INTO watchlist (date,base,inst_id,dir,score,"
                    "trend_dev,atr_pct,price,is_stock) VALUES (?,?,?,?,?,?,?,?,?)",
                    [d.get("date", ""), c.get("base"), c.get("instId"), c.get("dir", 0),
                     c.get("score", 0), c.get("trend_dev", 0), c.get("atr_pct", 0),
                     c.get("price", 0), 1 if c.get("is_stock") else 0])
            conn.commit()
            print(f"  迁移: watchlist.json → watchlist 表（{len(d.get('candidates', []))} 个）")
        # 4. arb_positions.json → arb_positions
        if os.path.exists("arb_positions.json") and not conn.execute(
                "SELECT COUNT(*) c FROM arb_positions").fetchone()["c"]:
            with open("arb_positions.json") as f:
                rows = json.load(f)
            for r in rows:
                if isinstance(r, dict) and r.get("base"):
                    conn.execute("INSERT OR REPLACE INTO arb_positions (base,rec,updated_at) VALUES (?,?,?)",
                                 [r["base"], json.dumps(r), time.time()])
            conn.commit()
            print(f"  迁移: arb_positions.json → arb_positions 表（{len(rows)} 条）")
        # 5. position_ownership.json → ownership
        if os.path.exists("position_ownership.json") and not conn.execute(
                "SELECT COUNT(*) c FROM ownership").fetchone()["c"]:
            with open("position_ownership.json") as f:
                d = json.load(f)
            for k, rec in d.items():
                if isinstance(rec, dict):
                    conn.execute(
                        "INSERT OR REPLACE INTO ownership (key,qty,notional,strategies,updated_at) "
                        "VALUES (?,?,?,?,?)",
                        [k, rec.get("qty", 0), rec.get("notional", 0),
                         json.dumps(rec.get("strategies", {})), rec.get("updated_at", time.time())])
            conn.commit()
            print(f"  迁移: position_ownership.json → ownership 表（{len(d)} 条）")
        # 6. 阈值状态 → thresholds
        for key, fname in (("dir", "threshold_state_dir.json"), ("arb", "threshold_state_arb.json")):
            if os.path.exists(fname) and not conn.execute(
                    "SELECT COUNT(*) c FROM thresholds WHERE key=?", [key]).fetchone()["c"]:
                with open(fname) as f:
                    d = json.load(f)
                conn.execute(
                    "INSERT OR REPLACE INTO thresholds (key,threshold,records,updated_at) VALUES (?,?,?,?)",
                    [key, d.get("threshold", 70), json.dumps(d.get("records", [])), time.time()])
                conn.commit()
                print(f"  迁移: {fname} → thresholds[{key}]")
    finally:
        conn.close()


_TRADE_COLS = ("id", "symbol", "signal", "reason", "entry_price", "stop_loss",
               "take_profit", "size", "size_unit", "direction", "venue", "score",
               "adopted_lesson_ids", "atr_value", "signal_price", "notional_usdt",
               "risk_usdt", "entry_time", "exit_price", "exit_time", "exit_reason",
               "pnl", "status", "review", "review_ts")


def q(sql, params=(), db_path=None):
    """查询多行（返回 dict 列表）。"""
    conn = _connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(sql, params)]
        return rows
    finally:
        conn.close()


def q1(sql, params=(), db_path=None):
    """查询一行（无则 None）。"""
    rows = q(sql, params, db_path)
    return rows[0] if rows else None


def x(sql, params=(), db_path=None):
    """执行写操作（INSERT/UPDATE/DELETE），返回 lastrowid。"""
    conn = _connect(db_path)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("✅ crypto_agent.db 建表 + 迁移完成")
    print("trades:", q1("SELECT COUNT(*) c FROM trades")["c"])
    print("lessons:", q1("SELECT COUNT(*) c FROM lessons")["c"])
    print("watchlist:", q1("SELECT COUNT(*) c FROM watchlist")["c"])
