import pytest

from opsflow.db import connect, seed
from opsflow.workflow import OpsApp, build_graph


@pytest.fixture
def app():
    conn = connect()
    seed(conn)
    return OpsApp(conn)


def stock(app, sku):
    return app.conn.execute("SELECT stock FROM products WHERE sku = ?", (sku,)).fetchone()[0]


def test_graph_is_valid():
    build_graph().validate()


def test_auto_route_creates_order_and_notifies(app, sample):
    res = app.handle_email(sample("01"))
    assert res.status == "done" and res.state["route"] == "auto"
    order = app.conn.execute("SELECT * FROM orders").fetchone()
    assert order["status"] == "confirmed" and order["po_number"] == "PO-2026-118"
    assert order["total"] == 5 * 899 + 2 * 4299 + 60  # livrare: 50 lei + 5 lei × 2 articole peste 5
    assert stock(app, "KIT-LOGI") == 115
    assert app.crm.activities[0]["kind"] == "confirmation"
    assert app.notifier.messages[0].startswith("✅ Agro-Nord SRL")
    reply = app.mailer.sent[0]
    assert reply["To"] == "aprovizionare@agronord.md" and reply["In-Reply-To"] == "<po118@agronord.md>"


def test_big_order_waits_for_human_then_approved(app, sample):
    res = app.handle_email(sample("02"))
    assert res.status == "waiting"
    assert res.interrupt["customer"] == "Clinica Vita Med SRL"
    assert app.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0  # nimic executat încă
    res = app.decide(res.run_id, "approve", by="ana.operator")
    assert res.status == "done" and res.state["approved_by"] == "ana.operator"
    assert app.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert "aprobat de ana.operator" in app.notifier.messages[-1]


def test_human_can_reject(app, sample):
    res = app.handle_email(sample("03"))
    assert "creditul disponibil" in res.interrupt["why"][0]
    res = app.decide(res.run_id, "reject", by="ana.operator", comment="sold restant")
    assert res.status == "done" and res.state["replied"] == "rejection"
    assert app.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert "sold restant" in app.crm.activities[-1]["summary"]


def test_invalid_decision_fails_the_run_without_side_effects(app, sample):
    res = app.handle_email(sample("02"))
    res = app.decide(res.run_id, "poate", by="x")
    assert res.status == "failed" and "decizie invalidă" in res.error
    assert app.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_blocked_customer_is_rejected_automatically(app, sample):
    res = app.handle_email(sample("04"))
    assert res.state["route"] == "reject" and res.state["replied"] == "rejection"
    assert app.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


@pytest.mark.parametrize("name,fragment", [("05", "TAB-SAM11"), ("07", "nu am identificat firma"),
                                           ("08", "nu am găsit produse")])
def test_clarification_branch(app, sample, name, fragment):
    res = app.handle_email(sample(name))
    assert res.state["route"] == "clarify"
    assert any(fragment in r for r in res.state["reasons"])
    assert fragment.split()[0] in app.mailer.sent[0].get_content() or "clarificări" in app.mailer.sent[0].get_content()


def test_out_of_stock_needs_approval_and_becomes_backorder(app, sample):
    res = app.handle_email(sample("06"))
    assert any("stoc insuficient" in r for r in res.interrupt["why"])
    res = app.decide(res.run_id, "approve", by="sef.depozit")
    assert res.state["order_status"] == "backorder"
    assert stock(app, "UPS-APC1500") == 0  # stocul nu devine negativ


def test_same_email_twice_does_not_duplicate_the_order(app, sample):
    first = app.handle_email(sample("01"))
    second = app.handle_email(sample("01"))
    assert first.run_id == second.run_id and second.status == "done"
    assert app.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert len(app.crm.activities) == 1 and len(app.notifier.messages) == 1


def test_audit_trail_per_run_and_chain_intact(app, sample):
    res = app.handle_email(sample("02"))
    app.decide(res.run_id, "approve", by="ana")
    nodes = [r["node"] for r in app.audit.for_run(res.run_id)]
    assert nodes[:6] == ["parse", "extract", "validate", "check_db", "route", "quote_shipping"]
    assert nodes.count("request_approval") == 2  # interrupt + aprobare
    assert nodes[-1] == "notify"
    assert app.audit.verify()
