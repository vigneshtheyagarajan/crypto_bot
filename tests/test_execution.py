import pytest

from execution import LiveBroker, PaperBroker, OrderError, floor_to_step, retry


class ApiError(Exception):
    def __init__(self, code, msg="api error"):
        super().__init__(msg)
        self.code = code


SYMBOL_INFO = {
    "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
    "filters": [
        {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ],
}


class FakeClient:
    def __init__(self, free_btc=0.0, price=100000.0):
        self.free_btc, self.px = free_btc, price
        self.orders, self.create_effect, self.lookup = [], None, None

    def get_symbol_info(self, symbol):
        return SYMBOL_INFO

    def get_asset_balance(self, asset):
        return {"asset": asset, "free": str(self.free_btc)}

    def get_symbol_ticker(self, symbol):
        return {"price": str(self.px)}

    def create_order(self, **p):
        self.orders.append(p)
        if self.create_effect:
            raise self.create_effect
        if p["side"] == "BUY":
            usdt = float(p["quoteOrderQty"])
            q = usdt / self.px
            return {"orderId": 1, "status": "FILLED", "executedQty": f"{q:.5f}",
                    "cummulativeQuoteQty": f"{usdt:.2f}",
                    "fills": [{"commission": f"{q * 0.001:.8f}", "commissionAsset": "BTC"}]}
        q = float(p["quantity"])
        return {"orderId": 2, "status": "FILLED", "executedQty": p["quantity"],
                "cummulativeQuoteQty": f"{q * self.px:.2f}",
                "fills": [{"commission": f"{q * self.px * 0.001:.8f}", "commissionAsset": "USDT"}]}

    def get_order(self, symbol, origClientOrderId):
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return self.lookup


def broker(client):
    return LiveBroker(client, sleep=lambda s: None)


def test_floor_to_step_never_rounds_up():
    assert floor_to_step(0.0012399, 0.00001) == pytest.approx(0.00123)
    assert floor_to_step(1.0, 0.00001) == 1.0


def test_sell_uses_held_quantity_not_usdt_value():
    c = FakeClient(free_btc=0.00123456)
    f = broker(c).sell("BTCUSDT", qty=0.00125)       # bot thinks it holds more
    assert c.orders[-1]["quantity"] == "0.00123"       # capped to free, floored to step
    assert "quoteOrderQty" not in c.orders[-1]
    assert f.base_qty == pytest.approx(0.00123)
    assert f.quote_qty == pytest.approx(0.00123 * 100000 * 0.999, rel=1e-6)


def test_sell_below_minimum_raises_instead_of_pretending():
    c = FakeClient(free_btc=0.00004)                 # $4 < $5 min notional
    with pytest.raises(OrderError):
        broker(c).sell("BTCUSDT")
    assert c.orders == []


def test_buy_records_fill_net_of_base_fee():
    c = FakeClient()
    f = broker(c).buy("BTCUSDT", 125.0)
    assert c.orders[-1]["quoteOrderQty"] == "125.00"
    assert c.orders[-1]["newClientOrderId"].startswith("cb")
    assert f.base_qty == pytest.approx(0.00125 * 0.999, rel=1e-3)
    assert f.fee_usdt == pytest.approx(0.125, rel=1e-2)


def test_rejected_order_raises_order_error():
    c = FakeClient()
    c.create_effect = ApiError(-2010, "insufficient balance")
    with pytest.raises(OrderError):
        broker(c).buy("BTCUSDT", 125.0)


def test_timeout_then_order_found_is_not_resent():
    c = FakeClient()
    c.create_effect = TimeoutError("read timed out")
    c.lookup = {"orderId": 9, "status": "FILLED", "executedQty": "0.00125",
                "cummulativeQuoteQty": "125.00", "fills": []}
    f = broker(c).buy("BTCUSDT", 125.0)
    assert len(c.orders) == 1                          # no blind retry
    assert f.order_id == "9" and f.base_qty == pytest.approx(0.00125)


def test_timeout_and_order_never_placed():
    c = FakeClient()
    c.create_effect = TimeoutError("read timed out")
    c.lookup = ApiError(-2013, "order does not exist")
    with pytest.raises(OrderError, match="not placed"):
        broker(c).buy("BTCUSDT", 125.0)


def test_partial_fill_reports_what_filled():
    c = FakeClient(free_btc=0.01)

    def partial(**p):
        c.orders.append(p)
        return {"orderId": 3, "status": "EXPIRED", "executedQty": "0.004",
                "cummulativeQuoteQty": "400", "fills": []}
    c.create_order = partial
    f = broker(c).sell("BTCUSDT")
    assert f.status == "EXPIRED" and f.base_qty == pytest.approx(0.004)


def test_paper_round_trip_costs_fees_and_slippage():
    p = PaperBroker(lambda s: 100.0)
    b = p.buy("BTCUSDT", 100.0)
    s = p.sell("BTCUSDT", b.base_qty)
    # 0.1% fee + 0.05% slippage each side, about 0.3% round trip
    assert s.quote_qty == pytest.approx(100 * 0.999 * 0.999 * 0.9995 / 1.0005, rel=1e-9)
    assert 99.6 < s.quote_qty < 99.8


def test_retry_does_not_retry_order_errors():
    calls = []

    def f():
        calls.append(1)
        raise OrderError("no")
    with pytest.raises(OrderError):
        retry(f, sleep=lambda s: None)
    assert len(calls) == 1
