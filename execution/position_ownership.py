"""
持仓所有权账本（R1-12）——跨进程/跨策略的持仓归属与组合总敞口控制。

解决：同 symbol 同 posSide 的合约持仓在交易所合并后，无法区分"哪部分是套利腿、
哪部分是方向仓"。本账本以 (symbol+posSide) 为 key，claim/release 本策略持有数量；
组合总合约敞口 ≤ max_total_notional（600 USDT）在 claim 时强制。

并发安全：独立锁文件 flock + 原子写（.tmp → os.replace）。
"""
import json
import os
import time

try:
    import fcntl
except ImportError:
    fcntl = None


class PositionLedger:
    def __init__(self, path="position_ownership.json",
                 lock_path="position_ownership.lock",
                 max_total_notional=600.0):
        # 存储：SQLite（storage 层 ownership 表，事务保证并发安全，替代 flock）
        self.path = path
        self.lock_path = lock_path
        self.max_total_notional = max_total_notional
        self.db_path = None if path == "position_ownership.json" else path
        self._data = self._load()

    def _load(self):
        import storage.db as sdb
        sdb.init_db(self.db_path)
        rows = sdb.q("SELECT * FROM ownership", db_path=self.db_path)
        out = {}
        for r in rows:
            out[r["key"]] = {"qty": r["qty"], "notional": r["notional"],
                             "strategies": json.loads(r["strategies"] or "{}"),
                             "updated_at": r["updated_at"]}
        return out

    def _locked(self, fn):
        # SQLite 事务本身即并发安全（WAL + busy_timeout），保留此接口兼容旧调用
        return fn()

    def _save(self):
        import storage.db as sdb
        for key, rec in self._data.items():
            sdb.x("INSERT OR REPLACE INTO ownership (key,qty,notional,strategies,updated_at) "
                  "VALUES (?,?,?,?,?)",
                  [key, rec.get("qty", 0.0), rec.get("notional", 0.0),
                   json.dumps(rec.get("strategies", {})), rec.get("updated_at", time.time())],
                  db_path=self.db_path)

    # ---------- 组合总敞口 ----------
    def total_notional(self):
        return sum(rec.get("notional", 0) for rec in self._data.values())

    # ---------- claim / release ----------
    def claim(self, symbol, pos_side, strategy, qty, notional):
        """开仓前认领：组合总敞口超限则拒绝。返回 (ok, reason)。"""
        key = f"{symbol}:{pos_side}"
        result = {}

        def _do():
            if self.total_notional() + notional > self.max_total_notional:
                result["r"] = (False,
                               f"组合总敞口 {self.total_notional():.0f}+{notional:.0f} "
                               f"> {self.max_total_notional:.0f} 上限")
                return
            rec = self._data.setdefault(key, {"qty": 0.0, "notional": 0.0,
                                              "strategies": {}})
            rec["qty"] += qty
            rec["notional"] += notional
            rec["strategies"][strategy] = rec["strategies"].get(strategy, 0.0) + qty
            rec["updated_at"] = time.time()
            self._save()
            result["r"] = (True, "")

        self._locked(_do)
        return result.get("r", (False, "账本异常"))

    def release(self, symbol, pos_side, strategy, qty, notional=None):
        """平仓后释放。qty 为本策略持有数量；notional 缺失时按比例释放。"""
        key = f"{symbol}:{pos_side}"

        def _do():
            rec = self._data.get(key)
            if not rec:
                return
            release_notional = notional
            if release_notional is None and rec["notional"] > 0 and rec["qty"] > 0:
                release_notional = rec["notional"] * (qty / rec["qty"])
            rec["qty"] = max(0.0, rec["qty"] - qty)
            rec["notional"] = max(0.0, rec["notional"] - (release_notional or 0.0))
            s_qty = rec["strategies"].get(strategy, 0.0)
            rec["strategies"][strategy] = max(0.0, s_qty - qty)
            if rec["qty"] <= 0:
                # 审计 H2:归零必须物理 DELETE,否则重启后 _load 把幽灵持仓读回
                self._data.pop(key, None)
                import storage.db as sdb
                sdb.x("DELETE FROM ownership WHERE key=?", [key], db_path=self.db_path)
            else:
                rec["updated_at"] = time.time()
            self._save()

        self._locked(_do)

    def force_release(self, key):
        """物理删除一条 claim(对账用)。返回被释放的 key 或 None。"""
        import storage.db as sdb
        sdb.x("DELETE FROM ownership WHERE key=?", [key], db_path=self.db_path)
        if key in self._data:
            self._data.pop(key, None)
            return key
        return None

    def restore(self, symbol, pos_side, strategy, qty, notional):
        """启动对账补账(DEF-11):journal 事实源中的未平仓交易在账本缺失/不完整时
        恢复 claim。与 claim() 的区别:不走 600 上限闸门——这是恢复既有事实,不是
        授予新敞口。语义 = 【以 journal 聚合值为准覆盖】:重复对账写入同一聚合值,
        结果不变(幂等);部分补账残留也会被下一次对账修正到正确值。"""
        key = f"{symbol}:{pos_side}"
        rec = self._data.setdefault(key, {"qty": 0.0, "notional": 0.0,
                                          "strategies": {}})
        rec["qty"] = float(qty)
        rec["notional"] = float(notional)
        rec["strategies"][strategy] = float(qty)
        rec["updated_at"] = time.time()
        self._save()
        return key

    def reconcile(self, active_keys):
        """对账(审计 C1):active_keys = 交易所持仓 ∪ 未平仓 journal,是唯一事实源。
        账本中不在 active_keys 的 claim 视为幽灵 → 物理删除。返回被释放的 key 列表。"""
        released = []
        for key in list(self._data.keys()):
            if key not in active_keys:
                r = self.force_release(key)
                if r:
                    released.append(r)
        return released

    def snapshot(self):
        return json.loads(json.dumps(self._data))


if __name__ == "__main__":
    # 自测
    for _f in ("test_ownership.json", "test_ownership.json.tmp"):
        if os.path.exists(_f):
            os.remove(_f)
    pl = PositionLedger("test_ownership.json", max_total_notional=600)
    ok, reason = pl.claim("BTC/USDT:USDT", "short", "arb", 1.0, 400.0)
    assert ok, reason
    ok2, reason2 = pl.claim("ETH/USDT:USDT", "long", "dir", 1.5, 250.0)
    assert not ok2, f"超限应拒绝: {reason2}"
    pl.release("BTC/USDT:USDT", "short", "arb", 1.0, 400.0)
    assert pl.total_notional() == 0
    print(f"账本自测通过 ✅：claim 400 放行 / 再 claim 250 超限拒绝({reason2}) / release 后总敞口 0")
    os.remove("test_ownership.json")
    os.remove("position_ownership.lock") if os.path.exists("position_ownership.lock") else None
