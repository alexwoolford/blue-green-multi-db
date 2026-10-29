import json

from bluegreen.ledger import Ledger
from bluegreen.redact import add_secret, redact
from bluegreen.report import write_report


def test_ledger_redacts_and_report_renders(tmp_path):
    add_secret("super-secret-password")
    ledger = Ledger(tmp_path, "20261028-140000-7f3a")
    with ledger.step("upload", target="aura", tenant="acme", cycle="c0002") as details:
        details["dump_bytes"] = 10
        details["note"] = "password=super-secret-password"
    rows = ledger.read()
    assert rows[0]["ok"] is True
    assert rows[0]["step"] == "upload"
    assert "super-secret-password" not in json.dumps(rows)
    try:
        with ledger.step("verify", target="aura", tenant="acme", cycle="c0002"):
            raise RuntimeError("boom super-secret-password")
    except RuntimeError:
        pass
    failed = ledger.read()[-1]
    assert failed["ok"] is False
    assert failed["error_type"] == "RuntimeError"
    assert "super-secret-password" not in failed["error_msg"]
    assert redact("super-secret-password") == "***"

    report = write_report(tmp_path, ledger.run_id)
    text = report.read_text(encoding="utf-8")
    for heading in (
        "## 1. Run summary",
        "## 2. Answers",
        "## 3. Cycle timings",
        "## 4. Cutover",
        "## 5. Not verified by this run",
    ):
        assert heading in text
    assert "super-secret-password" not in text
    assert "Q1" in text
