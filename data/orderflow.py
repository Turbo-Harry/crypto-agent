"""Thread-safe, deterministic L2 order-flow accumulator.

The accumulator consumes successive order-book events and exposes a bounded
window snapshot.  It performs no network or trading operations, so the same
book sequence can be replayed in tests and research without changing feature
semantics.
"""
from collections import defaultdict, deque
import threading
import time


def _levels(book, side, depth):
    rows = []
    for row in (book or {}).get(side, [])[:depth]:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and qty >= 0:
            rows.append((price, qty))
    return rows


def multilevel_ofi_event(current, previous, depth):
    """Return (OFI numerator, depth scale, bid cancels, ask cancels).

    Each rank follows the Cont best-queue event rule, extended over the first
    ``depth`` ranks.  Same-price queue depletion is kept separately as a
    cancellation proxy; prices that leave the visible range are not labelled
    cancellations because trades and rank changes cannot be distinguished.
    """
    bids = _levels(current, "bids", depth)
    asks = _levels(current, "asks", depth)
    prev_bids = _levels(previous, "bids", depth)
    prev_asks = _levels(previous, "asks", depth)
    usable = min(len(bids), len(asks), len(prev_bids), len(prev_asks), depth)
    if usable < 1:
        return None

    numerator = 0.0
    for idx in range(usable):
        bid, bid_qty = bids[idx]
        prev_bid, prev_bid_qty = prev_bids[idx]
        ask, ask_qty = asks[idx]
        prev_ask, prev_ask_qty = prev_asks[idx]
        numerator += (
            (bid_qty if bid >= prev_bid else 0.0)
            - (prev_bid_qty if bid <= prev_bid else 0.0)
            - (ask_qty if ask <= prev_ask else 0.0)
            + (prev_ask_qty if ask >= prev_ask else 0.0)
        )

    current_depth = sum(qty for _, qty in bids[:usable] + asks[:usable])
    previous_depth = sum(qty for _, qty in
                         prev_bids[:usable] + prev_asks[:usable])
    scale = (current_depth + previous_depth) / 2.0
    if scale <= 0:
        return None

    current_bid_map = dict(bids)
    current_ask_map = dict(asks)
    bid_cancel = sum(max(0.0, qty - current_bid_map[price])
                     for price, qty in prev_bids
                     if price in current_bid_map)
    ask_cancel = sum(max(0.0, qty - current_ask_map[price])
                     for price, qty in prev_asks
                     if price in current_ask_map)
    return numerator, scale, bid_cancel, ask_cancel


class OrderFlowAccumulator:
    """Aggregate multilevel OFI events over a short, freshness-gated window."""

    def __init__(self, depth, window_seconds, min_events, max_age_seconds):
        self.depth = int(depth)
        self.window_seconds = float(window_seconds)
        self.min_events = int(min_events)
        self.max_age_seconds = float(max_age_seconds)
        self._previous = {}
        self._latest_ts = {}
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def update(self, base, book, ts=None):
        """Consume one L2 event; the first valid book only establishes state."""
        now = float(ts if ts is not None else time.time())
        current = {"bids": _levels(book, "bids", self.depth),
                   "asks": _levels(book, "asks", self.depth)}
        if not current["bids"] or not current["asks"]:
            return False
        with self._lock:
            previous = self._previous.get(base)
            self._previous[base] = current
            self._latest_ts[base] = now
            if previous is None:
                return False
            event = multilevel_ofi_event(current, previous, self.depth)
            if event is None:
                return False
            self._events[base].append((now,) + event)
            self._prune(base, now)
        return True

    def _prune(self, base, now):
        cutoff = now - self.window_seconds
        events = self._events[base]
        while events and events[0][0] < cutoff:
            events.popleft()

    def snapshot(self, base, now=None):
        """Return values plus explicit missing/insufficient/stale status."""
        now = float(now if now is not None else time.time())
        with self._lock:
            latest_ts = self._latest_ts.get(base)
            if latest_ts is None:
                return {"status": "missing", "ofi_event_multilevel": None,
                        "ofi_event_cancel_imbalance": None,
                        "ofi_event_count": 0, "ofi_event_age_ms": None}
            self._prune(base, now)
            events = list(self._events[base])
            age = max(0.0, now - latest_ts)
            common = {"ofi_event_count": len(events),
                      "ofi_event_age_ms": age * 1000.0}
            if age > self.max_age_seconds:
                return {"status": "stale", "ofi_event_multilevel": None,
                        "ofi_event_cancel_imbalance": None, **common}
            if len(events) < self.min_events:
                return {"status": "insufficient",
                        "ofi_event_multilevel": None,
                        "ofi_event_cancel_imbalance": None, **common}
            numerator = sum(event[1] for event in events)
            scale = sum(event[2] for event in events)
            bid_cancel = sum(event[3] for event in events)
            ask_cancel = sum(event[4] for event in events)
            cancelled = bid_cancel + ask_cancel
            return {
                "status": "ready",
                "ofi_event_multilevel": numerator / scale if scale else None,
                "ofi_event_cancel_imbalance": (
                    (ask_cancel - bid_cancel) / cancelled if cancelled else 0.0),
                **common,
            }
