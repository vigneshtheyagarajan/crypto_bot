"""
Order execution shared by the bots.

PaperBroker  simulates market fills with fees and slippage, so paper P&L is
             comparable to what live trading would have produced.
LiveBroker   talks to Binance Spot. It is only reachable from code that has
             passed the live-trading guard, and that guard is closed in this
             version (see trend_bot.py).

What LiveBroker fixes compared with the old risk_manager.place_order:
  - Sells the quantity you actually hold, rounded DOWN to the symbol's
    LOT_SIZE step, never "$125 worth".
  - Records the real fill: executed quantity, average price, and the fee,
    with a base-asset fee subtracted from the quantity you now hold.
  - Every order carries a newClientOrderId. If the request times out, the
    order is looked up by that id instead of being retried blindly, so a
    timeout can't turn into a double buy.
  - Rejections raise OrderError so the caller can keep the position open and
    alert, instead of pretending the trade closed.
"""
import time
import uuid
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN

FEE = 0.001          # Binance spot taker fee per side, no BNB discount
SLIPPAGE = 0.0005    # assumed adverse fill vs last price, per side


class OrderError(Exception):
    """The order was rejected or could not be confirmed. Nothing changed."""


@dataclass
class Fill:
    side: str            # "BUY" or "SELL"
    symbol: str
    base_qty: float      # base asset now held (BUY) or sold (SELL), net of base fees
    quote_qty: float     # USDT spent (BUY) or received net of fees (SELL)
    avg_price: float
    fee_usdt: float      # fee valued in USDT (0 if paid in a third asset)
    fee_asset: str
    fee_amount: float
    status: str          # FILLED, or EXPIRED/PARTIAL for a partial fill
    order_id: str
    client_order_id: str
    time: float

    def to_dict(self):
        return asdict(self)


def retry(fn, attempts=4, base_delay=1.0, sleep=time.sleep):
    """Call fn(), retrying network-type errors with exponential backoff.
    OrderError is never retried: a rejection is an answer, not a glitch."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except OrderError:
            raise
        except Exception as e:  # network errors, 5xx, rate limits
            last = e
            if i < attempts - 1:
                sleep(base_delay * (2 ** i))
    raise last


def floor_to_step(qty, step):
    q, s = Decimal(str(qty)), Decimal(str(step))
    if s <= 0:
        return float(q)
    return float((q / s).to_integral_value(rounding=ROUND_DOWN) * s)


def _new_client_id(prefix="cb"):
    return f"{prefix}{uuid.uuid4().hex[:24]}"


# ── paper ─────────────────────────────────────────────────────────────────────

class PaperBroker:
    """Simulated market orders at the current price, with slippage and fees.
    The fee on a buy is taken from the coins received, as Binance does when
    you don't pay fees in BNB."""

    def __init__(self, price_fn, fee=FEE, slippage=SLIPPAGE, clock=time.time):
        self.price_fn, self.fee, self.slippage, self.clock = price_fn, fee, slippage, clock

    def buy(self, symbol, usdt):
        if usdt <= 0:
            raise OrderError("nothing to buy with")
        px = self.price_fn(symbol)
        if not px:
            raise OrderError(f"no price for {symbol}")
        fill_px = px * (1 + self.slippage)
        gross_qty = usdt / fill_px
        fee_base = gross_qty * self.fee
        cid = _new_client_id("paper")
        return Fill("BUY", symbol, gross_qty - fee_base, usdt, fill_px,
                    fee_base * fill_px, symbol.replace("USDT", ""), fee_base,
                    "FILLED", cid, cid, self.clock())

    def sell(self, symbol, qty):
        if qty <= 0:
            raise OrderError("nothing to sell")
        px = self.price_fn(symbol)
        if not px:
            raise OrderError(f"no price for {symbol}")
        fill_px = px * (1 - self.slippage)
        gross = qty * fill_px
        fee = gross * self.fee
        cid = _new_client_id("paper")
        return Fill("SELL", symbol, qty, gross - fee, fill_px, fee, "USDT", fee,
                    "FILLED", cid, cid, self.clock())


# ── live ──────────────────────────────────────────────────────────────────────

# Binance error codes that mean "rejected, nothing happened".
_REJECT_CODES = {-1013, -1100, -1102, -1106, -1111, -1121, -2010, -2011}
_UNKNOWN_ORDER = -2013


class LiveBroker:
    def __init__(self, client, sleep=time.sleep, clock=time.time):
        self.client, self.sleep, self.clock = client, sleep, clock
        self._filters = {}

    # exchange metadata
    def filters(self, symbol):
        if symbol not in self._filters:
            info = retry(lambda: self.client.get_symbol_info(symbol), sleep=self.sleep)
            f = {x["filterType"]: x for x in info["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
            if lot and float(lot.get("stepSize", 0)) == 0:
                lot = f.get("LOT_SIZE", {})
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters[symbol] = {
                "step": float(lot.get("stepSize", 0) or 0),
                "min_qty": float(lot.get("minQty", 0) or 0),
                "min_notional": float(notional.get("minNotional", 0) or 0),
                "base": info.get("baseAsset", symbol.replace("USDT", "")),
                "quote": info.get("quoteAsset", "USDT"),
            }
        return self._filters[symbol]

    def free_balance(self, asset):
        b = retry(lambda: self.client.get_asset_balance(asset=asset), sleep=self.sleep)
        return float(b["free"]) if b else 0.0

    def price(self, symbol):
        t = retry(lambda: self.client.get_symbol_ticker(symbol=symbol), sleep=self.sleep)
        return float(t["price"])

    # orders
    def buy(self, symbol, usdt):
        f = self.filters(symbol)
        usdt = floor_to_step(usdt, 0.01)
        if usdt < f["min_notional"]:
            raise OrderError(f"{symbol}: ${usdt} is below the minimum order ${f['min_notional']}")
        return self._submit(symbol, "BUY", quoteOrderQty=f"{usdt:.2f}")

    def sell(self, symbol, qty=None):
        """Sell `qty` (default: everything free), capped at the free balance
        and rounded down to the lot step."""
        f = self.filters(symbol)
        free = self.free_balance(f["base"])
        q = free if qty is None else min(qty, free)
        q = floor_to_step(q, f["step"])
        if q <= 0 or q < f["min_qty"]:
            raise OrderError(f"{symbol}: sell size {q} below minimum {f['min_qty']} "
                             f"(free balance {free})")
        if f["min_notional"] and q * self.price(symbol) < f["min_notional"]:
            raise OrderError(f"{symbol}: sell value below minimum notional {f['min_notional']}")
        return self._submit(symbol, "SELL", quantity=_fmt(q, f["step"]))

    def _submit(self, symbol, side, **size):
        cid = _new_client_id()
        params = dict(symbol=symbol, side=side, type="MARKET",
                      newClientOrderId=cid, newOrderRespType="FULL", **size)
        try:
            order = self.client.create_order(**params)
        except Exception as e:
            code = getattr(e, "code", None)
            if code in _REJECT_CODES:
                raise OrderError(f"{side} {symbol} rejected: {e}") from e
            # Timeout or unknown failure: the order may or may not exist.
            order = self._lookup(symbol, cid, e)
        return self._parse(symbol, side, order, cid)

    def _lookup(self, symbol, cid, original):
        for i in range(5):
            self.sleep(1.0 * (2 ** i))
            try:
                return self.client.get_order(symbol=symbol, origClientOrderId=cid)
            except Exception as e:
                if getattr(e, "code", None) == _UNKNOWN_ORDER:
                    raise OrderError(f"order {cid} was not placed: {original}") from e
        raise OrderError(f"order {cid} state unknown after {original}; check Binance manually")

    def _parse(self, symbol, side, o, cid):
        f = self.filters(symbol)
        executed = float(o.get("executedQty", 0))
        quote = float(o.get("cummulativeQuoteQty", 0))
        status = o.get("status", "")
        if executed <= 0:
            raise OrderError(f"{side} {symbol} not filled (status {status})")
        avg = quote / executed
        fee_base = fee_quote = fee_other = 0.0
        fee_asset = ""
        for fl in o.get("fills", []):
            c, a = float(fl.get("commission", 0)), fl.get("commissionAsset", "")
            fee_asset = a
            if a == f["base"]:
                fee_base += c
            elif a == f["quote"]:
                fee_quote += c
            else:
                fee_other += c
        fee_usdt = fee_base * avg + fee_quote
        if side == "BUY":
            base_qty, quote_qty = executed - fee_base, quote + fee_quote
        else:
            base_qty, quote_qty = executed, quote - fee_quote
        return Fill(side, symbol, base_qty, quote_qty, avg, fee_usdt, fee_asset,
                    fee_base + fee_quote + fee_other,
                    "FILLED" if status == "FILLED" else (status or "PARTIAL"),
                    str(o.get("orderId", "")), cid, self.clock())


def _fmt(q, step):
    s = Decimal(str(step)).normalize()
    places = max(0, -s.as_tuple().exponent)
    return f"{q:.{places}f}"
