from upgrade_agent.application.services.pipeline_service import PipelineService
from upgrade_agent.ports.pipeline_ports import GateCheckerPort


def execute(service: PipelineService, gate_checker: GateCheckerPort, skip_build: bool):
    return service.run_gates_only(gate_checker=gate_checker, skip_build=skip_build)
