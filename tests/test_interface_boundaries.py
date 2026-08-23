"""Executable contracts for interface-first module boundaries."""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engines.runtime_api import DirectionalRuntimeAPI
from exchange.models import BalanceInfo, PositionInfo
from interfaces.trading import TradingRuntimePort
from storage.query_api import (
    agent_status_summary,
    latest_analysis,
    list_anomalies,
    list_factor_trials,
    list_risk_events,
)


class _Exchange:
    name = "contract-test"

    def fetch_balance(self):
        return BalanceInfo(total_eq=1000, usdt_free=900, usdt_total=950)

    def fetch_positions(self):
        return [PositionInfo("BTC-USDT-SWAP", "BTC", "long",
                             contracts=1, base_qty=0.01, avg_px=100)]

    def venue_for(self, base):
        return "swap"


class _Risk:
    halt_reason = ""

    @staticmethod
    def can_trade():
        return True


class _Journal:
    def __init__(self, db_path):
        self.db_path = db_path
        self.trades = [{
            "id": "txn_contract", "symbol": "BTC", "status": "closed",
            "direction": "long", "entry_price": 100.0, "exit_price": 102.0,
            "stop_loss": 99.0, "take_profit": 102.0, "size": 1.0,
            "size_unit": "base", "notional_usdt": 100.0, "pnl": 0.02,
            "entry_time": time.time(), "venue": "swap",
        }]


class _Trader:
    def __init__(self, db_path):
        self.exchange = _Exchange()
        self.journal = _Journal(db_path)
        self.risk = _Risk()
        self._db_path = db_path
        self.live_mode = False
        self.paused = False
        self.crypto_watchlist = ["BTC"]
        self.stock_watchlist = []
        self.watchlist = ["BTC"]
        self.watch_scores = {"BTC": 91.0}
        self.rt = None
        self.last_error = ""

    @staticmethod
    def effective_threshold():
        return 50.0

    @staticmethod
    def _trade_budget(_base):
        return 1

    @staticmethod
    def scan_signal(base):
        return {"base": base, "dir": "long"}

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def _assert_service_uses_boundary():
    tree = ast.parse((ROOT / "service" / "app.py").read_text())
    forbidden_imports = {"storage.db", "tools.readiness",
                         "tools.entry_accuracy_audit"}
    bad_imports = []
    local_imports = []
    bad_attrs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            local_imports.extend(alias.name for alias in node.names
                                 if alias.name.split(".")[0] in
                                 {"decision", "engines", "execution", "storage",
                                  "interfaces", "tools"})
            bad_imports.extend(alias.name for alias in node.names
                               if alias.name in forbidden_imports)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in {
                    "decision", "engines", "execution", "storage",
                    "interfaces", "tools"}:
                local_imports.append(node.module)
            if node.module in forbidden_imports:
                bad_imports.append(node.module)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
            call = node.value.func
            if isinstance(call, ast.Name) and call.id == "_trader":
                bad_attrs.append(node.attr)
    assert not bad_imports, f"service 绕过接口导入: {bad_imports}"
    assert not bad_attrs, f"service 直接读取 trader 内部属性: {bad_attrs}"
    allowed = {"decision", "engines.runtime_api", "interfaces.trading",
               "storage.query_api"}
    assert set(local_imports) <= allowed, (
        f"service 未经公开 API 跨模块依赖: {sorted(set(local_imports) - allowed)}")


def main():
    tmp = tempfile.mkdtemp(prefix="interface_contract_")
    db_path = os.path.join(tmp, "contract.db")
    api = DirectionalRuntimeAPI(_Trader(db_path))
    assert isinstance(api, TradingRuntimePort)
    assert api.adapter_name == "contract-test"
    assert api.status_snapshot()["balance"].usdt_free == 900
    assert api.watchlist_snapshot()["items"][0]["base"] == "BTC"
    assert api.inspect_signal("btc")["venue"] == "swap"
    assert api.journal_snapshot(20)["total_pnl_usdt"] == 2.0
    assert api.realtime_snapshot("btc")["fresh"] is False
    assert api.reconcile_snapshot()["balanced"] is False
    api.pause()
    assert api.paused is True
    api.resume()
    assert api.paused is False

    # Storage callers consume explicit read-only functions, not schema/SQL.
    assert list_anomalies(db_path) == []
    assert latest_analysis(db_path) is None
    assert list_factor_trials(db_path, "A_pullback") == []
    assert list_risk_events(db_path) == []
    assert agent_status_summary(db_path)["total_runs"] == 0
    _assert_service_uses_boundary()
    print("interface boundaries: 17 passed, 0 failed")


if __name__ == "__main__":
    main()
