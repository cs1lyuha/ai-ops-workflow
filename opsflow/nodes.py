"""Nodurile workflow-ului. Fiecare nod primește starea și contextul și întoarce
doar câmpurile pe care le schimbă.

Ordinea: parse → extract → validate → check_db → route → (quote → [approval] → create_order →
confirm) | clarify | reject → update_crm → notify.

Regulile de business stau aici, în cod, nu în prompt: modelul doar citește
e-mailul, iar codul hotărăște dacă o comandă trece automat.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import ValidationError

from opsflow.engine import Context, Interrupt, State
from opsflow.extract import PurchaseOrder, parse_email

APPROVAL_THRESHOLD = 20_000.0  # lei; peste această sumă decide un om
TRUSTED_AFTER = 3              # comenzi plătite la timp după care clientul e „de încredere”


def parse(state: State, ctx: Context) -> State:
    email = parse_email(state["raw_email"])
    return {"email": email.model_dump(mode="json")}


def extract(state: State, ctx: Context) -> State:
    from opsflow.extract import Email

    catalog = [dict(r) for r in ctx.conn.execute("SELECT sku, name FROM products ORDER BY sku")]
    po = ctx.extractor.extract(Email(**state["email"]), catalog)
    return {"order": po.model_dump(mode="json"), "extractor": ctx.extractor.name}


def validate(state: State, ctx: Context) -> State:
    problems: list[str] = []
    try:
        po = PurchaseOrder.model_validate(state["order"])
    except ValidationError as err:
        return {"problems": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors()]}
    if not po.lines:
        problems.append("nu am găsit produse cu cod SKU în e-mail")
    received = state["email"].get("received")
    if po.requested_date and received and po.requested_date < date.fromisoformat(received):
        problems.append(f"data de livrare {po.requested_date} este în trecut")
    if not po.delivery_city:
        problems.append("lipsește orașul de livrare")
    return {"problems": problems}


def check_db(state: State, ctx: Context) -> State:
    order, conn = state["order"], ctx.conn
    customer = None
    if order.get("customer_idno"):
        customer = conn.execute("SELECT * FROM customers WHERE idno = ?", (order["customer_idno"],)).fetchone()
    if customer is None:  # fără IDNO: încercăm după domeniul expeditorului
        customer = conn.execute("SELECT * FROM customers WHERE email_domain = ?",
                                (state["email"]["sender"].rsplit("@", 1)[-1],)).fetchone()
    unknown, shortages, total, lines = [], [], 0.0, []
    for line in order.get("lines", []):
        product = conn.execute("SELECT * FROM products WHERE sku = ?", (line["sku"],)).fetchone()
        if product is None:
            unknown.append(line["sku"])
            continue
        total += product["price"] * line["qty"]
        lines.append({"sku": line["sku"], "name": product["name"], "qty": line["qty"], "price": product["price"]})
        if product["stock"] < line["qty"]:
            shortages.append({"sku": line["sku"], "requested": line["qty"], "stock": product["stock"]})
    facts: dict[str, Any] = {"unknown_skus": unknown, "shortages": shortages, "total": round(total, 2),
                             "lines": lines, "customer": None}
    if customer is not None:
        facts["customer"] = {
            "idno": customer["idno"], "name": customer["name"], "blocked": bool(customer["blocked"]),
            "available_credit": round(customer["credit_limit"] - customer["balance"], 2),
            "trusted": customer["orders_paid_on_time"] >= TRUSTED_AFTER,
        }
    return {"facts": facts}


def route(state: State, ctx: Context) -> State:
    facts, problems = state["facts"], list(state.get("problems", []))
    customer = facts["customer"]
    if customer and customer["blocked"]:
        return {"route": "reject", "reasons": ["clientul are contul blocat (datorii restante)"]}
    if customer is None:
        problems.append("nu am identificat firma (IDNO lipsă sau necunoscut)")
    if facts["unknown_skus"]:
        problems.append("coduri necunoscute: " + ", ".join(facts["unknown_skus"]))
    if problems:
        return {"route": "clarify", "reasons": problems}
    reasons = []
    if facts["total"] > APPROVAL_THRESHOLD:
        reasons.append(f"valoare {facts['total']:.0f} lei > pragul de {APPROVAL_THRESHOLD:.0f} lei")
    if facts["total"] > customer["available_credit"]:
        reasons.append(f"depășește creditul disponibil ({customer['available_credit']:.0f} lei)")
    if facts["shortages"]:
        reasons.append("stoc insuficient: " + ", ".join(f"{s['sku']} ({s['stock']}/{s['requested']})"
                                                         for s in facts["shortages"]))
    if not customer["trusted"]:
        reasons.append("client nou (mai puțin de 3 comenzi plătite la timp)")
    return {"route": "approval" if reasons else "auto", "reasons": reasons}


def quote_shipping(state: State, ctx: Context) -> State:
    items = sum(line["qty"] for line in state["facts"]["lines"])
    quote = ctx.effects.once(ctx.key(), lambda key: ctx.shipping.quote(state["order"]["delivery_city"], items, key))
    return {"shipping": quote}


def request_approval(state: State, ctx: Context) -> State:
    decision = state.get("approval")
    if decision is None:
        raise Interrupt({
            "reason": "aprobare necesară",
            "customer": state["facts"]["customer"]["name"],
            "total": state["facts"]["total"],
            "why": state["reasons"],
        })
    if decision.get("decision") not in {"approve", "reject"}:
        raise ValueError(f"decizie invalidă: {decision!r}")
    if decision["decision"] == "reject":
        return {"route": "reject", "reasons": state["reasons"] + [f"respinsă de {decision.get('by', 'operator')}: "
                                                                  f"{decision.get('comment', '')}".strip()]}
    return {"approved_by": decision.get("by", "operator")}


def create_order(state: State, ctx: Context) -> State:
    """Idempotent prin `orders.run_id UNIQUE`: dacă rulează de două ori, a doua oară doar citește."""
    conn, facts, run_id = ctx.conn, state["facts"], ctx.run_id
    existing = conn.execute("SELECT id FROM orders WHERE run_id = ?", (run_id,)).fetchone()
    if existing:
        return {"order_id": existing["id"]}
    status = "backorder" if facts["shortages"] else "confirmed"
    total = facts["total"] + state["shipping"]["price"]
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO orders (run_id, customer_idno, po_number, total, status, created_at) "
            "VALUES (?,?,?,?,?, strftime('%s','now'))",
            (run_id, facts["customer"]["idno"], state["order"].get("po_number") or "-", total, status))
        order_id = cur.lastrowid
        for line in facts["lines"]:
            conn.execute("INSERT INTO order_items VALUES (?,?,?,?)", (order_id, line["sku"], line["qty"], line["price"]))
            conn.execute("UPDATE products SET stock = MAX(0, stock - ?) WHERE sku = ?", (line["qty"], line["sku"]))
        conn.execute("UPDATE customers SET balance = balance + ? WHERE idno = ?", (total, facts["customer"]["idno"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"order_id": order_id, "order_status": status, "order_total": total}


def _reply(state: State, ctx: Context, subject: str, body: str) -> None:
    email = state["email"]
    ctx.effects.once(ctx.key(), lambda key: ctx.mailer.send(email["sender"], subject, body, key,
                                                           in_reply_to=email["message_id"]) or True)


def confirm_customer(state: State, ctx: Context) -> State:
    lines = "\n".join(f"- {l['qty']} x {l['name']} ({l['sku']})" for l in state["facts"]["lines"])
    ship = state["shipping"]
    _reply(state, ctx, f"Confirmare comandă #{state['order_id']}",
           f"Comanda dumneavoastră a fost înregistrată:\n{lines}\n\n"
           f"Total: {state['order_total']:.2f} lei (livrare {ship['price']} lei, {ship['carrier']}, "
           f"{ship['eta_days']} zile lucrătoare).")
    return {"replied": "confirmation"}


def ask_clarification(state: State, ctx: Context) -> State:
    reasons = "\n".join(f"- {r}" for r in state["reasons"])
    _reply(state, ctx, "Avem nevoie de câteva detalii pentru comanda dvs.",
           f"Bună ziua,\n\nca să procesăm comanda avem nevoie de clarificări:\n{reasons}\n\n"
           "Vă rugăm să răspundeți la acest e-mail cu codurile produselor (SKU), IDNO-ul firmei și adresa de livrare.")
    return {"replied": "clarification"}


def reply_rejection(state: State, ctx: Context) -> State:
    _reply(state, ctx, "Comanda nu poate fi procesată",
           "Bună ziua,\n\nnu putem procesa comanda în acest moment. Un manager de cont vă va contacta.")
    return {"replied": "rejection"}


def update_crm(state: State, ctx: Context) -> State:
    customer = (state.get("facts") or {}).get("customer") or {}
    who = customer.get("name") or state["email"]["sender"]
    summary = {
        "confirmation": f"Comandă #{state.get('order_id')} creată, {state.get('order_total', 0):.0f} lei",
        "clarification": "Cerere de clarificare trimisă: " + "; ".join(state.get("reasons", [])),
        "rejection": "Comandă respinsă: " + "; ".join(state.get("reasons", [])),
    }[state["replied"]]
    ctx.effects.once(ctx.key(), lambda key: ctx.crm.log_activity(who, state["replied"], summary, key))
    return {"crm_logged": True}


def notify(state: State, ctx: Context) -> State:
    icon = {"confirmation": "✅", "clarification": "❓", "rejection": "⛔"}[state["replied"]]
    customer = (state.get("facts") or {}).get("customer") or {}
    text = (f"{icon} {customer.get('name') or state['email']['sender']} · "
            f"{state.get('order', {}).get('po_number') or 'fără PO'} · rută: {state['route']}")
    if state.get("approved_by"):
        text += f" · aprobat de {state['approved_by']}"
    ctx.effects.once(ctx.key(), lambda key: ctx.notifier.send(text, key) or True)
    return {"notified": True}
