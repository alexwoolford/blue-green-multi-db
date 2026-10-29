"""Deterministic synthetic graph CSVs. Same inputs produce the same bytes."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bluegreen.naming import cycle_number, validate_cycle, validate_tenant

GENERATOR_VERSION = "1.0.0"
SEGMENTS = ("retail", "commercial", "wealth")
STATUSES = ("open", "open", "open", "frozen", "closed")
CATEGORIES = ("grocery", "travel", "fuel", "software")


def stable_seed(*parts: object) -> int:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _format_scale(scale: float) -> str:
    text = f"{scale:.6f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _generated_at(seed: int, tenant: str, cycle: str) -> str:
    rng = random.Random(stable_seed(seed, tenant, cycle, "generated_at"))
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=rng.randint(0, 500_000))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _counts(scale: float) -> dict[str, int]:
    return {
        "customers": int(round(40_000 * scale)),
        "accounts": int(round(55_000 * scale)),
        "merchants": int(round(5_000 * scale)),
        "sent": int(round(150_000 * scale)),
        "paid": int(round(150_000 * scale)),
    }


def _population(seed: int, tenant: str, cycle: str, scale: float) -> dict[str, object]:
    counts = _counts(scale)
    n_c = counts["customers"]
    n_a = counts["accounts"]
    n_m = counts["merchants"]
    if n_c < 2 or n_a < 2 or n_m < 1:
        raise ValueError(f"SCALE={scale} is too small to build a connected graph")
    k = cycle_number(cycle)
    removed: set[int] = set()
    n_new_c = 0
    n_new_a = 0
    if k > 1:
        rng = random.Random(stable_seed(seed, tenant, cycle, "churn"))
        n_remove = min(max(1, int(round(0.03 * n_c))), n_c - 1)
        removed = set(rng.sample(range(n_c), n_remove))
        n_new_c = max(1, int(round(0.05 * n_c)))
        n_new_a = max(1, int(round(0.05 * n_a)))
    base_customers = [i for i in range(n_c) if i not in removed]
    new_customers = list(range(n_c * k, n_c * k + n_new_c))
    customers = base_customers + new_customers

    def base_owner(index: int) -> int:
        return index % n_c

    base_accounts = [i for i in range(n_a) if base_owner(i) not in removed]
    new_accounts = list(range(n_a * k, n_a * k + n_new_a))
    owners = {i: base_owner(i) for i in base_accounts}
    for offset, index in enumerate(new_accounts):
        owners[index] = customers[offset % len(customers)]
    accounts = base_accounts + new_accounts
    return {
        "customers": customers,
        "accounts": accounts,
        "owners": owners,
        "n_merchants": n_m,
        "n_sent": counts["sent"],
        "n_paid": counts["paid"],
    }


def _customer_row(seed: int, tenant: str, index: int) -> list[str]:
    rng = random.Random(stable_seed(seed, tenant, "customer", index))
    day = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=rng.randint(0, 2000))
    return [
        f"C-{tenant}-{index}",
        f"Customer {index}",
        rng.choice(SEGMENTS),
        day.date().isoformat(),
    ]


def _account_row(seed: int, tenant: str, index: int) -> list[str]:
    rng = random.Random(stable_seed(seed, tenant, "account", index))
    day = datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(days=rng.randint(0, 1500))
    return [f"A-{tenant}-{index}", day.date().isoformat(), rng.choice(STATUSES)]


def _merchant_row(seed: int, tenant: str, index: int) -> list[str]:
    rng = random.Random(stable_seed(seed, tenant, "merchant", index))
    return [f"M-{tenant}-{index}", f"Merchant {index}", rng.choice(CATEGORIES)]


class _GzipText:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._raw = path.open("wb")
        self._gz = gzip.GzipFile(filename="", mode="wb", fileobj=self._raw, mtime=0)
        self._text = io.TextIOWrapper(self._gz, encoding="utf-8", newline="\n", write_through=True)
        self.writer = csv.writer(self._text, lineterminator="\n")

    def close(self) -> None:
        self._text.close()

    def __enter__(self) -> "_GzipText":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inputs_match(manifest: dict[str, object], tenant: str, cycle: str, seed: int, scale: float) -> bool:
    try:
        same_scale = abs(float(manifest.get("scale")) - float(scale)) < 1e-9
    except (TypeError, ValueError):
        same_scale = False
    return (
        manifest.get("tenant") == tenant
        and manifest.get("cycle") == cycle
        and manifest.get("seed") == seed
        and same_scale
        and manifest.get("generator_version") == GENERATOR_VERSION
    )


def read_manifest(work: Path, tenant: str, cycle: str) -> dict[str, object]:
    path = work / "data" / tenant / cycle / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def generate(
    work: Path,
    tenant: str,
    cycle: str,
    seed: int,
    scale: float,
) -> tuple[dict[str, object], bool]:
    """Write CSVs and manifest.json. Returns (manifest, skipped)."""
    validate_tenant(tenant)
    validate_cycle(cycle)
    out = work / "data" / tenant / cycle
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = existing.get("files") or {}
        if _inputs_match(existing, tenant, cycle, seed, scale) and all(
            (out / name).exists() for name in files
        ):
            return existing, True

    pop = _population(seed, tenant, cycle, scale)
    customers: list[int] = pop["customers"]  # type: ignore[assignment]
    accounts: list[int] = pop["accounts"]  # type: ignore[assignment]
    owners: dict[int, int] = pop["owners"]  # type: ignore[assignment]
    n_merchants = int(pop["n_merchants"])
    n_sent = int(pop["n_sent"])
    n_paid = int(pop["n_paid"])
    n_nodes = len(customers) + len(accounts) + n_merchants + 1
    n_rels = len(accounts) + n_sent + n_paid
    generated_at = _generated_at(seed, tenant, cycle)

    with _GzipText(out / "customers.csv.gz") as handle:
        handle.writer.writerow(["customerId:ID(Customer)", "name", "segment", "createdAt:date"])
        for index in customers:
            handle.writer.writerow(_customer_row(seed, tenant, index))

    with _GzipText(out / "accounts.csv.gz") as handle:
        handle.writer.writerow(["accountId:ID(Account)", "openedAt:date", "status"])
        for index in accounts:
            handle.writer.writerow(_account_row(seed, tenant, index))

    with _GzipText(out / "merchants.csv.gz") as handle:
        handle.writer.writerow(["merchantId:ID(Merchant)", "name", "category"])
        for index in range(n_merchants):
            handle.writer.writerow(_merchant_row(seed, tenant, index))

    snapshot_path = out / "snapshot.csv"
    with snapshot_path.open("w", encoding="utf-8", newline="\n") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "snapshotId:ID(Snapshot)",
                "tenant",
                "cycle",
                "generatedAt:datetime",
                "seed:long",
                "scale:double",
                "expectedNodes:long",
                "expectedRels:long",
                "generatorVersion",
            ]
        )
        writer.writerow(
            [
                f"S-{tenant}-{cycle}",
                tenant,
                cycle,
                generated_at,
                str(seed),
                _format_scale(scale),
                str(n_nodes),
                str(n_rels),
                GENERATOR_VERSION,
            ]
        )

    with _GzipText(out / "owns.csv.gz") as handle:
        handle.writer.writerow([":START_ID(Customer)", ":END_ID(Account)"])
        for index in accounts:
            handle.writer.writerow([f"C-{tenant}-{owners[index]}", f"A-{tenant}-{index}"])

    sent_rng = random.Random(stable_seed(seed, tenant, cycle, "sent"))
    with _GzipText(out / "sent.csv.gz") as handle:
        handle.writer.writerow([":START_ID(Account)", ":END_ID(Account)", "amount:double", "ts:datetime"])
        origin = datetime(2024, 1, 1, tzinfo=timezone.utc)
        span = len(accounts)
        for _ in range(n_sent):
            left = sent_rng.randrange(span)
            right = sent_rng.randrange(span - 1)
            if right >= left:
                right += 1
            cents = sent_rng.randint(1, 100_000)
            moment = origin + timedelta(seconds=sent_rng.randint(0, 30_000_000))
            handle.writer.writerow(
                [
                    f"A-{tenant}-{accounts[left]}",
                    f"A-{tenant}-{accounts[right]}",
                    f"{cents / 100:.2f}",
                    moment.strftime("%Y-%m-%dT%H:%M:%S"),
                ]
            )

    paid_rng = random.Random(stable_seed(seed, tenant, cycle, "paid"))
    with _GzipText(out / "paid.csv.gz") as handle:
        handle.writer.writerow(
            [":START_ID(Account)", ":END_ID(Merchant)", "amount:double", "ts:datetime"]
        )
        origin = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for _ in range(n_paid):
            account = accounts[paid_rng.randrange(len(accounts))]
            merchant = paid_rng.randrange(n_merchants)
            cents = paid_rng.randint(1, 100_000)
            moment = origin + timedelta(seconds=paid_rng.randint(0, 30_000_000))
            handle.writer.writerow(
                [
                    f"A-{tenant}-{account}",
                    f"M-{tenant}-{merchant}",
                    f"{cents / 100:.2f}",
                    moment.strftime("%Y-%m-%dT%H:%M:%S"),
                ]
            )

    file_names = [
        "customers.csv.gz",
        "accounts.csv.gz",
        "merchants.csv.gz",
        "snapshot.csv",
        "owns.csv.gz",
        "sent.csv.gz",
        "paid.csv.gz",
    ]
    files = {
        name: {"bytes": (out / name).stat().st_size, "sha256": _sha256(out / name)}
        for name in file_names
    }
    manifest: dict[str, object] = {
        "tenant": tenant,
        "cycle": cycle,
        "seed": seed,
        "scale": float(scale),
        "generator_version": GENERATOR_VERSION,
        "generated_at": generated_at,
        "nodes": {
            "Customer": len(customers),
            "Account": len(accounts),
            "Merchant": n_merchants,
            "Snapshot": 1,
        },
        "relationships": {"OWNS": len(accounts), "SENT": n_sent, "PAID": n_paid},
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest, False
