from upgrade_agent.application.services.pipeline_service import PipelineService
from upgrade_agent.domain.models.run_options import ClassicRunOptions
from upgrade_agent.ports.pipeline_ports import ClassicPipelinePort, StepRunnerPort


def execute(
    service: PipelineService,
    runner: StepRunnerPort,
    classic_pipeline: ClassicPipelinePort,
    options: ClassicRunOptions,
):
    return service.run_classic(
        runner=runner,
        classic_pipeline=classic_pipeline,
        start_phase=options.start_phase,
        phase=options.phase,
    )
