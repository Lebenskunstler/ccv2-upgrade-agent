from typing import Protocol, Any


class LocalServerPort(Protocol):
    def is_running(self) -> bool:
        ...

    def stop_server(self) -> bool:
        ...


class HACPort(Protocol):
    ...


class LogReaderPort(Protocol):
    ...


class ErrorClassifierPort(Protocol):
    def classify(self, error_text: str) -> Any:
        ...


class HealingExecutorPort(Protocol):
    def execute(self, rule: Any, context: Any) -> Any:
        ...


class EscalationHandlerPort(Protocol):
    def generate_ai_suggestion(self, step_id: str, error_text: str) -> str:
        ...

    def generate_report(self, step: str, error_text: str, fix_attempts: list, suggested_action: str) -> str:
        ...


class LogWriterPort(Protocol):
    def log_step_start(self, step_id: str, title: str):
        ...

    def log_step_done(self, step_id: str, title: str, passed: bool, details: str = "", fix_applied: str = ""):
        ...

    def log_gate_result(self, gate_id: int, gate_name: str, passed: bool, detail: str):
        ...

    def log_fix_applied(self, step_id: str, rule_id: str, action_taken: str):
        ...

    def log_finding(self, finding: str, step_id: str = ""):
        ...

    def log_escalation(self, step_id: str, error_text: str, suggested_action: str):
        ...

    def log_version_fix_found(self, error_text: str, fix_version: str):
        ...

    def log_code_navigation(self, step_id: str, error_type: str, file_paths: list[str]):
        ...

    def log_session_summary(self, gates_passed: bool, total_steps: int, failed_steps: list[str], fixes_applied: list[str]):
        ...


class CodeNavigatorPort(Protocol):
    def find_version_that_fixes(self, error_text: str, release: Any) -> str | None:
        ...

    def find_files_for_error(self, error_text: str) -> list[Any]:
        ...


class ReleaseNoteParserPort(Protocol):
    def parse(self) -> Any:
        ...


class GateCheckerPort(Protocol):
    local_server: LocalServerPort

    def check_all(self, skip_build: bool = False) -> Any:
        ...

    def check_build(self) -> Any:
        ...

    def check_server(self) -> Any:
        ...

    def check_system_update(self, already_triggered: bool = False) -> Any:
        ...


class AbstractPipelinePort(Protocol):
    def run(self, max_iterations: int, skip_build: bool = False) -> Any:
        ...


class StepRunnerPort(Protocol):
    def phase_0_preflight(self):
        ...

    def phase_1_build_deploy(self):
        ...

    def phase_2_health(self):
        ...

    def phase_3_system_update(self):
        ...

    def phase_4_data_integration(self):
        ...

    def phase_5_smoke_tests(self):
        ...


class ClassicPipelinePort(Protocol):
    def run(self, start_phase: int = 0) -> dict:
        ...
