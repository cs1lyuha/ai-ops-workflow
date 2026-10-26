"""Ce se întâmplă dacă procesul cade la pasul N? Fiecare test simulează alt punct de cădere."""

import pytest

from opsflow.db import connect, seed
from opsflow.services import FakeCRM, FakeShippingAPI
from opsflow.workflow import OpsApp


@pytest.fixture
def conn():
    c = connect()
    seed(c)
    return c


def count_orders(conn):
    return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def test_courier_down_then_resume(conn, sample):
    app = OpsApp(conn, shipping=FakeShippingAPI(fail_times=1))
    res = app.handle_email(sample("01"))
    assert res.status == "failed" and res.next_node == "quote_shipping"
    assert count_orders(conn) == 0
    [res] = app.resume_failed()
    assert res.status == "done" and count_orders(conn) == 1
    # aceeași cheie de idempotență la ambele încercări
    assert len(set(app.shipping.calls)) == 1 and len(app.shipping.calls) == 2


def test_crm_wrote_but_connection_dropped(conn, sample):
    """Cel mai urât caz: CRM-ul a salvat activitatea, dar noi am primit timeout."""
    app = OpsApp(conn, crm=FakeCRM(crash_after_write=1))
    res = app.handle_email(sample("01"))
    assert res.status == "failed" and res.next_node == "update_crm"
    res = app.runner.resume(res.run_id)
    assert res.status == "done"
    assert len(app.crm.activities) == 1  # Idempotency-Key a împiedicat dublura
    assert count_orders(conn) == 1       # comanda nu s-a creat de două ori


def test_process_restart_while_waiting_for_approval(conn, sample):
    first = OpsApp(conn)
    res = first.handle_email(sample("02"))
    assert res.status == "waiting"
    del first  # procesul s-a oprit; aprobarea vine a doua zi, din alt proces
    second = OpsApp(conn)
    res = second.decide(res.run_id, "approve", by="ana")
    assert res.status == "done" and count_orders(conn) == 1


def test_crash_inside_create_order_rolls_back(conn, sample, monkeypatch):
    app = OpsApp(conn)
    original = conn.execute
    calls = {"n": 0}

    class Boom(Exception):
        pass

    def flaky(sql, *args):
        if sql.startswith("UPDATE customers SET balance") and calls["n"] == 0:
            calls["n"] += 1
            raise Boom("disc plin")
        return original(sql, *args)

    class ConnProxy:
        def __getattr__(self, name):
            return getattr(conn, name)

        execute = staticmethod(flaky)

    app.runner.services["conn"] = ConnProxy()
    res = app.handle_email(sample("01"))
    assert res.status == "failed" and res.next_node == "create_order"
    assert count_orders(conn) == 0                      # tranzacția a fost anulată
    assert conn.execute("SELECT stock FROM products WHERE sku='KIT-LOGI'").fetchone()[0] == 120
    res = app.runner.resume(res.run_id)
    assert res.status == "done" and count_orders(conn) == 1


def test_resume_after_done_is_noop(conn, sample):
    app = OpsApp(conn)
    res = app.handle_email(sample("04"))
    again = app.runner.resume(res.run_id)
    assert again.status == "done"
    assert len(app.mailer.sent) == 1
