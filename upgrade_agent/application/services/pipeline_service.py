from upgrade_agent.application.dto.run_results import (
    AbstractPipelineRunResult,
    ClassicPipelineRunResult,
    GatesOnlyResult,
)
from upgrade_agent.ports.pipeline_ports import (
    AbstractPipelinePort,
    ClassicPipelinePort,
    GateCheckerPort,
    StepRunnerPort,
)
from upgrade_agent.workflows.classic_pipeline import StepStatus


class PipelineService:
    def run_gates_only(self, gate_checker: GateCheckerPort, skip_build: bool) -> GatesOnlyResult:
        result = gate_checker.check_all(skip_build=skip_build)
        return GatesOnlyResult(all_pass=result.all_pass, raw=result)

    def run_abstract(
        self,
        pipeline: AbstractPipelinePort,
        max_iterations: int,
        skip_build: bool,
    ) -> AbstractPipelineRunResult:
        result = pipeline.run(max_iterations=max_iterations, skip_build=skip_build)
        return AbstractPipelineRunResult(succeeded=result.succeeded, raw=result)

    def run_classic(
        self,
        runner: StepRunnerPort,
        classic_pipeline: ClassicPipelinePort,
        start_phase: int,
        phase: int | None,
    ) -> ClassicPipelineRunResult:
        if phase is None:
            results = classic_pipeline.run(start_phase=start_phase)
            return ClassicPipelineRunResult(status=results["status"], raw=results)

        phase_methods = {
            0: runner.phase_0_preflight,
            1: runner.phase_1_build_deploy,
            2: runner.phase_2_health,
            3: runner.phase_3_system_update,
            4: runner.phase_4_data_integration,
            5: runner.phase_5_smoke_tests,
        }

        if phase not in phase_methods:
            raise ValueError(f"Invalid phase: {phase}. Must be 0-5.")

        results_list = phase_methods[phase]()
        status = "SUCCESS" if all(r.status != StepStatus.ESCALATED for r in results_list) else "ESCALATED"
        results = {
            "status": status,
            "results": {f"phase_{phase}": results_list},
        }
        return ClassicPipelineRunResult(status=status, raw=results)
