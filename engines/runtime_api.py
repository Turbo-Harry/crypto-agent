"""Service-facing adapter for the directional trading runtime.

This is the only place outside the engine implementation that translates the
current ``DirectionalTrader`` object graph into stable, read-only snapshots.
The HTTP layer depends on :class:`interfaces.trading.TradingRuntimePort` and no
longer reaches into mutable engine collaborators.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Mapping, Sequence

import config
from execution.trade_journal import (
    LEGACY_CT_VAL,
    realized_pnl_usdt,
    total_net_realized_pnl_usdt,
    total_realized_pnl_usdt,
)
from interfaces.trading import TradingRuntimePort
from storage.query_api import live_pnl_baseline, latest_position_snapshot_ts


class DirectionalRuntimeAPI(TradingRuntimePort):
    """Explicit boundary around a ``DirectionalTrader`` instance."""

    def __init__(self, trader: Any):
        self._trader = trader

    @property
    def adapter_name(self) -> str:
        return str(getattr(self._trader.exchange, "name", "unknown"))

    @property
    def paused(self) -> bool:
        return bool(self._trader.paused)

    @property
    def db_path(self) -> str | None:
        return (getattr(self._trader, "_db_path", None)
                or getattr(self._trader.journal, "db_path", None))

    @staticmethod
    def _notional(trade: Mapping[str, Any]) -> float:
        value = trade.get("notional_usdt")
        if value is not None:
            return float(value)
        return round(float(trade.get("size") or 0)
                     * float(trade.get("entry_price") or 0), 2)

    def status_snapshot(self) -> Mapping[str, Any]:
        try:
            balance = self._trader.exchange.fetch_balance()
        except Exception:
            balance = None
        try:
            positions = list(self._trader.exchange.fetch_positions())
        except Exception:
            positions = []
        trades = list(self._trader.journal.trades)
        open_trades = [row for row in trades if row.get("status") == "open"]
        today = time.strftime("%Y-%m-%d")

        def is_today(row: Mapping[str, Any]) -> bool:
            entered = row.get("entry_time")
            return bool(entered) and time.strftime(
                "%Y-%m-%d", time.localtime(entered)) == today

        live_realized = live_equity = live_start = None
        if getattr(self._trader, "live_mode", False):
            closed_live = [row for row in trades
                           if row.get("status") == "closed"
                           and row.get("venue") == "live"]
            live_realized = total_net_realized_pnl_usdt(closed_live)
            live_start = live_pnl_baseline(self.db_path)
            if live_start and balance and balance.total_eq > 0:
                live_equity = round(balance.total_eq - live_start, 2)

        cooling = {"cooling": False, "remaining": 0.0, "streak": 0}
        try:
            from decision.loss_cooling import (
                cooling_remaining_hours,
                is_cooling,
                streak,
            )
            cooling = {"cooling": is_cooling(self.db_path),
                       "remaining": cooling_remaining_hours(self.db_path),
                       "streak": streak(self.db_path)}
        except Exception:
            pass

        return {
            "balance": balance,
            "positions": positions,
            "open_trades": open_trades,
            "risk_halted": not self._trader.risk.can_trade(),
            "risk_reason": self._trader.risk.halt_reason,
            "decision_threshold": self._trader.effective_threshold(),
            "today_trade_count": sum(is_today(row) for row in trades),
            "total_notional_usdt": round(sum(self._notional(row)
                                               for row in trades), 2),
            "open_notional_usdt": round(sum(self._notional(row)
                                              for row in open_trades), 2),
            "today_notional_usdt": round(sum(self._notional(row)
                                               for row in trades
                                               if is_today(row)), 2),
            "live_realized_pnl_usdt": live_realized,
            "live_equity_pnl_usdt": live_equity,
            "live_pnl_start_equity": live_start,
            "loss_cooling": cooling["cooling"],
            "loss_cooling_remaining_hours": cooling["remaining"],
            "loss_streak": cooling["streak"],
        }

    def watchlist_snapshot(self) -> Mapping[str, Any]:
        crypto = list(getattr(self._trader, "crypto_watchlist", []))
        stocks = list(getattr(self._trader, "stock_watchlist", []))
        if not crypto and not stocks:
            stock_set = set(config.STOCK_SWAP_TOKENS)
            crypto = [base for base in self._trader.watchlist
                      if base not in stock_set]
            stocks = [base for base in self._trader.watchlist
                      if base in stock_set]

        def item(base: str, pool: str) -> dict[str, Any]:
            return {"base": base, "score": self._trader.watch_scores.get(base),
                    "budget": self._trader._trade_budget(base), "pool": pool}

        crypto_items = [item(base, "crypto") for base in crypto]
        stock_items = [item(base, "stock") for base in stocks]
        return {"date": time.strftime("%Y-%m-%d"),
                "crypto_items": crypto_items, "stock_items": stock_items,
                "items": crypto_items + stock_items}

    def inspect_signal(self, base: str) -> Mapping[str, Any]:
        normalized = base.upper()
        signal = self._trader.scan_signal(normalized)
        return {"base": normalized,
                "venue": self._trader.exchange.venue_for(normalized),
                "signal": signal,
                "message": "有信号" if signal else "无回踩确认信号"}

    def journal_snapshot(self, limit: int) -> Mapping[str, Any]:
        all_trades = list(self._trader.journal.trades)
        closed = [row for row in all_trades if row.get("status") == "closed"]
        wins = [row for row in closed if (row.get("pnl") or 0) > 0]
        live_total = total_net_realized_pnl_usdt(
            [row for row in closed if row.get("venue") == "live"])
        trades = []
        for row in all_trades[-max(0, limit):]:
            item = dict(row)
            item["pnl_usdt"] = realized_pnl_usdt(row)
            trades.append(item)
        return {
            "total": len(all_trades),
            "closed": len(closed),
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "total_pnl_usdt": total_realized_pnl_usdt(closed),
            "live_total_pnl_usdt": live_total,
            "trades": trades,
        }

    def realtime_snapshot(self, base: str) -> Mapping[str, Any]:
        normalized = base.upper()
        data: dict[str, Any] = {}
        orderflow = {"status": "missing", "ofi_event_multilevel": None,
                     "ofi_event_cancel_imbalance": None,
                     "ofi_event_count": 0, "ofi_event_age_ms": None}
        realtime = self._trader.rt
        if realtime is not None:
            data = realtime.get(normalized, max_age=60)
            try:
                getter = getattr(realtime, "get_orderflow", None)
                if getter:
                    orderflow.update(getter(normalized) or {})
            except Exception:
                pass
        return {"base": normalized, "price": data.get("price"),
                "swap_price": data.get("swap_price"),
                "funding": data.get("funding"),
                "vol_15m": data.get("vol_15m"),
                "fresh": bool(data.get("price")), **orderflow}

    def refresh_watchlist(self) -> Sequence[Mapping[str, Any]]:
        from engines.daily_scan import screen_daily

        rows = screen_daily(exchange=self._trader.exchange,
                            db_path=self.db_path)
        self._trader.crypto_watchlist = [row["base"] for row in rows
                                         if not row.get("is_stock")]
        self._trader.stock_watchlist = [row["base"] for row in rows
                                        if row.get("is_stock")]
        self._trader.watchlist = (self._trader.crypto_watchlist
                                  + self._trader.stock_watchlist)
        self._trader.watch_scores = {row["base"]: row["score"] for row in rows}
        self._trader._watch_date = time.strftime("%Y-%m-%d")
        self._trader._last_watch_refresh = time.time()
        return rows

    def reconcile_snapshot(self) -> Mapping[str, Any]:
        journal_open: list[dict[str, Any]] = []
        journal_by_symbol: defaultdict[str, float] = defaultdict(float)
        notes: list[str] = []
        for row in self._trader.journal.trades:
            if row.get("status") != "open":
                continue
            size = float(row.get("size") or 0)
            if row.get("size_unit") == "contracts(legacy)":
                contract_value = float(row.get("ct_val")
                                       or LEGACY_CT_VAL.get(row["symbol"], 1.0))
                base_qty = size * contract_value
                notes.append(
                    f"{row['id']} {row['symbol']} 为 legacy 单位（{size} 张 × "
                    f"ctVal {contract_value}），已折算")
            else:
                base_qty = size
            journal_by_symbol[row["symbol"]] += base_qty
            journal_open.append({"id": row["id"], "symbol": row["symbol"],
                                 "base_qty": round(base_qty, 8),
                                 "venue": row.get("venue") or "swap",
                                 "notional_usdt": row.get("notional_usdt")})

        positions = self._trader.exchange.fetch_positions()
        exchange_positions: list[dict[str, Any]] = []
        exchange_by_symbol: defaultdict[str, float] = defaultdict(float)
        for position in positions:
            exchange_by_symbol[position.base] += position.base_qty
            exchange_positions.append({
                "inst_id": position.inst_id, "side": position.side,
                "contracts": position.contracts,
                "base_qty": round(position.base_qty, 8),
                "avg_px": position.avg_px,
            })
        symbols = sorted(set(journal_by_symbol) | set(exchange_by_symbol))
        per_symbol = [{
            "symbol": symbol,
            "journal_base": round(journal_by_symbol.get(symbol, 0.0), 8),
            "exchange_base": round(exchange_by_symbol.get(symbol, 0.0), 8),
            "diff": round(exchange_by_symbol.get(symbol, 0.0)
                          - journal_by_symbol.get(symbol, 0.0), 8),
        } for symbol in symbols]
        return {
            "snapshot_ts": latest_position_snapshot_ts(self.db_path),
            "journal_open": journal_open,
            "exchange_positions": exchange_positions,
            "per_symbol": per_symbol,
            "balanced": all(abs(row["diff"]) < 1e-9 for row in per_symbol),
            "notes": notes,
        }

    def run_daily_analysis(self) -> Mapping[str, Any]:
        from decision.analyst import run_daily
        return run_daily(db_path=self.db_path, notifier=self._trader._notify)

    def pause(self) -> None:
        self._trader.pause()

    def resume(self) -> None:
        self._trader.resume()

    def error_snapshot(self) -> str:
        return str(self._trader.last_error)


def runtime_api(trader: Any) -> TradingRuntimePort:
    """Return the stable boundary for a concrete engine instance."""
    return DirectionalRuntimeAPI(trader)
