from opsflow.db import AuditLog, Effects, SqliteCheckpoints, connect, seed
from opsflow.engine import END, Graph, Interrupt, Runner


def db():
    conn = connect()
    seed(conn)
    return conn


def test_seed_is_idempotent():
    conn = db()
    seed(conn)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 6


def test_checkpoints_round_trip_with_unicode():
    store = SqliteCheckpoints(db())
    store.save("r", step=2, next_node="x", state={"client": "Agro-Nord, Bălți"}, status="waiting",
               interrupt={"reason": "aprobare"})
    cp = store.load("r")
    assert cp["state"]["client"] == "Agro-Nord, Bălți" and cp["interrupt"]["reason"] == "aprobare"
    store.save("r", step=3, next_node=END, state={}, status="done")
    assert store.load("r")["status"] == "done"
    assert store.load("nope") is None


def test_by_status_lists_waiting_runs():
    store = SqliteCheckpoints(db())
    store.save("a", step=1, next_node="n", state={}, status="waiting")
    store.save("b", step=1, next_node="n", state={}, status="done")
    assert [r["run_id"] for r in store.by_status("waiting")] == ["a"]


def test_effects_run_once_per_key():
    fx = Effects(db())
    calls = []
    first = fx.once("k1", lambda key: calls.append(key) or {"id": 7})
    second = fx.once("k1", lambda key: calls.append(key) or {"id": 8})
    assert first == second == {"id": 7}
    assert calls == ["k1"] and fx.replayed == 1


def test_audit_chain_detects_tampering():
    conn = db()
    log = AuditLog(conn)
    for node in ("extract", "validate", "route"):
        log.record("r1", node, "ok", {"route": "auto"}, 1.5)
    assert log.verify()
    conn.execute("UPDATE audit SET outcome = 'error' WHERE node = 'validate'")
    assert not log.verify()


def test_runner_persists_in_sqlite_and_resumes_in_new_runner():
    conn = db()

    def gate(s, c):
        if "ok" not in s:
            raise Interrupt({"reason": "aprobare"})

    g = Graph("t")
    g.add_node("gate", gate)
    g.add_edge("gate", END)
    Runner(g, SqliteCheckpoints(conn)).start("r", {"x": 1})
    # „procesul a repornit”: runner nou, aceeași bază
    res = Runner(g, SqliteCheckpoints(conn)).resume("r", {"ok": True})
    assert res.status == "done" and res.state["x"] == 1
