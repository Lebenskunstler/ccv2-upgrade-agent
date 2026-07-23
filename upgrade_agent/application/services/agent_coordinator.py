from pathlib import Path

from upgrade_agent.application.factories.runtime_factory import build_runtime_dependencies
from upgrade_agent.application.services.pipeline_service import PipelineService
from upgrade_agent.application.use_cases import (
    check_gates_only,
    run_abstract_pipeline,
    run_classic_pipeline,
)
from upgrade_agent.config.loader import load_env_config
from upgrade_agent.domain.models.cli_options import CliOptions
from upgrade_agent.domain.models.run_options import AbstractRunOptions, ClassicRunOptions
from upgrade_agent.interfaces.presenters.console_presenter import (
    print_dry_run_plan,
    print_gates_result,
    print_phase_results,
)
from upgrade_agent.adapters.parser.release_note_parser import ReleaseNoteParser
from upgrade_agent.adapters.logs.log_writer import LogWriter
from upgrade_agent.adapters.navigation.code_navigator import CodeNavigator
from upgrade_agent.workflows.abstract_pipeline import AbstractUpgradePipeline
from upgrade_agent.workflows.classic_pipeline import StepRunner, UpgradePipeline


class AgentCoordinator:
    def __init__(
        self,
        config_dir: Path,
        knowledge_dir: Path,
        healing_map_path: Path,
        upgrade_log_template_path: Path,
    ):
        self.config_dir = config_dir
        self.knowledge_dir = knowledge_dir
        self.healing_map_path = healing_map_path
        self.upgrade_log_template_path = upgrade_log_template_path
        self.default_upgrade_log = str(upgrade_log_template_path)

    def run(self, console, options: CliOptions) -> int:
        console.print("\n[bold cyan]SAP Commerce Upgrade Agent[/bold cyan]")
        console.print(f"Environment: [bold]{options.env.upper()}[/bold]")

        config_path = self.config_dir / f"{options.env.lower()}.yaml"
        if not config_path.exists():
            console.print(f"[red]Config not found: {config_path}[/red]")
            return 1

        config = load_env_config(self.config_dir, options.env)
        console.print(f"Platform: [bold]{config.get('platform_version', 'unknown')}[/bold]")
        console.print(f"Mode: [bold]{config.get('deploy_mode', 'unknown')}[/bold]\n")

        if options.dry_run:
            console.print("[yellow]DRY RUN — configuration validated, no actions taken[/yellow]")
            print_dry_run_plan(
                console,
                config,
                options.release_notes,
                options.gates_only,
                options.phase,
                options.start_phase,
            )
            return 0

        runtime = build_runtime_dependencies(
            config=config,
            env=options.env,
            manifest_path=options.manifest or config.get("manifest_path", ""),
            healing_map_path=self.healing_map_path,
            upgrade_log_template_path=self.upgrade_log_template_path,
        )
        pipeline_service = PipelineService()

        if options.release_notes or options.gates_only:
            notes_path = options.release_notes or str(self.knowledge_dir / "release-notes.txt")
            log_path = options.upgrade_log or self.default_upgrade_log
            gate_checker = runtime.gate_checker

            if options.gates_only:
                console.print("[cyan]Checking 3 gates only (no release notes processing)[/cyan]\n")
                use_case_result = check_gates_only.execute(
                    service=pipeline_service,
                    gate_checker=gate_checker,
                    skip_build=options.skip_build,
                )
                print_gates_result(console, use_case_result.raw)
                return 0 if use_case_result.all_pass else 1

            console.print(f"[cyan]Abstract pipeline — release notes: {notes_path}[/cyan]")
            console.print(f"[cyan]Upgrade log: {log_path}[/cyan]\n")

            release_parser = ReleaseNoteParser(notes_path)
            log_writer = LogWriter(log_path)
            code_navigator = CodeNavigator(options.custom_code_root or "")

            abstract_pipeline = AbstractUpgradePipeline(
                release_notes_path=notes_path,
                gate_checker=gate_checker,
                classifier=runtime.classifier,
                healer=runtime.healer,
                escalation=runtime.escalation_handler,
                context=runtime.context,
                release_parser=release_parser,
                log_writer=log_writer,
                code_navigator=code_navigator,
                max_gate_retries=3,
            )

            abstract_options = AbstractRunOptions(
                release_notes_path=notes_path,
                upgrade_log_path=log_path,
                custom_code_root=options.custom_code_root or "",
                max_iterations=options.max_iterations,
                skip_build=options.skip_build,
            )
            use_case_result = run_abstract_pipeline.execute(
                service=pipeline_service,
                pipeline=abstract_pipeline,
                options=abstract_options,
            )
            result = use_case_result.raw

            console.print(f"\n{'=' * 60}")
            color = "green" if result.succeeded else "red"
            console.print(f"[bold {color}]{result.message}[/bold {color}]")
            console.print(f"\n[dim]{result.final_gate_summary}[/dim]")

            if result.total_fixes:
                console.print(f"\n[cyan]Fixes applied ({len(result.total_fixes)}):[/cyan]")
                for fix in result.total_fixes:
                    console.print(f"  • {fix}")

            if result.total_escalations:
                console.print(f"\n[red]Escalations ({len(result.total_escalations)}):[/red]")
                for escalation in result.total_escalations:
                    console.print(f"  ⚠ {escalation}")

            if gate_checker.reporter:
                last_iter = result.iterations[-1] if result.iterations else None
                gate_checker.reporter.write_summary(
                    last_iter.gates if last_iter else None,
                    iterations=len(result.iterations),
                    fixes=result.total_fixes,
                    escalations=result.total_escalations,
                )

            return 0 if result.succeeded else 1

        runner = StepRunner(
            hac=runtime.hac,
            local_server=runtime.local_server,
            log_reader=runtime.log_reader,
            classifier=runtime.classifier,
            healer=runtime.healer,
            escalation=runtime.escalation_handler,
            context=runtime.context,
        )
        classic_pipeline = UpgradePipeline(runner)
        classic_options = ClassicRunOptions(start_phase=options.start_phase, phase=options.phase)

        if options.phase is not None:
            console.print(f"[cyan]Classic mode — Phase {options.phase} only[/cyan]\n")
        else:
            console.print(f"[cyan]Classic mode — full pipeline from Phase {options.start_phase}[/cyan]\n")

        try:
            classic_result = run_classic_pipeline.execute(
                service=pipeline_service,
                runner=runner,
                classic_pipeline=classic_pipeline,
                options=classic_options,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1

        results = classic_result.raw
        print_phase_results(console, results)
        return 0 if results["status"] == "SUCCESS" else 1
