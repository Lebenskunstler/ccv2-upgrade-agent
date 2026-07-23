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
import sys
import urllib3
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

from upgrade_agent.application.services.agent_coordinator import AgentCoordinator
from upgrade_agent.domain.models.cli_options import CliOptions

console = Console()
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
CONFIG_DIR = PROJECT_ROOT / "config"
HEALING_MAP = PROJECT_ROOT / "healing_map.yaml"
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

    coordinator = AgentCoordinator(
        config_dir=CONFIG_DIR,
        knowledge_dir=KNOWLEDGE_DIR,
        healing_map_path=HEALING_MAP,
        upgrade_log_template_path=UPGRADE_LOG_TEMPLATE,
    )
    options = CliOptions(
        env=env,
        release_notes=release_notes,
        upgrade_log=upgrade_log,
        custom_code_root=custom_code_root,
        gates_only=gates_only,
        max_iterations=max_iterations,
        skip_build=skip_build,
        start_phase=start_phase,
        phase=phase,
        dry_run=dry_run,
        manifest=manifest,
    )
    exit_code = coordinator.run(console, options)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
