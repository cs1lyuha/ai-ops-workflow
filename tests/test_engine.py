import pytest

from opsflow.engine import END, Graph, GraphError, Interrupt, MemoryCheckpoints, Runner


def linear():
    g = Graph("t")
    g.add_node("a", lambda s, c: {"trace": s.get("trace", []) + ["a"]})
    g.add_node("b", lambda s, c: {"trace": s["trace"] + ["b"]})
    g.add_edge("a", "b").add_edge("b", END)
    return g


def test_linear_run_and_checkpoints():
    store = MemoryCheckpoints()
    res = Runner(linear(), store).start("r1", {})
    assert res.status == "done" and res.state["trace"] == ["a", "b"]
    assert [h[3] for h in store.history] == ["running", "running", "done"]


def test_router_picks_branch():
    g = Graph("t")
    g.add_node("start", lambda s, c: None)
    g.add_node("small", lambda s, c: {"path": "small"})
    g.add_node("big", lambda s, c: {"path": "big"})
    g.add_router("start", lambda s: "big" if s["n"] > 10 else "small", {"small", "big"})
    g.add_edge("small", END).add_edge("big", END)
    assert Runner(g, MemoryCheckpoints()).start("x", {"n": 50}).state["path"] == "big"
    assert Runner(g, MemoryCheckpoints()).start("y", {"n": 1}).state["path"] == "small"


def test_router_cannot_jump_outside_declared_targets():
    g = Graph("t")
    g.add_node("a", lambda s, c: None)
    g.add_node("b", lambda s, c: None)
    g.add_router("a", lambda s: "nowhere", {"b"})
    g.add_edge("b", END)
    with pytest.raises(GraphError):
        Runner(g, MemoryCheckpoints()).start("r", {})


def test_validate_catches_missing_targets_and_dangling_nodes():
    g = Graph("t")
    g.add_node("a", lambda s, c: None)
    g.add_edge("a", "ghost")
    with pytest.raises(GraphError):
        g.validate()
    g2 = Graph("t2")
    g2.add_node("a", lambda s, c: None)
    with pytest.raises(GraphError):
        g2.validate()


def test_interrupt_then_resume_with_update():
    def approve(s, c):
        if "decision" not in s:
            raise Interrupt({"reason": "aprobare", "amount": 100})
        return {"approved": s["decision"] == "yes"}

    g = Graph("t")
    g.add_node("approve", approve)
    g.add_edge("approve", END)
    store = MemoryCheckpoints()
    runner = Runner(g, store)
    res = runner.start("r", {})
    assert res.status == "waiting" and res.interrupt["amount"] == 100
    assert store.load("r")["status"] == "waiting"
    res = runner.resume("r", {"decision": "yes"})
    assert res.status == "done" and res.state["approved"] is True


def test_crash_is_resumed_from_failed_node_only():
    calls = {"a": 0, "b": 0}

    def a(s, c):
        calls["a"] += 1

    def b(s, c):
        calls["b"] += 1
        if calls["b"] == 1:
            raise ConnectionError("API căzut")
        return {"ok": True}

    g = Graph("t")
    g.add_node("a", a).add_node("b", b)
    g.add_edge("a", "b").add_edge("b", END)
    runner = Runner(g, MemoryCheckpoints())
    first = runner.start("r", {})
    assert first.status == "failed" and "API căzut" in first.error
    second = runner.resume("r")
    assert second.status == "done"
    assert calls == {"a": 1, "b": 2}  # nodul a NU a rulat din nou


def test_max_steps_guards_cycles():
    g = Graph("t")
    g.add_node("loop", lambda s, c: None)
    g.add_edge("loop", "loop")
    res = Runner(g, MemoryCheckpoints(), max_steps=5).start("r", {})
    assert res.status == "failed" and res.steps == 5


def test_start_twice_is_rejected_and_done_resume_is_noop():
    runner = Runner(linear(), MemoryCheckpoints())
    runner.start("r", {})
    with pytest.raises(GraphError):
        runner.start("r", {})
    assert runner.resume("r").status == "done"


def test_context_key_and_services():
    seen = {}

    def node(s, c):
        seen["key"] = c.key("x")
        seen["svc"] = c.mailer

    g = Graph("t")
    g.add_node("n", node)
    g.add_edge("n", END)
    Runner(g, MemoryCheckpoints(), {"mailer": "M"}).start("run7", {})
    assert seen == {"key": "run7:n:x", "svc": "M"}


def test_audit_hook_sees_every_step():
    events = []
    Runner(linear(), MemoryCheckpoints(), on_step=lambda r, n, o, s, ms: events.append((n, o))).start("r", {})
    assert events == [("a", "ok"), ("b", "ok")]
