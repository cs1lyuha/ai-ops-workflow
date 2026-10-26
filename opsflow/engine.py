"""Motor de workflow scris de mână: graf de noduri, muchii condiționate,
checkpoint după fiecare nod, întreruperi pentru aprobare umană și reluare
după crash.

Modelul e același ca la LangGraph (StateGraph / add_node /
add_conditional_edges / interrupt), doar fără framework, ca să se vadă ce
se întâmplă la fiecare pas:

    run ──▶ nod ──▶ checkpoint ──▶ router ──▶ nod ──▶ ... ──▶ END
                         │
              Interrupt ─┴─▶ status = waiting  (se reia cu resume(run_id, update))
              excepție  ────▶ status = failed   (se reia cu resume(run_id); nodul rulează din nou)

Regulă: un nod care are efecte în exterior (API, DB, mesaje) trebuie să fie
idempotent. Pentru asta, contextul oferă `ctx.once(...)` (vezi effects.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

END = "__end__"

State = dict[str, Any]


class Interrupt(Exception):
    """Ridicată de un nod care are nevoie de input uman. Payload-ul ajunge la operator."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("reason", "interrupt"))
        self.payload = payload


class GraphError(Exception):
    pass


@dataclass
class Context:
    run_id: str
    node: str
    services: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError:
            raise AttributeError(name) from None

    def key(self, suffix: str = "") -> str:
        """Cheie de idempotență stabilă pentru acest run și acest nod."""
        return f"{self.run_id}:{self.node}{':' + suffix if suffix else ''}"


NodeFn = Callable[[State, Context], "State | None"]
Router = Callable[[State], str]


class Graph:
    def __init__(self, name: str) -> None:
        self.name = name
        self.nodes: dict[str, NodeFn] = {}
        self.edges: dict[str, str] = {}
        self.routers: dict[str, tuple[Router, frozenset[str]]] = {}
        self.entry: str | None = None

    def add_node(self, name: str, fn: NodeFn) -> "Graph":
        if name in self.nodes or name == END:
            raise GraphError(f"nod duplicat sau rezervat: {name}")
        self.nodes[name] = fn
        if self.entry is None:
            self.entry = name
        return self

    def add_edge(self, src: str, dst: str) -> "Graph":
        self._check_free(src)
        self.edges[src] = dst
        return self

    def add_router(self, src: str, router: Router, targets: set[str]) -> "Graph":
        self._check_free(src)
        self.routers[src] = (router, frozenset(targets))
        return self

    def _check_free(self, src: str) -> None:
        if src in self.edges or src in self.routers:
            raise GraphError(f"nodul {src} are deja o muchie de ieșire")

    def validate(self) -> None:
        targets = set(self.edges.values()) | {t for _, ts in self.routers.values() for t in ts}
        unknown = targets - set(self.nodes) - {END}
        if unknown:
            raise GraphError(f"muchii către noduri inexistente: {sorted(unknown)}")
        dangling = set(self.nodes) - set(self.edges) - set(self.routers)
        if dangling:
            raise GraphError(f"noduri fără ieșire: {sorted(dangling)}")

    def next_node(self, node: str, state: State) -> str:
        if node in self.edges:
            return self.edges[node]
        router, targets = self.routers[node]
        choice = router(state)
        if choice not in targets:
            raise GraphError(f"router-ul lui {node} a ales {choice!r}, permis: {sorted(targets)}")
        return choice


class CheckpointStore(Protocol):
    def save(self, run_id: str, *, step: int, next_node: str, state: State, status: str,
             error: str = "", interrupt: dict | None = None) -> None: ...

    def load(self, run_id: str) -> dict[str, Any] | None: ...


class MemoryCheckpoints:
    """Implementare în memorie, pentru teste."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.history: list[tuple[str, int, str, str]] = []

    def save(self, run_id, *, step, next_node, state, status, error="", interrupt=None):
        import copy
        self.runs[run_id] = {"step": step, "next_node": next_node, "state": copy.deepcopy(state),
                             "status": status, "error": error, "interrupt": interrupt}
        self.history.append((run_id, step, next_node, status))

    def load(self, run_id):
        return self.runs.get(run_id)


@dataclass
class RunResult:
    run_id: str
    status: str            # done | waiting | failed
    state: State
    next_node: str
    steps: int
    interrupt: dict[str, Any] | None = None
    error: str = ""


AuditHook = Callable[[str, str, str, State, float], None]


class Runner:
    def __init__(self, graph: Graph, store: CheckpointStore, services: dict[str, Any] | None = None,
                 *, max_steps: int = 50, on_step: AuditHook | None = None) -> None:
        graph.validate()
        self.graph = graph
        self.store = store
        self.services = services or {}
        self.max_steps = max_steps
        self.on_step = on_step

    def start(self, run_id: str, state: State) -> RunResult:
        if self.store.load(run_id) is not None:
            raise GraphError(f"run-ul {run_id} există deja; folosește resume()")
        assert self.graph.entry is not None
        self.store.save(run_id, step=0, next_node=self.graph.entry, state=state, status="running")
        return self._loop(run_id, dict(state), self.graph.entry, 0)

    def resume(self, run_id: str, update: State | None = None) -> RunResult:
        cp = self.store.load(run_id)
        if cp is None:
            raise GraphError(f"run necunoscut: {run_id}")
        if cp["status"] == "done":
            return RunResult(run_id, "done", cp["state"], END, cp["step"])
        state = dict(cp["state"])
        state.update(update or {})
        return self._loop(run_id, state, cp["next_node"], cp["step"])

    def _loop(self, run_id: str, state: State, node: str, step: int) -> RunResult:
        while node != END:
            if step >= self.max_steps:
                self.store.save(run_id, step=step, next_node=node, state=state, status="failed",
                                error="max_steps depășit")
                return RunResult(run_id, "failed", state, node, step, error="max_steps depășit")
            ctx = Context(run_id, node, self.services)
            t0 = time.perf_counter()
            try:
                out = self.graph.nodes[node](state, ctx)
            except Interrupt as intr:
                self.store.save(run_id, step=step, next_node=node, state=state, status="waiting",
                                interrupt=intr.payload)
                self._audit(run_id, node, "interrupt", state, t0)
                return RunResult(run_id, "waiting", state, node, step, interrupt=intr.payload)
            except Exception as err:  # crash: nodul se reia la resume()
                msg = f"{type(err).__name__}: {err}"
                self.store.save(run_id, step=step, next_node=node, state=state, status="failed", error=msg)
                self._audit(run_id, node, "error", state, t0)
                return RunResult(run_id, "failed", state, node, step, error=msg)
            if out:
                state.update(out)
            step += 1
            nxt = self.graph.next_node(node, state)
            self.store.save(run_id, step=step, next_node=nxt, state=state,
                            status="done" if nxt == END else "running")
            self._audit(run_id, node, "ok", state, t0)
            node = nxt
        return RunResult(run_id, "done", state, END, step)

    def _audit(self, run_id: str, node: str, outcome: str, state: State, t0: float) -> None:
        if self.on_step:
            self.on_step(run_id, node, outcome, state, (time.perf_counter() - t0) * 1000)
