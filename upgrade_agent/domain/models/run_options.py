from dataclasses import dataclass


@dataclass
class AbstractRunOptions:
    release_notes_path: str
    upgrade_log_path: str
    custom_code_root: str
    max_iterations: int
    skip_build: bool


@dataclass
class ClassicRunOptions:
    start_phase: int
    phase: int | None
