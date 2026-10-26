from opsflow.cli import main


def test_demo_runs_every_sample(capsys):
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert out.count("rută=") == 11  # 8 e-mailuri + 3 aprobări
    assert "lanț intact" in out


def test_ingest_pending_approve_audit(tmp_path, capsys):
    db = str(tmp_path / "ops.db")
    sample = "samples/02-approval-vitamed.eml"
    main(["--db", db, "ingest", sample])
    run_id = capsys.readouterr().out.split()[0]
    main(["--db", db, "pending"])
    assert "Clinica Vita Med" in capsys.readouterr().out
    main(["--db", db, "approve", run_id, "--by", "ana"])
    assert "comanda #1" in capsys.readouterr().out
    main(["--db", db, "audit", run_id])
    assert "intact" in capsys.readouterr().out
