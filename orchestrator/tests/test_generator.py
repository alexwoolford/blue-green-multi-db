import csv
import gzip
import hashlib
from pathlib import Path

from bluegreen.generator import generate


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def _rows(path: Path) -> list[list[str]]:
    if path.suffix == ".gz":
        handle = gzip.open(path, "rt", encoding="utf-8", newline="")
    else:
        handle = path.open("r", encoding="utf-8", newline="")
    with handle:
        return list(csv.reader(handle))


def _ids(path: Path, column: int = 0) -> set[str]:
    rows = _rows(path)
    return {row[column] for row in rows[1:] if row}


def test_determinism(tmp_path: Path):
    first, _ = generate(tmp_path / "a", "acme", "c0002", 42, 0.01)
    second, skipped = generate(tmp_path / "a", "acme", "c0002", 42, 0.01)
    other, _ = generate(tmp_path / "b", "acme", "c0002", 42, 0.01)
    assert skipped is True
    assert first["files"] == other["files"]
    for name in first["files"]:
        assert _sha(tmp_path / "a" / "data" / "acme" / "c0002" / name) == _sha(
            tmp_path / "b" / "data" / "acme" / "c0002" / name
        )
    manifest_a = tmp_path / "a" / "data" / "acme" / "c0002" / "manifest.json"
    manifest_b = tmp_path / "b" / "data" / "acme" / "c0002" / "manifest.json"
    assert _sha(manifest_a) == _sha(manifest_b)
    assert second["nodes"]["Customer"] != generate(tmp_path / "c", "acme", "c0001", 42, 0.01)[0]["nodes"]["Customer"]


def test_churn_changes_counts(tmp_path: Path):
    base, _ = generate(tmp_path, "acme", "c0001", 42, 0.01)
    nxt, _ = generate(tmp_path, "acme", "c0002", 42, 0.01)
    assert base["nodes"]["Customer"] == 400
    assert base["nodes"]["Account"] == 550
    assert base["nodes"]["Merchant"] == 50
    assert base["relationships"]["SENT"] == 1500
    assert nxt["nodes"]["Customer"] != base["nodes"]["Customer"]
    assert nxt["nodes"]["Account"] != base["nodes"]["Account"]
    assert nxt["relationships"]["OWNS"] == nxt["nodes"]["Account"]


def test_no_dangling_endpoints(tmp_path: Path):
    generate(tmp_path, "acme", "c0002", 42, 0.01)
    root = tmp_path / "data" / "acme" / "c0002"
    customers = _ids(root / "customers.csv.gz")
    accounts = _ids(root / "accounts.csv.gz")
    merchants = _ids(root / "merchants.csv.gz")
    owns = _rows(root / "owns.csv.gz")[1:]
    sent = _rows(root / "sent.csv.gz")[1:]
    paid = _rows(root / "paid.csv.gz")[1:]
    assert owns
    assert all(row[0] in customers and row[1] in accounts for row in owns)
    assert all(row[0] in accounts and row[1] in accounts and row[0] != row[1] for row in sent)
    assert all(row[0] in accounts and row[1] in merchants for row in paid)
    snapshots = _rows(root / "snapshot.csv")
    assert len(snapshots) == 2
    assert snapshots[1][1] == "acme"
    assert snapshots[1][2] == "c0002"
