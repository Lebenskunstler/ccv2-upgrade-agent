from rich.table import Table

from upgrade_agent.workflows.classic_pipeline import StepStatus


def print_phase_results(console, results: dict):
    table = Table(title="Upgrade Pipeline Results", show_header=True, header_style="bold")
    table.add_column("Step", style="cyan", width=8)
    table.add_column("Status", width=12)
    table.add_column("Output / Error", width=80)

    for phase_results in results.get("results", {}).values():
        for result in phase_results:
            status_style = {
                StepStatus.PASS: "green",
                StepStatus.FAIL: "red",
                StepStatus.SKIP: "yellow",
                StepStatus.ESCALATED: "red bold",
            }.get(result.status, "white")
            detail = result.output if result.status == StepStatus.PASS else result.error
            table.add_row(
                result.step,
                f"[{status_style}]{result.status}[/{status_style}]",
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


def print_gates_result(console, gates_result):
    for gate in gates_result.gates:
        color = "green" if gate.passed else "red"
        icon = "✅" if gate.passed else "❌"
        detail = gate.output if gate.passed else (gate.error or "")
        console.print(f"[{color}]{icon} Gate {gate.gate_id} [{gate.name}]: {detail[:120]}[/{color}]")

    if gates_result.all_pass:
        console.print("\n[bold green]ALL 3 GATES GREEN ✅[/bold green]")
    else:
        console.print(f"\n[bold red]{len(gates_result.failures)} gate(s) failing ❌[/bold red]")


def print_dry_run_plan(console, config: dict, release_notes: str | None, gates_only: bool, phase: int | None, start_phase: int):
    import yaml

    console.print(yaml.dump(config, default_flow_style=False))

    if gates_only:
        console.print("\n[yellow]Would check: Gate 1 (build) → Gate 2 (server) → Gate 3 (system update)[/yellow]")
        return

    if release_notes:
        try:
            from upgrade_agent.adapters.parser.release_note_parser import ReleaseNoteParser

            parser = ReleaseNoteParser(release_notes)
            release = parser.parse()
            console.print(f"\n[yellow]Release notes: {release.target_version}[/yellow]")
            console.print(f"[yellow]Fixed issues: {len(release.fixed_issues)}[/yellow]")
            console.print(f"[yellow]Action-required steps: {len(release.get_action_required_steps())}[/yellow]")
            for step in release.get_action_required_steps():
                console.print(f"  [dim]→ [{step.id}][{step.step_type}] {step.title}[/dim]")
        except Exception as exc:
            console.print(f"[red]Could not parse release notes: {exc}[/red]")
    elif phase is not None:
        console.print(f"\n[yellow]Would run classic Phase {phase} only[/yellow]")
    else:
        console.print(f"\n[yellow]Would run classic pipeline from Phase {start_phase}[/yellow]")
