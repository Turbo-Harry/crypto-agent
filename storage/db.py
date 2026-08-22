"""
storage 层 — 全仓数据统一落库（SQLite，标准库，零新依赖）。

此前数据散落在多个 JSON 文件（trade_journal / experience_scored /
threshold_state / watchlist / positions_snapshot / arb_positions /
position_ownership），读写口径不一致、易漂移（见 pitfalls.md：
legacy 单位错位、状态文件被测试污染）。现统一收进一个数据库文件
`crypto_agent.db`，各业务模块只保留原接口，底层换库。

设计原则：
  - 每次操作独立短连接（WAL + busy_timeout + synchronous=NORMAL），
    天然线程安全，无需全局锁。
  - 写原语两档：x() 单条自动 commit；tx() 跨多条语句一个事务
    （全成或全不成）。不要在 tx() 块内再调 x()/q()——那些会另开连接。
  - 表结构与原 JSON dict 字段一一对应，模块内部零逻辑改动。
  - 首次启动自动把旧 JSON 导入（migrate_from_json），只导一次。
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

# 2026-08-23 双实例: CRYPTO_AGENT_DB 环境变量指定库文件(实盘 crypto_agent_live.db
# / 模拟盘 crypto_agent.db),两实例互不串库。默认保持历史行为。
DB_PATH = os.environ.get("CRYPTO_AGENT_DB") or "crypto_agent.db"

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
    fees_usdt REAL DEFAULT 0,          -- 2026-08-23 手续费(平仓复盘按账单写入)
    funding_usdt REAL DEFAULT 0,       -- 2026-08-23 资金费(持仓期间结算,同)
    shadow_dims TEXT,                  -- 2026-08-23 开仓时 6 维子分 JSON(权重进化证据)
    targets TEXT,                      -- 2026-08-23 目标价位带 T1/T2/T3 JSON
    forecast TEXT,                     -- 2026-08-23 开仓时预测 JSON(分布+触达概率)
    review TEXT, review_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, category TEXT, content TEXT,
    score REAL DEFAULT 50, adoptions INTEGER DEFAULT 0,
    good INTEGER DEFAULT 0, bad INTEGER DEFAULT 0,
    status TEXT DEFAULT 'unverified',
    source_trade TEXT,
    regime TEXT,                        -- Phase 4: 教训产生的市场环境标签(兼容旧数据)
    conditions TEXT DEFAULT '',         -- 2026-08-17 场景条件向量 JSON(direction/vol_band/trend/signal_type)
    hist_evidence TEXT DEFAULT '',      -- 2026-08-21 历史先验 JSON(同场景历史表现,只观测)
    share_key TEXT,                     -- 2026-08-23 经验共享: 内容身份哈希(跨库唯一)
    origin TEXT DEFAULT 'local',        -- 2026-08-23 经验共享: local=本实例产生, peer=对端镜像
    ts REAL, last_update REAL
);
CREATE INDEX IF NOT EXISTS idx_lessons_symbol ON lessons(symbol);
CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_share ON lessons(share_key);

-- 场景归纳教训(2026-08-17 用户要求'多维度经验总结'): 同 symbol+类别+场景
-- 条件的 trusted 教训 ≥ROLLUP_MIN_MEMBERS 时,日度沉淀一条归纳结论。
-- 归纳层只读汇总(不参与 ±10 验证循环,防'归纳验证归纳'回声)。
CREATE TABLE IF NOT EXISTS lesson_rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, category TEXT,
    conditions TEXT,             -- 场景条件向量 JSON(成员教训的公共条件)
    strength REAL DEFAULT 0,     -- 验证强度加权和
    member_count INTEGER DEFAULT 0,
    member_ids TEXT,             -- 成员教训 id JSON 数组
    share_key TEXT,              -- 2026-08-23 经验共享: 内容身份哈希
    origin TEXT DEFAULT 'local',
    ts REAL, last_update REAL
);
CREATE INDEX IF NOT EXISTS idx_rollups_key ON lesson_rollups(symbol, category);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rollups_share ON lesson_rollups(share_key);

-- 组合试验(2026-08-21 用户洞察"单条不盈利,combo 可能盈利"):
-- 每笔平仓实际采纳的教训组合(≥2 条)记一行,真实交易结果作验证样本。
-- 只观测;combo 统计达标走 experiments 提案,绝不自动改决策。
CREATE TABLE IF NOT EXISTS combo_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    signature TEXT,            -- 成员教训 id 升序拼接,组合身份
    member_ids TEXT,           -- JSON 数组
    pnl REAL,                  -- 本笔盈亏(比例)
    pnl_usdt REAL,             -- 本笔盈亏 USDT
    r_multiple REAL,
    ts REAL
);
CREATE INDEX IF NOT EXISTS idx_combo_sig ON combo_trials(signature);

-- 消息面情感快照(2026-08-23 用户要求'系统加消息面判断')
CREATE TABLE IF NOT EXISTS sentiment_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, composite REAL, fng_value REAL, news_score REAL, detail TEXT
);

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
    threshold REAL, decision TEXT,     -- open / open_failed / hold / cooldown / budget / reject
    reason TEXT                        -- 拒绝/放行原因
);
CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_decisions(ts);

CREATE TABLE IF NOT EXISTS engine_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, engine TEXT, error TEXT, traceback TEXT,
    archived INTEGER DEFAULT 0   -- 2026-08-20: 根因已修的行归档(保留证据,
                                 -- 但不占 H6 滚动窗口——否则修复后红灯挂 24h)
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

CREATE TABLE IF NOT EXISTS factor_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, name TEXT, rationale TEXT,     -- 经济逻辑必填(GP 产物标 hypothesis_only)
    n_samples INTEGER, n_folds INTEGER,
    mean_ic REAL, icir REAL, ic_tstat REAL, -- IC/ICIR/t 值(多重检验校正门槛 t>3.0)
    gross_spread REAL, turnover REAL, net_spread REAL,  -- 毛价差/换手/扣费净价差
    status TEXT,                            -- promote / watch / reject / redundant /
                                            -- hypothesis_only / reject_on_cost
    expression TEXT
);
CREATE INDEX IF NOT EXISTS idx_factor_trials_ts ON factor_trials(ts);

-- Phase 1 结构化特征采集（每笔交易一行，开仓写入入场特征、平仓更新离场特征）
-- 字段定义与来源见 docs/architecture/trade_features_schema.md
CREATE TABLE IF NOT EXISTS trade_features (
    trade_id TEXT PRIMARY KEY,
    entry_ts REAL, symbol TEXT, direction TEXT, venue TEXT,
    entry_price REAL, stop_loss REAL, take_profit REAL, atr REAL,
    signal_score REAL,                     -- 信号连续分(影子模式:只记录不进决策)
    regime_tag TEXT,                       -- trend / range / vol 三分位标签
    vol_pct REAL, trend_slope REAL, tf4h_spread REAL,
    of_imbalance REAL, of_taker_ratio REAL,          -- 订单流(币安镜像)
    of_oi_usd REAL, of_lsr_taker REAL,               -- 持仓/多空比(Gate.io)
    of_long_liq REAL, of_short_liq REAL,             -- 爆仓量
    exit_ts REAL, exit_price REAL, exit_reason TEXT,
    pnl REAL, r_multiple REAL, mfe_r REAL, mae_r REAL,
    holding_hours REAL, slippage_bps REAL, reversal INTEGER,
    features_missing TEXT                  -- 缺失字段清单(质量报告;生产目标=空串)
);
CREATE INDEX IF NOT EXISTS idx_tf_symbol_ts ON trade_features(symbol, entry_ts);

-- Phase 3 试验注册表（每次参数/规则变更提案必入账;多重检验与 PBO/DSR 证据）
CREATE TABLE IF NOT EXISTS ai_judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, base TEXT, direction TEXT, score REAL, entry_price REAL,
    verdict TEXT, reason TEXT, trade_id TEXT,
    outcome_pnl REAL, outcome_ts REAL
);

-- Agent Harness trace ledger.  These tables are append-oriented and contain
-- no order/exchange mutation capability; the policy kernel remains separate.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_ts REAL NOT NULL,
    completed_ts REAL,
    runtime_status TEXT NOT NULL,
    final_action TEXT NOT NULL,
    model_verdict TEXT,
    run_role TEXT NOT NULL DEFAULT 'champion',
    parent_run_id TEXT,
    prompt_version TEXT,
    model_version TEXT,
    context_version TEXT,
    schema_version TEXT,
    retrieval_version TEXT,
    input_hash TEXT,
    response_hash TEXT,
    latency_ms INTEGER,
    model_latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    error_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_signal ON agent_runs(signal_id, created_ts);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(runtime_status, created_ts);

CREATE TABLE IF NOT EXISTS agent_steps (
    run_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_ts REAL NOT NULL,
    finished_ts REAL,
    tool_name TEXT,
    input_hash TEXT,
    output_hash TEXT,
    evidence_ids TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    fallback_action TEXT,
    PRIMARY KEY (run_id, step_no)
);

CREATE TABLE IF NOT EXISTS agent_evaluations (
    run_id TEXT PRIMARY KEY,
    lifecycle_status TEXT NOT NULL DEFAULT 'pending',
    label TEXT,
    settle_ts REAL,
    tp_first INTEGER,
    sl_first INTEGER,
    timeout INTEGER,
    ambiguous INTEGER,
    pnl_r REAL,
    mfe_r REAL,
    mae_r REAL,
    incremental_ev REAL,
    saved_loss REAL,
    missed_profit REAL,
    evaluation_version TEXT,
    label_source TEXT
);

CREATE TABLE IF NOT EXISTS forecast_calibration (
    trade_id TEXT PRIMARY KEY,
    ts REAL, p_hit_tp REAL, p_hit_sl REAL,
    hit_tp INTEGER, hit_sl INTEGER, pnl REAL
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, change_id TEXT, kind TEXT, params TEXT,
    n_samples INTEGER, dsr REAL, pbo REAL,
    status TEXT,                -- proposed / insufficient_data / accepted /
                                -- rejected / applied / rolled_back
    decided_by TEXT, notes TEXT
);

-- Phase 4 策略 B 影子信号（突破/动量确认;只记录假设性交易,绝不下单——
-- 影子政策: 与策略 A 的真实样本分表对照,验证通过前不进决策）
CREATE TABLE IF NOT EXISTS shadow_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, base TEXT, strategy TEXT, dir TEXT,
    entry REAL, stop REAL, tp REAL, atr REAL,
    signal_score REAL, regime_tag TEXT,
    kline_ts INTEGER, status TEXT DEFAULT 'hypothetical',
    pnl REAL,                   -- 结算后的假设盈亏比例（扫描影子 A_wick）
    exit_reason TEXT,           -- stop / tp / timeout
    settled_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_signals(ts);
CREATE INDEX IF NOT EXISTS idx_shadow_base_strategy_kline
    ON shadow_signals(base, strategy, kline_ts);

-- 下单失败结构化日志(2026-08-16 用户问"有没有下单失败的日志"——此前只有
-- stdout 文本,无法查询/告警。每次下单/挂单失败必入账,含预检拒绝)
CREATE TABLE IF NOT EXISTS order_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, base TEXT, inst_id TEXT, side TEXT, qty REAL,
    stage TEXT,              -- open / close / stop_order / tp_order / preflight
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_of_ts ON order_failures(ts);

-- 告警信箱(2026-08-16 会话值守循环用): 体检失败入箱,值守轮处理并标记 resolved
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, source TEXT, items TEXT,
    status TEXT DEFAULT 'new',        -- new / resolved
    resolved_ts REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);

-- 统一异常中心(2026-08-17 用户要求:所有异常统一输出到一个接口,不要分散)
-- 生产者: health_check 失败 / 下单失败 / 引擎异常 / 风控熔断 / 其他
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, source TEXT,          -- health / order_failure / engine_error / risk / other
    severity TEXT,                 -- critical / error / warning
    title TEXT, detail TEXT,
    status TEXT DEFAULT 'new'      -- new / resolved
);
-- 旧单列索引名改过,CREATE INDEX IF NOT EXISTS 不会删旧名;每次 init_db
-- 都 DROP,避免「user_version 已升到最新、但旧进程又把旧索引建回来」。
DROP INDEX IF EXISTS idx_anom_status;
CREATE INDEX IF NOT EXISTS idx_anom_source_status ON anomalies(source, status);

-- 沙盘不可交易符号(2026-08-17): 开仓失败 51001(无合约)/51087(已退市) 自动登记,
-- 与 config.DEMO_UNTRADABLE 合并做预检,避免同符号每轮扫描反复下单失败。
CREATE TABLE IF NOT EXISTS untradable_symbols (
    base TEXT PRIMARY KEY,
    reason TEXT,
    ts REAL
);

-- 未触发信号复盘(2026-08-17 用户建议): 每轮 no_signal 记录四环节条件画像,
-- 回答"为什么没触发"——瓶颈在趋势/触线/影线/量能哪一环,近失(差一点)多少
CREATE TABLE IF NOT EXISTS signal_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, base TEXT,
    trend_up INTEGER, trend_down INTEGER,
    touch_long INTEGER, touch_short INTEGER,
    wick_long INTEGER, wick_short INTEGER,
    vol_ratio REAL,                 -- 当前量/近20均量
    bottleneck TEXT,                -- trend / touch / wick / vol / none
    near_miss INTEGER               -- 仅影线差一点(>=0.8×门槛)等近失标记
);
CREATE INDEX IF NOT EXISTS idx_sp_ts ON signal_profiles(ts);
"""

_lock = threading.Lock()          # 只保护建表/迁移（连接本身每次操作独立）
_initialized = False


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # WAL + synchronous=NORMAL 是 SQLite 官方推荐组合：checkpoint 仍会
    # fsync，已提交事务在进程/系统崩溃后不丢；FULL(2) 在 WAL 下只多保护
    # checkpoint 过程中的 OS 崩溃，写入更慢且对本仓短连接无额外收益。
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _user_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn, version):
    # PRAGMA user_version 必须吃整数字面量,不能用 ? 绑定。
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(conn, table, column, decl):
    """探测后再 ALTER;列已在则跳过。duplicate column 异常也吞掉(幂等容错)。"""
    if column in _table_columns(conn, table):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _migrate_v1_lessons_columns(conn):
    """v1: lessons 补 regime / conditions 列(旧库 CREATE TABLE IF NOT EXISTS 不会改表)。"""
    _add_column_if_missing(conn, "lessons", "regime", "TEXT")
    _add_column_if_missing(conn, "lessons", "conditions", "TEXT DEFAULT ''")


def _migrate_v2_indexes(conn):
    """v2: 索引对齐真实查询。老库里的 idx_anom_status 不会因改 SCHEMA 而消失,必须显式 DROP。"""
    conn.execute("DROP INDEX IF EXISTS idx_anom_status")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_anom_source_status "
        "ON anomalies(source, status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_base_strategy_kline "
        "ON shadow_signals(base, strategy, kline_ts)")


def _migrate_v3_shadow_settle(conn):
    """v3: 扫描影子可结算——旧库 CREATE TABLE IF NOT EXISTS 不会加列。"""
    _add_column_if_missing(conn, "shadow_signals", "pnl", "REAL")
    _add_column_if_missing(conn, "shadow_signals", "exit_reason", "TEXT")
    _add_column_if_missing(conn, "shadow_signals", "settled_ts", "REAL")


def _migrate_v4_engine_errors_archived(conn):
    """v4: 引擎错误归档标记(2026-08-20)——根因已修的行不占 H6 滚动窗口。"""
    _add_column_if_missing(conn, "engine_errors", "archived", "INTEGER DEFAULT 0")


def _migrate_v5_lesson_hist_evidence(conn):
    """v5: 教训历史先验(2026-08-21 用户要求'经验从历史看是否有符合的')。"""
    _add_column_if_missing(conn, "lessons", "hist_evidence", "TEXT")


def _migrate_v6_experience_sharing(conn):
    """v6: 经验共享(2026-08-23 用户指示'经验共享'——双实例教训互同步):
    lessons/lesson_rollups 加 share_key(内容哈希,跨库唯一身份)与
    origin(local/peer),并给存量行回填 share_key。"""
    import hashlib as _h
    for table in ("lessons", "lesson_rollups"):
        _add_column_if_missing(conn, table, "share_key", "TEXT")
        _add_column_if_missing(conn, table, "origin", "TEXT DEFAULT 'local'")
    # 存量行回填 share_key(内容身份,与 id 无关——两库 id 会撞车)
    try:
        rows = conn.execute(
            "SELECT id, symbol, category, content, source_trade FROM lessons "
            "WHERE share_key IS NULL").fetchall()
        for r in rows:
            key = _h.sha1("|".join(
                str(r[i] or "") for i in range(len(r))).encode("utf-8")).hexdigest()[:16]
            conn.execute("UPDATE lessons SET share_key=? WHERE id=?",
                         [f"l-{key}", r[0]])
        rrows = conn.execute(
            "SELECT id, symbol, category, conditions FROM lesson_rollups "
            "WHERE share_key IS NULL").fetchall()
        for r in rrows:
            key = _h.sha1("|".join(
                str(r[i] or "") for i in range(len(r))).encode("utf-8")).hexdigest()[:16]
            conn.execute("UPDATE lesson_rollups SET share_key=? WHERE id=?",
                         [f"r-{key}", r[0]])
        conn.commit()
    except Exception:
        pass
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_share "
                 "ON lessons(share_key)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_rollups_share "
                 "ON lesson_rollups(share_key)")


def _migrate_v7_trade_fees(conn):
    """v7: 交易费/资金费落账(2026-08-23 用户问'会计算费率和手续费吗')——
    trades 加 fees_usdt(手续费)/funding_usdt(资金费),平仓复盘时按账户账单
    实际值写入,净盈亏 = pnl_usdt - fees - funding。"""
    _add_column_if_missing(conn, "trades", "fees_usdt", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "trades", "funding_usdt", "REAL DEFAULT 0")


def _migrate_v8_shadow_dims(conn):
    """v8: 权重进化证据(2026-08-23 用户问'会按历史经验调整权重吗')——
    trades 加 shadow_dims(开仓时 6 维子分 JSON),平仓后与盈亏算 IC。"""
    _add_column_if_missing(conn, "trades", "shadow_dims", "TEXT")


def _migrate_v9_trade_targets(conn):
    """v9: 目标价位带(2026-08-23 用户问'会预测会升到什么价位吗')——
    trades 加 targets(T1/T2/T3 JSON,开仓时计算)。"""
    _add_column_if_missing(conn, "trades", "targets", "TEXT")


def _migrate_v10_forecast(conn):
    """v10: 预测机制(2026-08-23 用户要求'最好能有预测机制')——
    trades 加 forecast(开仓时预测 JSON);建 forecast_calibration(平仓校准)。"""
    _add_column_if_missing(conn, "trades", "forecast", "TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS forecast_calibration ("
                 "trade_id TEXT PRIMARY KEY, ts REAL, "
                 "p_hit_tp REAL, p_hit_sl REAL, hit_tp INTEGER, hit_sl INTEGER, "
                 "pnl REAL)")


def _migrate_v11_ai_memory(conn):
    """v11: AI 记忆(2026-08-23 用户问'AI会学习历史经验吗')——
    ai_judgments 表存每次把关判断+后续结果,下次判断时作为案例回喂 AI。"""
    conn.execute("CREATE TABLE IF NOT EXISTS ai_judgments ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                 "base TEXT, direction TEXT, score REAL, entry_price REAL, "
                 "verdict TEXT, reason TEXT, trade_id TEXT, "
                 "outcome_pnl REAL, outcome_ts REAL)")


def _migrate_v12_agent_harness(conn):
    """v12: durable Agent Harness runs, steps and mature evaluations."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE, created_ts REAL NOT NULL,
        completed_ts REAL, runtime_status TEXT NOT NULL,
        final_action TEXT NOT NULL, model_verdict TEXT,
        run_role TEXT NOT NULL DEFAULT 'champion', parent_run_id TEXT,
        prompt_version TEXT, model_version TEXT, context_version TEXT,
        schema_version TEXT, retrieval_version TEXT, input_hash TEXT,
        response_hash TEXT, latency_ms INTEGER, model_latency_ms INTEGER,
        input_tokens INTEGER, output_tokens INTEGER, estimated_cost REAL,
        error_type TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_agent_runs_signal ON agent_runs(signal_id, created_ts);
    CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(runtime_status, created_ts);
    CREATE TABLE IF NOT EXISTS agent_steps (
        run_id TEXT NOT NULL, step_no INTEGER NOT NULL, step_type TEXT NOT NULL,
        status TEXT NOT NULL, started_ts REAL NOT NULL, finished_ts REAL,
        tool_name TEXT, input_hash TEXT, output_hash TEXT, evidence_ids TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0, error_type TEXT,
        fallback_action TEXT, PRIMARY KEY (run_id, step_no)
    );
    CREATE TABLE IF NOT EXISTS agent_evaluations (
        run_id TEXT PRIMARY KEY, lifecycle_status TEXT NOT NULL DEFAULT 'pending',
        label TEXT, settle_ts REAL, tp_first INTEGER, sl_first INTEGER,
        timeout INTEGER, ambiguous INTEGER, pnl_r REAL, mfe_r REAL, mae_r REAL,
        incremental_ev REAL, saved_loss REAL, missed_profit REAL,
        evaluation_version TEXT, label_source TEXT
    );
    """)


# 版本号 → 迁移函数。只追加,不改已落地版本的语义。
MIGRATIONS = (
    (1, _migrate_v1_lessons_columns),
    (2, _migrate_v2_indexes),
    (3, _migrate_v3_shadow_settle),
    (4, _migrate_v4_engine_errors_archived),
    (5, _migrate_v5_lesson_hist_evidence),
    (6, _migrate_v6_experience_sharing),
    (7, _migrate_v7_trade_fees),
    (8, _migrate_v8_shadow_dims),
    (9, _migrate_v9_trade_targets),
    (10, _migrate_v10_forecast),
    (11, _migrate_v11_ai_memory),
    (12, _migrate_v12_agent_harness),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def _run_migrations(conn):
    """按 user_version 顺序执行未跑过的迁移,每步递增版本号。"""
    current = _user_version(conn)
    for version, fn in MIGRATIONS:
        if current < version:
            fn(conn)
            _set_user_version(conn, version)
            current = version


def init_db(db_path=None):
    """建表 + schema 迁移 + 一次性 JSON 迁移（幂等）。

    全新库: executescript(SCHEMA) 已建满列和现行索引,直接把 user_version
    设到 SCHEMA_VERSION,跳过逐步 ALTER。
    老库(user_version=0 但表已在): 按 MIGRATIONS 顺序补列/换索引;列已存在
    则跳过 ALTER,不报 duplicate column。
    """
    global _initialized
    with _lock:
        conn = _connect(db_path)
        try:
            is_fresh = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
            ).fetchone() is None
            try:
                conn.executescript(SCHEMA)
            except sqlite3.OperationalError as e:
                # 老库: SCHEMA 里引用了仅迁移才加的列(如 lessons.share_key 的
                # 唯一索引),executescript 会报 no such column——吞掉,交给
                # _run_migrations 补列+补索引(2026-08-23 v6 经验共享)。
                if "no such column" not in str(e).lower():
                    raise
            if is_fresh:
                _set_user_version(conn, SCHEMA_VERSION)
            else:
                _run_migrations(conn)
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
               "pnl", "status", "fees_usdt", "funding_usdt", "shadow_dims",
               "targets", "forecast", "review", "review_ts")


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
    """单条写（INSERT/UPDATE/DELETE），自动 commit，返回 lastrowid。

    一条语句就够时用本函数。多条写必须原子（全成或全不成）时改用 tx()。
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@contextmanager
def tx(db_path=None):
    """跨多条语句的事务。yield 一条已设 WAL/busy_timeout/row_factory 的连接。

    何时用 x()：单条 INSERT/UPDATE/DELETE，自动 commit。
    何时用 tx()：多条写必须全成或全不成（例如 watchlist 先 DELETE 再
    逐条 INSERT；一轮仓位快照的多行 INSERT）。

    正常退出 commit；异常 rollback 后重新抛出；finally close。
    调用方在 with 块内用 conn.execute(...)，不要自行 commit/close，
    也不要在块内再调 x()/q()（那些会另开连接，看不到未提交变更）。
    """
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 只增不删的流水表:按 ts 清理。台账/经验/研究资产不在此列,永久保留。
_PRUNE_TS_TABLES = (
    "scan_decisions",
    "position_snapshots",
    "signal_profiles",
    "engine_errors",
    "shadow_signals",
    "order_failures",
)
# 永不清理(prune_old_rows 禁止 DELETE): trades / lessons / lesson_rollups /
# trade_features / experiments / factor_trials / thresholds / watchlist /
# ownership / untradable_symbols / kv。


def prune_old_rows(db_path=None):
    """删除早于 DB_RETENTION_DAYS 的流水行。返回 {表名: 删除行数}。

    未处理(status='new')的 alerts/anomalies 即使过期也保留。
    整个清理包在一个 tx() 里;提交后再 PRAGMA optimize(比 VACUUM 轻,不锁库)。
    """
    import config
    init_db(db_path)
    cutoff = time.time() - config.DB_RETENTION_DAYS * 86400
    deleted = {}
    with tx(db_path=db_path) as conn:
        for table in _PRUNE_TS_TABLES:
            before = conn.total_changes
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", [cutoff])
            deleted[table] = conn.total_changes - before
        before = conn.total_changes
        conn.execute(
            "DELETE FROM analyses WHERE kind='daily' AND ts < ?", [cutoff])
        deleted["analyses"] = conn.total_changes - before
        before = conn.total_changes
        conn.execute(
            "DELETE FROM alerts WHERE status='resolved' "
            "AND COALESCE(resolved_ts, ts) < ?", [cutoff])
        deleted["alerts"] = conn.total_changes - before
        before = conn.total_changes
        conn.execute(
            "DELETE FROM anomalies WHERE status='resolved' AND ts < ?",
            [cutoff])
        deleted["anomalies"] = conn.total_changes - before
    opt = _connect(db_path)
    try:
        opt.execute("PRAGMA optimize")
    finally:
        opt.close()
    return deleted


if __name__ == "__main__":
    init_db()
    print("✅ crypto_agent.db 建表 + 迁移完成")
    print("trades:", q1("SELECT COUNT(*) c FROM trades")["c"])
    print("lessons:", q1("SELECT COUNT(*) c FROM lessons")["c"])
    print("watchlist:", q1("SELECT COUNT(*) c FROM watchlist")["c"])
