"""
GateReporter — writes per-gate and final summary reports to a run directory.

Report directory layout:
    <reports_root>/<from_version>-to-<to_version>-<YYYYMMDD-HHMMSS>/
        gate-1-BUILD.md
        gate-2-SERVER-UP.md
        gate-3-SYSTEM-UPDATE.md
        summary.md

Usage:
    reporter = GateReporter(
        reports_root="/home/.../hybris-archives/patch-jdk21",
        from_version="2211-jdk21.9",
        to_version="2211-jdk21.14",
    )
    reporter.log_gate(gate_result)          # call after each gate
    reporter.write_summary(all_gates_result, iterations=1, fixes=[])
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_GATE_NAMES = {1: "BUILD", 2: "SERVER-UP", 3: "SYSTEM-UPDATE"}


class GateReporter:
    def __init__(
        self,
        reports_root: str,
        from_version: str,
        to_version: str,
    ):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        slug = f"{from_version}-to-{to_version}-{ts}"
        self.run_dir = Path(reports_root) / "upgrade-reports" / slug
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.from_version = from_version
        self.to_version = to_version
        self.started_at = datetime.now(timezone.utc)
        logger.info(f"Gate reports → {self.run_dir}")

    # ------------------------------------------------------------------
    # Per-gate report
    # ------------------------------------------------------------------

    def log_gate(self, gate) -> None:
        """Write a markdown report for a single GateResult immediately after it runs."""
        gate_label = _GATE_NAMES.get(gate.gate_id, f"GATE-{gate.gate_id}")
        filename = f"gate-{gate.gate_id}-{gate_label}.md"
        path = self.run_dir / filename

        status = "✅ PASS" if gate.passed else "❌ FAIL"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines = [
            f"# Gate {gate.gate_id} — {gate_label}",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| Upgrade | `{self.from_version}` → `{self.to_version}` |",
            f"| Timestamp | {now} |",
            f"| Status | **{status}** |",
            "",
        ]

        if gate.passed:
            lines += [
                "## Output",
                "",
                gate.output or "*(no output)*",
                "",
            ]
        else:
            lines += [
                "## Error",
                "",
                f"```",
                gate.error or "*(no error detail)*",
                f"```",
                "",
            ]
            if gate.output:
                lines += [
                    "## Last Output",
                    "",
                    f"```",
                    gate.output[-2000:],
                    f"```",
                    "",
                ]

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Gate {gate.gate_id} report → {path.name}")

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------

    def write_summary(self, all_gates_result, iterations: int = 1, fixes: Optional[list] = None, escalations: Optional[list] = None) -> None:
        """Write summary.md after all gates have run."""
        if all_gates_result is None:
            return
        path = self.run_dir / "summary.md"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        elapsed = (datetime.now(timezone.utc) - self.started_at).seconds
        elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"

        overall = "✅ UPGRADE COMPLETE" if all_gates_result.all_pass else "❌ UPGRADE INCOMPLETE"

        lines = [
            f"# Upgrade Summary — {self.from_version} → {self.to_version}",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| Status | **{overall}** |",
            f"| Completed | {now} |",
            f"| Duration | {elapsed_str} |",
            f"| Iterations | {iterations} |",
            f"| Fixes applied | {len(fixes or [])} |",
            f"| Escalations | {len(escalations or [])} |",
            "",
            "## Gate Results",
            "",
            "| Gate | Name | Status | Detail |",
            "|---|---|---|---|",
        ]

        for gate in all_gates_result.gates:
            status = "✅ PASS" if gate.passed else "❌ FAIL"
            detail = gate.output if gate.passed else (gate.error or "")
            lines.append(f"| {gate.gate_id} | {gate.name} | {status} | {detail[:120]} |")

        if fixes:
            lines += ["", "## Fixes Applied", ""]
            for fix in fixes:
                lines.append(f"- {fix}")

        if escalations:
            lines += ["", "## Escalations", ""]
            for esc in escalations:
                lines.append(f"- ⚠ {esc}")

        lines += ["", "---", f"*Reports directory: `{self.run_dir}`*", ""]

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Summary report → {path}")
