"""`bg` command line."""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Optional

import typer

from bluegreen.aura_instance import InstanceGuard, aura_down, aura_reap, aura_status, aura_up
from bluegreen.config import load_settings
from bluegreen.cycle_aura import alias_probe, run_aura_cycle
from bluegreen.cycle_local import local_status, run_local_cycle
from bluegreen.doctor import run_doctor
from bluegreen.e2e import run_e2e
from bluegreen.errors import AppError
from bluegreen.generator import generate
from bluegreen.ledger import open_ledger
from bluegreen.pause_timing import run_pause_timing
from bluegreen.redact import RedactFormatter, redact
from bluegreen.report import latest_run_id, write_report
from bluegreen.watcher import Watcher

app = typer.Typer(no_args_is_help=True, add_completion=False)
local_app = typer.Typer(no_args_is_help=True, add_completion=False)
aura_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(local_app, name="local")
app.add_typer(aura_app, name="aura")


def _guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AppError as exc:
            typer.echo(redact(str(exc)), err=True)
            raise typer.Exit(code=exc.code)
        except typer.Exit:
            raise
        except Exception as exc:
            typer.echo(redact(f"{type(exc).__name__}: {exc}"), err=True)
            raise typer.Exit(code=1)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _require_yes(yes: bool, action: str) -> None:
    if not yes:
        from bluegreen.errors import ConfigError

        raise ConfigError(f"{action} can create or destroy a billed Aura instance. Re-run with --yes.")


@app.command()
@_guard
def doctor(
    aura: bool = typer.Option(False, "--aura", help="Also check the Aura API and a saved instance."),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Pre-flight checks: neo4j-admin help, local edition, and optional Aura auth."""
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    run_doctor(settings, ledger, aura=aura)


@app.command("gen")
@_guard
def gen(
    tenant: str = typer.Option(..., "--tenant"),
    cycle: str = typer.Option(..., "--cycle"),
    scale: Optional[float] = typer.Option(None, "--scale"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Generate CSVs and manifest.json. Skips when the manifest already matches."""
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    chosen = settings.scale if scale is None else scale
    with ledger.step("generate", target="local", tenant=tenant, cycle=cycle) as details:
        manifest, skipped = generate(settings.work_path(), tenant, cycle, settings.seed, chosen)
        details["skipped"] = skipped
        details["nodes"] = manifest["nodes"]
    print(f"generated {tenant} {cycle} skipped={skipped}")


@local_app.command("cycle")
@_guard
def local_cycle(
    tenant: str = typer.Option(..., "--tenant"),
    cycle: Optional[str] = typer.Option(None, "--cycle"),
    watch: bool = typer.Option(False, "--watch"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Run one Enterprise baseline cycle."""
    settings = load_settings()
    run_local_cycle(settings, tenant, cycle, watch=watch, run_id=run_id)


@local_app.command("status")
@_guard
def local_status_cmd() -> None:
    """SHOW DATABASES and SHOW ALIASES on neo4j-local."""
    local_status(load_settings())


@aura_app.command("up")
@_guard
def aura_up_cmd(
    yes: bool = typer.Option(False, "--yes"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Create a MultiDB Business Critical instance and wait until it is running."""
    _require_yes(yes, "aura up")
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    guard = InstanceGuard(settings, ledger.run_id)
    guard.install()
    try:
        aura_up(settings, ledger)
    except Exception:
        guard.teardown()
        raise


@aura_app.command("status")
@_guard
def aura_status_cmd() -> None:
    """Instance status, databases, aliases, and recorded routes."""
    aura_status(load_settings())


@aura_app.command("cycle")
@_guard
def aura_cycle_cmd(
    tenant: str = typer.Option(..., "--tenant"),
    cycle: Optional[str] = typer.Option(None, "--cycle"),
    watch: bool = typer.Option(False, "--watch"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Run one Aura dump/upload/switch cycle against the instance in the state file."""
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    guard = InstanceGuard(settings, ledger.run_id)
    guard.install()
    try:
        run_aura_cycle(settings, tenant, cycle, watch=watch, ledger=ledger)
    except Exception:
        guard.teardown()
        raise


@aura_app.command("alias-probe")
@_guard
def aura_alias_probe_cmd(
    probe_delete_aliased: bool = typer.Option(False, "--probe-delete-aliased"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Check alias support: create two scratch databases and flip an alias between them."""
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    alias_probe(settings, ledger, probe_delete_aliased=probe_delete_aliased)


@aura_app.command("pause-timing")
@_guard
def aura_pause_cmd(
    iterations: Optional[int] = typer.Option(None, "--iterations"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Time pause and resume of the whole instance (default one iteration)."""
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    guard = InstanceGuard(settings, ledger.run_id)
    guard.install()
    try:
        run_pause_timing(settings, ledger, iterations=iterations)
    except Exception:
        guard.teardown()
        raise


@aura_app.command("down")
@_guard
def aura_down_cmd(
    yes: bool = typer.Option(False, "--yes"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Delete the instance recorded in the state file."""
    _require_yes(yes, "aura down")
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    aura_down(settings, ledger)


@aura_app.command("reap")
@_guard
def aura_reap_cmd(yes: bool = typer.Option(False, "--yes")) -> None:
    """Delete prefix-matching instances older than AURA_MAX_RUNTIME_MINUTES."""
    _require_yes(yes, "aura reap")
    aura_reap(load_settings(), yes=True)


@app.command("watch")
@_guard
def watch_cmd(
    target: str = typer.Option(..., "--target", help="local or aura"),
    tenant: str = typer.Option(..., "--tenant"),
    duration: int = typer.Option(30, "--duration"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Standalone cutover probe. Prints the watcher file when it stops."""
    if target not in {"local", "aura"}:
        from bluegreen.errors import ConfigError

        raise ConfigError("--target must be local or aura")
    settings = load_settings()
    ledger = open_ledger(settings.work_path(), run_id)
    if target == "local":
        from bluegreen.cypher import local_driver
        from bluegreen.switch import AliasSwitch

        driver = local_driver(settings)
        strategy = AliasSwitch(driver)
    else:
        from bluegreen.cycle_aura import _driver, _require_state
        from bluegreen.switch import make_switch

        state = _require_state(settings)
        driver = _driver(state)
        strategy = make_switch(driver, settings.switch_mode, state.get("alias_supported"))
    path = ledger.run_dir / f"watcher-{target}-{tenant}.jsonl"
    watcher = Watcher(driver, lambda: strategy.resolve(tenant), path)
    watcher.start()
    try:
        time.sleep(duration)
    finally:
        watcher.stop()
        driver.close()
    print(path)


@app.command("report")
@_guard
def report_cmd(run_id: Optional[str] = typer.Option(None, "--run-id")) -> None:
    """Rebuild report.md from a ledger."""
    settings = load_settings()
    chosen = run_id or latest_run_id(settings.work_path())
    path = write_report(settings.work_path(), chosen)
    print(path)


@app.command("e2e")
@_guard
def e2e_cmd(yes: bool = typer.Option(False, "--yes")) -> None:
    """Local baseline, Aura cycles, optional pause/resume (PAUSE_ITERATIONS), report, teardown."""
    _require_yes(yes, "e2e")
    settings = load_settings()
    ledger = run_e2e(settings)
    print(f"e2e finished. run_id={ledger.run_id}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    app()
