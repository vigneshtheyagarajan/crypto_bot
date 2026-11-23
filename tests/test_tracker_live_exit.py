"""The old bot's live exit path: a failed sell must keep the trade open."""
import sys
import types

import pytest

from execution import OrderError, Fill


def _install_fake_risk_manager(monkeypatch, broker):
    m = types.ModuleType("risk_manager")
    m.get_live_broker = lambda: broker
    monkeypatch.setitem(sys.modules, "risk_manager", m)


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import importlib
    import tracker as t
    t = importlib.reload(t)
    monkeypatch.setattr(t, "_send", lambda msg: None)
    t.init_log()
    return t


def trade():
    return {"BTCUSDT": {"signal": "BUY", "entry": 100.0, "stop_loss": 99.0, "target": 102.0,
                        "qty": 1.25, "rsi": 50, "usdt_used": 125.0}}


def test_failed_live_sell_keeps_trade_open(tracker, monkeypatch):
    class Broken:
        def sell(self, symbol, qty):
            raise OrderError("insufficient balance")
    _install_fake_risk_manager(monkeypatch, Broken())
    open_t, freed, pnl = tracker.check_open_trades(trade(), lambda s: 98.0, is_live=True)
    assert "BTCUSDT" in open_t and freed == 0 and pnl == 0


def test_live_sell_uses_real_fill(tracker, monkeypatch):
    class Ok:
        def sell(self, symbol, qty):
            return Fill("SELL", symbol, qty, 120.0, 96.0, 0.12, "USDT", 0.12,
                        "FILLED", "1", "cb1", 0)
    _install_fake_risk_manager(monkeypatch, Ok())
    open_t, freed, pnl = tracker.check_open_trades(trade(), lambda s: 98.0, is_live=True)
    assert open_t == {} and freed == 125.0
    assert pnl == pytest.approx(-5.0)          # real proceeds, not the stop price
