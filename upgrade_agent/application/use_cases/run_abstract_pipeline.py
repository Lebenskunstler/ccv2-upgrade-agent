from upgrade_agent.application.services.pipeline_service import PipelineService
from upgrade_agent.ports.pipeline_ports import AbstractPipelinePort
from upgrade_agent.domain.models.run_options import AbstractRunOptions


def execute(service: PipelineService, pipeline: AbstractPipelinePort, options: AbstractRunOptions):
    return service.run_abstract(
        pipeline=pipeline,
        max_iterations=options.max_iterations,
        skip_build=options.skip_build,
    )
