from dataclasses import dataclass


@dataclass
class CliOptions:
    env: str
    release_notes: str | None
    upgrade_log: str | None
    custom_code_root: str | None
    gates_only: bool
    max_iterations: int
    skip_build: bool
    start_phase: int
    phase: int | None
    dry_run: bool
    manifest: str | None
