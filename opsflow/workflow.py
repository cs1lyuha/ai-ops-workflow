"""Graful complet și punctul de intrare pentru un e-mail."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from opsflow import nodes
from opsflow.db import AuditLog, Effects, SqliteCheckpoints
from opsflow.engine import END, Graph, RunResult, Runner
from opsflow.extract import Extractor, RuleExtractor
from opsflow.services import FakeCRM, FakeShippingAPI, MemoryNotifier, OutboxMailer


def build_graph() -> Graph:
    g = Graph("b2b-order-intake")
    for name in ("parse", "extract", "validate", "check_db", "route", "quote_shipping", "request_approval",
                 "create_order", "confirm_customer", "ask_clarification", "reply_rejection", "update_crm", "notify"):
        g.add_node(name, getattr(nodes, name))
    g.add_edge("parse", "extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", "check_db")
    g.add_edge("check_db", "route")
    g.add_router("route", lambda s: {"auto": "quote_shipping", "approval": "quote_shipping",
                                     "clarify": "ask_clarification", "reject": "reply_rejection"}[s["route"]],
                 {"quote_shipping", "ask_clarification", "reply_rejection"})
    g.add_router("quote_shipping", lambda s: "request_approval" if s["route"] == "approval" else "create_order",
                 {"request_approval", "create_order"})
    g.add_router("request_approval", lambda s: "reply_rejection" if s["route"] == "reject" else "create_order",
                 {"create_order", "reply_rejection"})
    g.add_edge("create_order", "confirm_customer")
    g.add_edge("confirm_customer", "update_crm")
    g.add_edge("ask_clarification", "update_crm")
    g.add_edge("reply_rejection", "update_crm")
    g.add_edge("update_crm", "notify")
    g.add_edge("notify", END)
    return g


@dataclass
class OpsApp:
    conn: sqlite3.Connection
    extractor: Extractor = field(default_factory=RuleExtractor)
    shipping: Any = field(default_factory=FakeShippingAPI)
    crm: Any = field(default_factory=FakeCRM)
    notifier: Any = field(default_factory=MemoryNotifier)
    mailer: Any = field(default_factory=OutboxMailer)

    def __post_init__(self) -> None:
        self.store = SqliteCheckpoints(self.conn)
        self.effects = Effects(self.conn)
        self.audit = AuditLog(self.conn)
        services = {"conn": self.conn, "extractor": self.extractor, "shipping": self.shipping, "crm": self.crm,
                    "notifier": self.notifier, "mailer": self.mailer, "effects": self.effects}
        self.runner = Runner(build_graph(), self.store, services, on_step=self.audit.record)

    @staticmethod
    def run_id_for(raw_email: str) -> str:
        """Același e-mail primit de două ori (re-livrare IMAP, webhook repetat) → același run."""
        from opsflow.extract import parse_email
        msg_id = parse_email(raw_email).message_id
        return "run-" + hashlib.sha256(msg_id.encode()).hexdigest()[:12]

    def handle_email(self, raw_email: str) -> RunResult:
        run_id = self.run_id_for(raw_email)
        if self.store.load(run_id) is not None:
            return self.runner.resume(run_id)
        return self.runner.start(run_id, {"raw_email": raw_email})

    def decide(self, run_id: str, decision: str, by: str, comment: str = "") -> RunResult:
        return self.runner.resume(run_id, {"approval": {"decision": decision, "by": by, "comment": comment}})

    def resume_failed(self) -> list[RunResult]:
        return [self.runner.resume(cp["run_id"]) for cp in self.store.by_status("failed")]
