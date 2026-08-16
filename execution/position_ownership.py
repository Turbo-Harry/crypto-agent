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
        self.path = path
        self.lock_path = lock_path
        self.max_total_notional = max_total_notional
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _locked(self, fn):
        """独立锁文件 flock（锁文件永不 os.replace）。无 fcntl 时退化直跑。"""
        if fcntl is None:
            return fn()
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

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
                self._data.pop(key, None)
            else:
                rec["updated_at"] = time.time()
            self._save()

        self._locked(_do)

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
