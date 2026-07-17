#!/usr/bin/env python3
"""
SAP Commerce jdk21 Upgrade Agent — abstract, version-independent.

Usage:
    # Abstract mode (release-note-driven, recommended):
    python main.py --env local --release-notes knowledge/release-notes.txt

    # Check only the 3 big gates (no release notes needed):
    python main.py --env local --gates-only

    # Classic phase-by-phase mode:
    python main.py --env local --start-phase 3
    python main.py --env local --phase 4
    python main.py --env local --dry-run

    # Specific phase + environment:
    python main.py --env local --phase 3 --release-notes path/to/notes.txt
"""
import logging
import os
import sys
import urllib3
from pathlib import Path
from typing import Optional

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import click
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from hac_client import HACClient
from local_server import LocalServer
from log_reader import LocalLogReader
from healer import ErrorClassifier, HealingExecutor, EscalationHandler
from orchestrator import StepRunner, UpgradeContext, UpgradePipeline, StepStatus
from gate_checker import GateChecker
from gate_reporter import GateReporter
from pipeline import AbstractUpgradePipeline

console = Console()

AGENT_DIR = Path(__file__).parent
KNOWLEDGE_DIR = AGENT_DIR / "knowledge"
CONFIG_DIR = AGENT_DIR / "config"
HEALING_MAP = AGENT_DIR / "healing_map.yaml"
UPGRADE_LOG_TEMPLATE = KNOWLEDGE_DIR / "upgrade-log-template.md"

_DEFAULT_UPGRADE_LOG = str(UPGRADE_LOG_TEMPLATE)


# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


# ------------------------------------------------------------------
# Config loading
# ------------------------------------------------------------------

def load_env_config(env_name: str) -> dict:
    config_path = CONFIG_DIR / f"{env_name.lower()}.yaml"
    if not config_path.exists():
        console.print(f"[red]Config not found: {config_path}[/red]")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return _expand_env_vars(config)


def _expand_env_vars(obj):
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    return obj


# ------------------------------------------------------------------
# Client factory
# ------------------------------------------------------------------

def build_clients(config: dict) -> tuple[HACClient, LocalServer, LocalLogReader]:
    hac_cfg = config.get("hac", {})
    local_cfg = config.get("local_server", {})
    log_cfg = config.get("log", {})

    hac_url = hac_cfg.get("base_url", "https://localhost:9002")
    hybris_dir = local_cfg.get("hybris_dir", os.environ.get("HYBRIS_HOME", ""))

    hac = HACClient(
        base_url=hac_url,
        username=hac_cfg.get("username", "admin"),
        password=os.environ.get(hac_cfg.get("password_env", "HAC_PASSWORD_LOCAL"), ""),
        verify_ssl=config.get("verify_ssl", False),
    )

    local_server = LocalServer(
        hybris_dir=hybris_dir,
        hac_url=local_cfg.get("hac_url", hac_url),
        verify_ssl=config.get("verify_ssl", False),
    )

    default_log_dir = os.path.join(hybris_dir, "data", "log") if hybris_dir else ""
    log_dir = log_cfg.get("dir", default_log_dir)
    log_reader = LocalLogReader(log_dir=log_dir)

    return hac, local_server, log_reader


# ------------------------------------------------------------------
# Gate reporter factory
# ------------------------------------------------------------------

def _build_reporter(config: dict) -> Optional["GateReporter"]:
    """Build a GateReporter from config, or None if reports_root is not set."""
    reports_root = config.get("reports_root", "")
    if not reports_root:
        return None
    from_ver = config.get("from_version", "unknown")
    to_ver = config.get("platform_version", config.get("target_version", "unknown"))
    try:
        return GateReporter(
            reports_root=reports_root,
            from_version=from_ver,
            to_version=to_ver,
        )
    except Exception as exc:
        logger.warning(f"Could not initialise GateReporter: {exc}")
        return None


# ------------------------------------------------------------------
# Result display (classic mode)
# ------------------------------------------------------------------

def print_phase_results(results: dict):
    table = Table(title="Upgrade Pipeline Results", show_header=True, header_style="bold")
    table.add_column("Step", style="cyan", width=8)
    table.add_column("Status", width=12)
    table.add_column("Output / Error", width=80)

    for phase_key, phase_results in results.get("results", {}).items():
        for r in phase_results:
            status_style = {
                StepStatus.PASS: "green",
                StepStatus.FAIL: "red",
                StepStatus.SKIP: "yellow",
                StepStatus.ESCALATED: "red bold",
            }.get(r.status, "white")
            detail = r.output if r.status == StepStatus.PASS else r.error
            table.add_row(
                r.step,
                f"[{status_style}]{r.status}[/{status_style}]",
                (detail or "")[:100],
            )

    console.print(table)

    overall = results.get("status", "UNKNOWN")
    if overall == "SUCCESS":
        console.print("\n[bold green]UPGRADE PIPELINE COMPLETE[/bold green]")
    elif overall == "ESCALATED":
        phase = results.get("halted_at_phase", "?")
        console.print(f"\n[bold red]PIPELINE HALTED — Human action required (Phase {phase})[/bold red]")
    else:
        failed = results.get("failed_steps", [])
        console.print(f"\n[bold red]PIPELINE FAILED — Steps: {failed}[/bold red]")


# ------------------------------------------------------------------
# Gates-only display
# ------------------------------------------------------------------

def print_gates_result(gates_result):
    for gate in gates_result.gates:
        color = "green" if gate.passed else "red"
        icon = "✅" if gate.passed else "❌"
        detail = gate.output if gate.passed else (gate.error or "")
        console.print(f"[{color}]{icon} Gate {gate.gate_id} [{gate.name}]: {detail[:120]}[/{color}]")

    if gates_result.all_pass:
        console.print("\n[bold green]ALL 3 GATES GREEN ✅[/bold green]")
    else:
        console.print(f"\n[bold red]{len(gates_result.failures)} gate(s) failing ❌[/bold red]")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

@click.command()
@click.option("--env", default="local", help="Config to use (config/<env>.yaml)")
@click.option("--release-notes", default=None, help="Path to SAP release notes (.md or .txt)")
@click.option("--upgrade-log", default=None, help="Path to upgrade-LOG.md (default: knowledge/upgrade-log-template.md)")
@click.option("--custom-code-root", default=None, help="Path to core-customize root for code navigation")
@click.option("--gates-only", is_flag=True, help="Check only the 3 big gates (no release notes needed)")
@click.option("--max-iterations", default=3, type=int, help="Max pipeline iterations (abstract mode)")
@click.option("--skip-build", is_flag=True, help="Skip Gate 1 build check")
@click.option("--start-phase", default=0, type=int, help="Classic mode: start from this phase (0-5)")
@click.option("--phase", default=None, type=int, help="Classic mode: run only this phase (0-5)")
@click.option("--dry-run", is_flag=True, help="Validate config and print plan without executing")
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging")
@click.option("--manifest", default=None, help="Path to manifest.json (overrides config)")
@click.option("--env-file", default=".env", help="Path to .env file with secrets")
def main(
    env, release_notes, upgrade_log, custom_code_root,
    gates_only, max_iterations, skip_build,
    start_phase, phase, dry_run, verbose, manifest, env_file,
):
    """SAP Commerce Upgrade Agent — version-independent, release-note-driven."""
    setup_logging(verbose)
    load_dotenv(env_file)

    console.print(f"\n[bold cyan]SAP Commerce Upgrade Agent[/bold cyan]")
    console.print(f"Environment: [bold]{env.upper()}[/bold]")

    config = load_env_config(env)
    console.print(f"Platform: [bold]{config.get('platform_version', 'unknown')}[/bold]")
    console.print(f"Mode: [bold]{config.get('deploy_mode', 'unknown')}[/bold]\n")

    if dry_run:
        console.print("[yellow]DRY RUN — configuration validated, no actions taken[/yellow]")
        _print_dry_run_plan(config, release_notes, gates_only, phase, start_phase)
        return

    hac, local_server, log_reader = build_clients(config)
    context = UpgradeContext(
        env=env,
        config=config,
        manifest_path=manifest or config.get("manifest_path", ""),
    )

    migration_log_path = str(UPGRADE_LOG_TEMPLATE) if UPGRADE_LOG_TEMPLATE.exists() else None
    classifier = ErrorClassifier(
        healing_map_path=str(HEALING_MAP),
        migration_log_path=migration_log_path,
    )
    healer = HealingExecutor(hac, local_server, log_reader, config)
    escalation_handler = EscalationHandler(migration_log_path=migration_log_path)

    # ------------------------------------------------------------------
    # Mode 1: Abstract pipeline (release-note-driven)
    # ------------------------------------------------------------------
    if release_notes or gates_only:
        notes_path = release_notes or str(KNOWLEDGE_DIR / "release-notes.txt")
        log_path = upgrade_log or _DEFAULT_UPGRADE_LOG

        gate_checker = GateChecker(local_server, hac, log_reader, config,
                                    reporter=_build_reporter(config))

        if gates_only:
            console.print("[cyan]Checking 3 gates only (no release notes processing)[/cyan]\n")
            result = gate_checker.check_all(skip_build=skip_build)
            print_gates_result(result)
            sys.exit(0 if result.all_pass else 1)

        console.print(f"[cyan]Abstract pipeline — release notes: {notes_path}[/cyan]")
        console.print(f"[cyan]Upgrade log: {log_path}[/cyan]\n")

        abstract_pipeline = AbstractUpgradePipeline(
            release_notes_path=notes_path,
            upgrade_log_path=log_path,
            gate_checker=gate_checker,
            classifier=classifier,
            healer=healer,
            escalation=escalation_handler,
            context=context,
            custom_code_root=custom_code_root or "",
            max_gate_retries=3,
        )

        result = abstract_pipeline.run(
            max_iterations=max_iterations,
            skip_build=skip_build,
        )

        console.print(f"\n{'='*60}")
        color = "green" if result.succeeded else "red"
        console.print(f"[bold {color}]{result.message}[/bold {color}]")
        console.print(f"\n[dim]{result.final_gate_summary}[/dim]")

        if result.total_fixes:
            console.print(f"\n[cyan]Fixes applied ({len(result.total_fixes)}):[/cyan]")
            for fix in result.total_fixes:
                console.print(f"  • {fix}")

        if result.total_escalations:
            console.print(f"\n[red]Escalations ({len(result.total_escalations)}):[/red]")
            for esc in result.total_escalations:
                console.print(f"  ⚠ {esc}")

        if gate_checker.reporter:
            last_iter = result.iterations[-1] if result.iterations else None
            gate_checker.reporter.write_summary(
                last_iter.gates if last_iter else None,
                iterations=len(result.iterations),
                fixes=result.total_fixes,
                escalations=result.total_escalations,
            )

        sys.exit(0 if result.succeeded else 1)

    # ------------------------------------------------------------------
    # Mode 2: Classic phase-by-phase
    # ------------------------------------------------------------------
    runner = StepRunner(
        hac=hac,
        local_server=local_server,
        log_reader=log_reader,
        classifier=classifier,
        healer=healer,
        escalation=escalation_handler,
        context=context,
    )
    classic_pipeline = UpgradePipeline(runner)

    if phase is not None:
        console.print(f"[cyan]Classic mode — Phase {phase} only[/cyan]\n")
        phase_methods = {
            0: runner.phase_0_preflight,
            1: runner.phase_1_build_deploy,
            2: runner.phase_2_health,
            3: runner.phase_3_system_update,
            4: runner.phase_4_data_integration,
            5: runner.phase_5_smoke_tests,
        }
        if phase not in phase_methods:
            console.print(f"[red]Invalid phase: {phase}. Must be 0-5.[/red]")
            sys.exit(1)
        results_list = phase_methods[phase]()
        results = {
            "status": "SUCCESS" if all(r.status != StepStatus.ESCALATED for r in results_list) else "ESCALATED",
            "results": {f"phase_{phase}": results_list},
        }
    else:
        console.print(f"[cyan]Classic mode — full pipeline from Phase {start_phase}[/cyan]\n")
        results = classic_pipeline.run(start_phase=start_phase)

    print_phase_results(results)
    sys.exit(0 if results["status"] == "SUCCESS" else 1)


# ------------------------------------------------------------------
# Dry-run plan printer
# ------------------------------------------------------------------

def _print_dry_run_plan(config, release_notes, gates_only, phase, start_phase):
    console.print(yaml.dump(config, default_flow_style=False))

    if gates_only:
        console.print("\n[yellow]Would check: Gate 1 (build) → Gate 2 (server) → Gate 3 (system update)[/yellow]")
        return

    if release_notes:
        try:
            from release_note_parser import ReleaseNoteParser
            parser = ReleaseNoteParser(release_notes)
            rel = parser.parse()
            console.print(f"\n[yellow]Release notes: {rel.target_version}[/yellow]")
            console.print(f"[yellow]Fixed issues: {len(rel.fixed_issues)}[/yellow]")
            console.print(f"[yellow]Action-required steps: {len(rel.get_action_required_steps())}[/yellow]")
            for step in rel.get_action_required_steps():
                console.print(f"  [dim]→ [{step.id}][{step.step_type}] {step.title}[/dim]")
        except Exception as e:
            console.print(f"[red]Could not parse release notes: {e}[/red]")
    elif phase is not None:
        console.print(f"\n[yellow]Would run classic Phase {phase} only[/yellow]")
    else:
        console.print(f"\n[yellow]Would run classic pipeline from Phase {start_phase}[/yellow]")


if __name__ == "__main__":
    main()
