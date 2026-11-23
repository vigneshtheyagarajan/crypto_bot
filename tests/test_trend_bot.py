import os
from datetime import datetime, timezone, timedelta

import pytest

import trend_bot as tb
from execution import PaperBroker, OrderError

DAY = 86_400_000


def day_ms(d):
    return int(datetime(2026, 11, d, tzinfo=timezone.utc).timestamp() * 1000)


class FakeClient:
    """Daily closes: 30 days ending the day before `today`."""

    def __init__(self, closes, today=23, price=None):
        self.closes, self.today = closes, today
        self.price = price if price is not None else closes[-1]
        self.fail = None

    def get_klines(self, symbol, interval, limit):
        if self.fail:
            raise self.fail
        start = day_ms(self.today) - len(self.closes) * DAY
        kl = [[start + i * DAY, 0, 0, 0, str(c), 0, start + (i + 1) * DAY - 1]
              for i, c in enumerate(self.closes)]
        # the still-forming candle for today, with a misleading close
        kl.append([day_ms(self.today), 0, 0, 0, "1", 0, day_ms(self.today) + DAY - 1])
        return kl[-limit:]

    def get_symbol_ticker(self, symbol):
        return {"price": str(self.price)}


@pytest.fixture(autouse=True)
def tmp_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(tb, "TRADES_FILE", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(tb, "STOP_FILE", str(tmp_path / "STOP"))
    return tmp_path


def run(state, client, now_day=23, broker=None):
    msgs = []
    broker = broker or PaperBroker(lambda s: client.price)
    now = day_ms(now_day) + 3_600_000           # 01:00 UTC
    return tb.check(state, client, broker, msgs.append, now_ms=now), msgs


def rising():
    return [100 + i for i in range(30)]


def falling():
    return [200 - i for i in range(30)]


def test_buys_when_close_above_sma_and_counts_fees():
    st = tb.new_state(500, 129)
    st, msgs = run(st, FakeClient(rising()))
    assert st["in_position"] and st["usdt"] == pytest.approx(0)
    assert st["fees_paid_usdt"] == pytest.approx(0.5, rel=1e-6)
    assert st["last_decision_day"] == "2026-11-22"
    assert any("PAPER BUY" in m for m in msgs)


def test_ignores_forming_candle():
    # forming candle close is "1"; if it were used, the bot would sell/stay cash
    st = tb.new_state(500, 129)
    st, _ = run(st, FakeClient(rising()))
    assert st["in_position"]


def test_trades_at_most_once_per_daily_close():
    c = FakeClient(rising())
    st, _ = run(tb.new_state(500, 129), c)
    n = st["trade_count"]
    for _ in range(5):
        st, _ = run(st, c)
    assert st["trade_count"] == n


def test_sells_when_close_drops_below_sma():
    st, _ = run(tb.new_state(500, 129), FakeClient(rising()))
    st, _ = run(st, FakeClient(falling(), today=24, price=171), now_day=24)
    assert not st["in_position"] and st["base"] == 0
    assert st["trade_count"] == 2


def test_position_survives_restart_across_midnight():
    st, _ = run(tb.new_state(500, 129), FakeClient(rising()))
    restored = tb.load_state()                    # simulate process restart next day
    assert restored["in_position"] and restored["base"] > 0
    st, _ = run(restored, FakeClient(rising() + [131], today=24, price=131), now_day=24)
    assert st["in_position"] and st["trade_count"] == 1   # still holding, no re-buy


def test_failed_order_does_not_mark_day_done():
    class Broken(PaperBroker):
        def buy(self, symbol, usdt):
            raise OrderError("rejected")
    st = tb.new_state(500, 129)
    with pytest.raises(OrderError):
        run(st, FakeClient(rising()), broker=Broken(lambda s: 129))
    assert st["last_decision_day"] is None and not st["in_position"]
    st, _ = run(st, FakeClient(rising()))           # retried next check
    assert st["in_position"]


def test_stale_data_fails_closed():
    st = tb.new_state(500, 129)
    c = FakeClient(rising(), today=20)               # newest candle 3 days old
    with pytest.raises(ValueError, match="old"):
        run(st, c, now_day=23)
    assert not st["in_position"]


def test_kill_switch_moves_to_cash_and_stays_halted():
    st, _ = run(tb.new_state(500, 129), FakeClient(rising()))
    crash = FakeClient(rising(), price=129 * 0.70)   # -30% vs peak
    st, msgs = run(st, crash)
    assert st["halted"] and not st["in_position"]
    assert any("KILL SWITCH" in m for m in msgs)
    st, _ = run(st, FakeClient(rising() + [200], today=24, price=200), now_day=24)
    assert not st["in_position"]                     # stays out until reset


def test_stop_file_pauses(tmp_logs):
    open(tb.STOP_FILE, "w").close()
    st, _ = run(tb.new_state(500, 129), FakeClient(rising()))
    assert not st["in_position"] and st["last_decision_day"] is None


def test_live_mode_is_refused(monkeypatch):
    monkeypatch.setattr(tb, "TRADING_MODE", "live")
    with pytest.raises(SystemExit) as e:
        tb.main([])
    assert "disabled" in str(e.value)


def test_state_file_written_atomically():
    st = tb.new_state(500, 129)
    tb.save_state(st)
    assert tb.load_state()["usdt"] == 500
    assert not os.path.exists(tb.STATE_FILE + ".tmp")
