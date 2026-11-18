"""A cbBTC trend-leg bot for the make.fun btc-race-24h-20261118-1444 bounty.

The bot speaks agent-fun/bars-replay/v1 through the vendored kit shim
(vendor/makefun_bars.py): the harness sends hello once, then one batch per
minute bar, and this bot answers every batch with exactly one reply echoing
its seq, carrying at most one order. Orders decided on a batch fill at the
next bar's open, so every signal here reads closes only, never the future.

Strategy (two entry modes, one open position, long only, full size):

1. Trend leg (the profit engine). Buy when the close is a genuine breakout,
   not mid-range chop: it sits in the top 15% of its trailing 240-bar range
   AND the 60-bar return is at least +50 bps AND the 30-bar return is at
   least +25 bps. Exit on a +150 bps take-profit, a -120 bps stop, the
   trend fading (60-bar return back under -30 bps), or a 480-bar max hold.
   After any exit the bot waits 120 bars before a new entry, so it never
   re-buys the top of the same leg it just sold.

2. Healthy dip (a secondary mode, for days with no trend leg). Buy a dip of
   at least 120 bps below the trailing 120-bar high, but only while the
   close is above its 240-bar average and already ticking up, so dips are
   never bought into a falling market. Exit on a +60 bps take-profit, a
   -70 bps stop, or a 120-bar max hold. The trend-fade exit does not apply
   to dip entries.

Risk: a hard -120 bps stop (trend) / -70 bps stop (dip) caps every round
trip, and the bot holds cash whenever no signal fires, so a falling day
costs it nothing. Every parameter was picked from a plateau on the dev
window (neighbouring values all stay profitable), not from a single peak.

Deterministic: pure integer arithmetic on the closes the harness sends, no
randomness, no wall clock, no network, no file reads outside this repo.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- trend-leg mode ---
RANGE_WIN = 240          # bars of range for the breakout gate
POS_GATE_NUM = 85        # close must be at or above 85% of the range
POS_GATE_DEN = 100
RET60_MIN_BPS = 50       # 60-bar return to enter
RET30_MIN_BPS = 25       # 30-bar return to enter
EXIT_RET60_BPS = -30     # 60-bar return that ends a trend leg
TREND_TAKE_BPS = 150
TREND_STOP_BPS = 120
TREND_HOLD = 480         # max bars in a trend leg
COOLDOWN = 120           # bars to wait after any exit before re-entering

# --- healthy-dip mode ---
DIP_TRAIL = 120          # bars of high for the dip size
DIP_BPS = 120            # dip below the trailing high to enter
DIP_TAKE_BPS = 60
DIP_STOP_BPS = 70
DIP_HOLD = 120
DIP_SMA_WIN = 240        # close must be above this average to buy a dip

MAX_SLIPPAGE_BPS = 100   # wide enough that normal fills are never refused


def _bps_gain(new, old):
    """(new - old) / old in basis points, integer math, old > 0."""
    return (new - old) * 10000 // old


class Bot:
    """One bot instance: closes history plus the open position's facts."""

    def __init__(self):
        self.closes = []        # closes, oldest first, ints at price_scale 5
        self.entry_price = None
        self.entry_idx = None
        self.mode = None        # "trend" or "dip" while a position is open
        self.take_bps = None
        self.stop_bps = None
        self.hold_bars = None
        self.cool_until = -1
        self.order_no = 0

    # -- orders ---------------------------------------------------------
    def _buy(self, state):
        self.order_no += 1
        return [{"side": "buy", "client_id": "b%d" % self.order_no,
                 "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def _sell(self, state):
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            return []
        self.order_no += 1
        self.cool_until = len(self.closes) - 1 + COOLDOWN
        return [{"side": "sell", "client_id": "s%d" % self.order_no,
                 "symbol": SYMBOL, "qty": holding.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    # -- one batch ------------------------------------------------------
    def on_batch(self, state, batch):
        # Fills first: a batch can carry fills even on a minute with no bar
        # for our symbol, and a fill seen nowhere else must not be missed.
        for fill in batch["fills"]:
            if fill["status"] != "filled":
                continue
            if fill["side"] == "buy":
                self.entry_price = int(fill["price"])
                # The fill lands at this batch's bar, which is appended next,
                # so its index is the length of the history as it stands.
                self.entry_idx = len(self.closes)
            elif fill["side"] == "sell":
                self.entry_price = None
                self.entry_idx = None
                self.mode = None

        # Keep the close history: one entry per bar the harness sent us.
        bar_here = False
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                self.closes.append(int(bar["c"]))
                bar_here = True
        if not bar_here:
            return []
        idx = len(self.closes) - 1
        close = self.closes[idx]

        # The account is the truth about what is held; trust it over memory.
        holding = state.positions.get(SYMBOL)
        have_position = holding is not None and holding.qty > 0
        if not have_position:
            self.entry_price = None
            self.entry_idx = None
            self.mode = None

        if have_position:
            return self._maybe_exit(state, idx, close)
        return self._maybe_enter(state, idx, close)

    def _maybe_exit(self, state, idx, close):
        if self.entry_price is None:
            return []  # position open but its fill has not been reported yet
        gain = _bps_gain(close, self.entry_price)
        if gain >= self.take_bps or gain <= -self.stop_bps:
            return self._sell(state)
        if self.mode == "trend":
            ret60 = _bps_gain(close, self.closes[idx - 60])
            if ret60 <= EXIT_RET60_BPS:
                return self._sell(state)
        if self.entry_idx is not None and idx - self.entry_idx >= self.hold_bars:
            return self._sell(state)
        return []

    def _maybe_enter(self, state, idx, close):
        if idx < RANGE_WIN or idx < self.cool_until:
            return []
        if state.cash < 1000000:  # below the 1 USDC minimum notional
            return []
        if self._trend_entry(idx, close):
            self.mode = "trend"
            self.take_bps = TREND_TAKE_BPS
            self.stop_bps = TREND_STOP_BPS
            self.hold_bars = TREND_HOLD
            return self._buy(state)
        if self._dip_entry(idx, close):
            self.mode = "dip"
            self.take_bps = DIP_TAKE_BPS
            self.stop_bps = DIP_STOP_BPS
            self.hold_bars = DIP_HOLD
            return self._buy(state)
        return []

    def _trend_entry(self, idx, close):
        window = self.closes[idx - RANGE_WIN:idx]
        low = min(window)
        high = max(window)
        if high == low:
            return False
        # Close in the top 15% of its trailing 240-bar range.
        if (close - low) * POS_GATE_DEN < POS_GATE_NUM * (high - low):
            return False
        if _bps_gain(close, self.closes[idx - 60]) < RET60_MIN_BPS:
            return False
        if _bps_gain(close, self.closes[idx - 30]) < RET30_MIN_BPS:
            return False
        return True

    def _dip_entry(self, idx, close):
        if idx < DIP_TRAIL or idx < DIP_SMA_WIN:
            return False
        high = max(self.closes[idx - DIP_TRAIL:idx])
        if _bps_gain(high, close) < DIP_BPS:
            return False
        # Only buy a dip while the market is healthy: above its average.
        sma = sum(self.closes[idx - DIP_SMA_WIN:idx]) // DIP_SMA_WIN
        if close < sma:
            return False
        # And only once the dip has stopped falling: this bar ticked up.
        if close <= self.closes[idx - 1]:
            return False
        return True


def main():
    bot = Bot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
