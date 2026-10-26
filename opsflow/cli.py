"""Linia de comandă.

    python -m opsflow.cli demo                         # toate e-mailurile din samples/, într-o bază temporară
    python -m opsflow.cli ingest samples/*.eml         # procesează e-mailuri (bază: ops.db)
    python -m opsflow.cli pending                      # ce așteaptă aprobare
    python -m opsflow.cli approve <run_id> --by ana
    python -m opsflow.cli reject <run_id> --by ana --comment "sold restant"
    python -m opsflow.cli resume-failed                # reia run-urile căzute
    python -m opsflow.cli audit <run_id>               # pașii unui run + verificarea lanțului de hash-uri

`--claude` folosește ClaudeExtractor (cere ANTHROPIC_API_KEY). Cu
TELEGRAM_BOT_TOKEN și TELEGRAM_CHAT_ID, notificările pleacă pe Telegram.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from opsflow.db import connect, seed
from opsflow.services import ConsoleNotifier, OutboxMailer, TelegramNotifier
from opsflow.workflow import OpsApp

ROOT = Path(__file__).resolve().parent.parent


def make_app(db: str, claude: bool, quiet: bool = False) -> OpsApp:
    conn = connect(db)
    seed(conn)
    kwargs = {"mailer": OutboxMailer(None if quiet else ROOT / "outbox")}
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        kwargs["notifier"] = TelegramNotifier()
    elif not quiet:
        kwargs["notifier"] = ConsoleNotifier()
    if claude:
        from opsflow.extract import ClaudeExtractor
        kwargs["extractor"] = ClaudeExtractor()
    return OpsApp(conn, **kwargs)


def show(res) -> None:
    line = f"{res.run_id}  {res.status:<8} rută={res.state.get('route', '-'):<9}"
    if res.status == "waiting":
        line += f" ⏸ {res.interrupt['customer']}, {res.interrupt['total']:.0f} lei: " + "; ".join(res.interrupt["why"])
    elif res.status == "failed":
        line += f" ✖ la {res.next_node}: {res.error}"
    elif res.state.get("order_id"):
        line += f" comanda #{res.state['order_id']} ({res.state['order_status']}, {res.state['order_total']:.0f} lei)"
    else:
        line += " " + "; ".join(res.state.get("reasons", []))
    print(line)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsflow")
    p.add_argument("--db", default=str(ROOT / "ops.db"))
    p.add_argument("--claude", action="store_true", help="extragere cu Claude în loc de reguli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    ing = sub.add_parser("ingest")
    ing.add_argument("files", nargs="+")
    sub.add_parser("pending")
    for name in ("approve", "reject"):
        d = sub.add_parser(name)
        d.add_argument("run_id")
        d.add_argument("--by", required=True)
        d.add_argument("--comment", default="")
    sub.add_parser("resume-failed")
    au = sub.add_parser("audit")
    au.add_argument("run_id")
    args = p.parse_args(argv)

    if args.cmd == "demo":
        app = make_app(":memory:", args.claude, quiet=True)
        results = [app.handle_email(f.read_text(encoding="utf-8")) for f in sorted((ROOT / "samples").glob("*.eml"))]
        for res in results:
            show(res)
        print("\n→ operatorul aprobă tot ce așteaptă:\n")
        for res in results:
            if res.status == "waiting":
                show(app.decide(res.run_id, "approve", by="demo"))
        print("\nNotificări:", *app.notifier.messages, sep="\n  ")
        print(f"\nAudit: {app.conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0]} pași, "
              f"lanț {'intact' if app.audit.verify() else 'COMPROMIS'}")
        return 0

    app = make_app(args.db, args.claude)
    if args.cmd == "ingest":
        for f in args.files:
            show(app.handle_email(Path(f).read_text(encoding="utf-8")))
    elif args.cmd == "pending":
        waiting = app.store.by_status("waiting")
        if not waiting:
            print("Nimic nu așteaptă aprobare.")
        for cp in waiting:
            i = cp["interrupt"]
            print(f"{cp['run_id']}  {i['customer']}  {i['total']:.0f} lei  — " + "; ".join(i["why"]))
    elif args.cmd in ("approve", "reject"):
        show(app.decide(args.run_id, args.cmd, by=args.by, comment=args.comment))
    elif args.cmd == "resume-failed":
        for res in app.resume_failed() or []:
            show(res)
    elif args.cmd == "audit":
        for r in app.audit.for_run(args.run_id):
            print(f"{r['node']:<18} {r['outcome']:<10} {r['route'] or '':<9} {r['duration_ms']:6.1f} ms  {r['hash'][:12]}")
        print("lanț de hash-uri:", "intact" if app.audit.verify() else "COMPROMIS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
