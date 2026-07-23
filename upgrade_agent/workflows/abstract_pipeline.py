"""
AbstractUpgradePipeline — release-note-driven, version-independent upgrade loop.

INPUT:  SAP release notes (.md or .txt, any version)
LOOP:   Parse release notes → execute action steps → check 3 gates → fix failures → iterate
OUTPUT: All 3 gates green + full audit trail in upgrade-LOG.md

The pipeline is abstract: it does not hardcode any version-specific steps.
Steps come from the release notes. The healing map handles known failures.
The code navigator tells the agent WHERE to look in custom code.
The gate checker decides WHEN the upgrade is done.

Each iteration:
  1. Check gate 1 (build) if not already passing.
  2. If build fails → classify error → try to heal → log → retry build.
  3. If build passes → check gate 2 (server up).
  4. If server fails → classify → heal → log → retry.
  5. If server passes → check gate 3 (system update).
  6. If system update fails → classify → heal → log → retry system update.
  7. All 3 green → done.

Max iterations per gate: configurable (default 3).
On max retries exceeded or unknown error → escalate.
"""
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

from upgrade_agent.ports.pipeline_ports import (
    GateCheckerPort,
    ErrorClassifierPort,
    HealingExecutorPort,
    EscalationHandlerPort,
    LogWriterPort,
    CodeNavigatorPort,
    ReleaseNoteParserPort,
)
from upgrade_agent.workflows.classic_pipeline import UpgradeContext

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class IterationResult:
    iteration: int
    gates: Any
    steps_attempted: list[str]
    fixes_applied: list[str]
    escalations: list[str]

    @property
    def succeeded(self) -> bool:
        return self.gates.all_pass


@dataclass
class PipelineResult:
    succeeded: bool
    iterations: list[IterationResult]
    total_fixes: list[str]
    total_escalations: list[str]
    final_gate_summary: str

    @property
    def message(self) -> str:
        if self.succeeded:
            return (
                f"✅ UPGRADE COMPLETE — All 3 gates passed in "
                f"{len(self.iterations)} iteration(s). "
                f"Fixes applied: {len(self.total_fixes)}."
            )
        return (
            f"❌ UPGRADE INCOMPLETE — Gates still failing after "
            f"{len(self.iterations)} iteration(s). "
            f"Escalations: {len(self.total_escalations)}."
        )


@dataclass
class _SyntheticGateResult:
    gate_id: int
    name: str
    passed: bool
    output: str
    error: Optional[str] = None


@dataclass
class _EmptyParsedRelease:
    source_file: str = ""
    target_version: str = "UNKNOWN"
    action_steps: list[Any] = field(default_factory=list)

    def get_action_required_steps(self) -> list[Any]:
        return []


# ------------------------------------------------------------------
# Abstract Pipeline
# ------------------------------------------------------------------

class AbstractUpgradePipeline:
    """
    Version-independent SAP Commerce upgrade pipeline.

    Usage:
        pipeline = AbstractUpgradePipeline(
            release_notes_path="path/to/sap-release-notes.txt",
            upgrade_log_path="path/to/upgrade-LOG.md",
            gate_checker=...,
            classifier=...,
            healer=...,
            escalation=...,
            context=...,
            custom_code_root="path/to/core-customize",
        )
        result = pipeline.run(max_iterations=3)
    """

    def __init__(
        self,
        release_notes_path: str,
        gate_checker: GateCheckerPort,
        classifier: ErrorClassifierPort,
        healer: HealingExecutorPort,
        escalation: EscalationHandlerPort,
        context: UpgradeContext,
        release_parser: ReleaseNoteParserPort,
        log_writer: LogWriterPort,
        code_navigator: CodeNavigatorPort,
        max_gate_retries: int = 3,
    ):
        self.release_notes_path = release_notes_path
        self.gate_checker = gate_checker
        self.classifier = classifier
        self.healer = healer
        self.escalation = escalation
        self.context = context
        self.max_gate_retries = max_gate_retries

        self.release_parser = release_parser
        self.log = log_writer
        self.navigator = code_navigator

        self._release: Optional[Any] = None

    # ------------------------------------------------------------------
    # Release note loading
    # ------------------------------------------------------------------

    def _load_release(self) -> Any:
        if self._release is not None:
            return self._release
        if not self.release_notes_path or not Path(self.release_notes_path).exists():
            logger.warning("No release notes path — running without parsed steps")
            return _EmptyParsedRelease()
        self._release = self.release_parser.parse()
        logger.info(
            f"Loaded release notes: {self._release.target_version} — "
            f"{len(self._release.action_steps)} steps"
        )
        return self._release

    # ------------------------------------------------------------------
    # Gate repair loop
    # ------------------------------------------------------------------

    def _repair_gate(
        self,
        gate: Any,
        fixes_applied: list[str],
        escalations: list[str],
        release: Any,
    ) -> bool:
        """
        Attempt to repair a failing gate.

        Strategy:
          1. Check if error matches a healing rule → apply fix → log.
          2. Cross-reference release notes: is this error fixed in a later version?
          3. Run code navigator → log which files to inspect.
          4. If no rule and no version fix → escalate.

        Returns True if a fix was applied (caller should re-check the gate).
        """
        error_text = gate.error or ""
        step_id = f"GATE-{gate.gate_id}-REPAIR"

        logger.info(f"Attempting to repair {gate.name}: {error_text[:100]}")

        # 1. Check if SAP fixed this in a newer release
        fix_version = self.navigator.find_version_that_fixes(error_text, release)
        if fix_version:
            msg = (
                f"Error '{error_text[:80]}' may be fixed in {fix_version}. "
                f"Consider upgrading platform_version to {fix_version} in manifest.json."
            )
            logger.warning(msg)
            self.log.log_version_fix_found(error_text, fix_version)

        # 2. Find custom code locations to inspect
        nav_hits = self.navigator.find_files_for_error(error_text)
        if nav_hits:
            top = nav_hits[0]
            self.log.log_code_navigation(step_id, top.error_category, top.files)
            logger.info(f"Code navigator: [{top.error_category}] {top.fix_hint}")
            for hit in nav_hits[1:]:
                logger.info(f"  Also check: [{hit.error_category}] {hit.fix_hint}")

        # 3. Try healing map
        rule = self.classifier.classify(error_text)
        if rule:
            logger.info(f"Healing rule matched: {rule.id}")
            heal: Any = self.healer.execute(rule, self.context)
            action_summary = f"[{rule.id}] {heal.action_taken}"

            self.log.log_fix_applied(step_id, rule.id, heal.action_taken)

            if heal.needs_escalation or not heal.healed:
                reason = heal.escalation_reason or "Healer could not apply fix automatically"
                self._escalate(step_id, error_text, reason, escalations)
                return False

            if heal.needs_redeploy:
                self._escalate(
                    step_id, error_text,
                    f"Fix for {rule.id} requires a rebuild/redeploy: {heal.action_taken}",
                    escalations,
                )
                return False

            fixes_applied.append(action_summary)
            return True

        # 4. No rule + no version fix → escalate
        ai_suggestion = self.escalation.generate_ai_suggestion(step_id, error_text)
        self._escalate(step_id, error_text, ai_suggestion, escalations)
        return False

    def _escalate(
        self,
        step_id: str,
        error_text: str,
        reason: str,
        escalations: list[str],
    ):
        report = self.escalation.generate_report(
            step=step_id,
            error_text=error_text,
            fix_attempts=[],
            suggested_action=reason,
        )
        print("\n" + report)
        self.log.log_escalation(step_id, error_text, reason)
        escalations.append(f"{step_id}: {reason[:100]}")

    # ------------------------------------------------------------------
    # Pre-flight: log action steps from release notes
    # ------------------------------------------------------------------

    def _log_preflight(self, release: Any):
        """Log what the release notes say needs to be done, before executing."""
        action_steps = release.get_action_required_steps()
        if not action_steps:
            self.log.log_finding("No ACTION REQUIRED steps found in release notes — standard upgrade path")
            return

        self.log.log_finding(
            f"Release {release.target_version}: {len(action_steps)} action-required step(s) found"
        )
        for step in action_steps:
            files_str = (", ".join(step.files_hint[:3]) + "..." if len(step.files_hint) > 3 else ", ".join(step.files_hint)) if step.files_hint else "N/A"
            self.log.log_finding(
                f"[{step.id}][{step.step_type}] {step.title} | files: {files_str}",
                step_id=step.id,
            )

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    def run(self, max_iterations: int = 3, skip_build: bool = False) -> PipelineResult:
        """
        Run the abstract upgrade pipeline.

        Each iteration:
          - Checks all 3 gates in order.
          - On any gate failure → attempts repair → re-checks that gate.
          - Repeats up to max_gate_retries times per gate.
          - If all gates pass → done.
          - If max_iterations reached → returns partial result.

        Args:
            max_iterations: Hard cap on full pipeline iterations.
            skip_build: If True, assume Gate 1 (build) is already passing.

        Returns:
            PipelineResult with overall success flag and audit trail.
        """
        release = self._load_release()

        self.log.log_finding(
            f"Abstract upgrade pipeline started. "
            f"Target: {release.target_version}. "
            f"Max iterations: {max_iterations}.",
            step_id="PIPELINE",
        )

        self._log_preflight(release)

        all_iterations: list[IterationResult] = []
        total_fixes: list[str] = []
        total_escalations: list[str] = []

        # Track Gate 1 across iterations: no need to rebuild if it already passed.
        gate1_ever_passed = skip_build
        g1_cached = _SyntheticGateResult(gate_id=1, name="BUILD", passed=True, output="Skipped (skip_build=True)") if skip_build else None

        for iteration in range(1, max_iterations + 1):
            logger.info(f"\n{'='*60}\nIteration {iteration}/{max_iterations}\n{'='*60}")
            self.log.log_finding(f"=== Iteration {iteration}/{max_iterations} ===", step_id="ITER")

            iter_fixes: list[str] = []
            iter_escalations: list[str] = []
            iter_steps: list[str] = []

            # --- Gate 1: Build ---
            if gate1_ever_passed:
                # Build already succeeded in a prior iteration — skip the rebuild.
                g1 = g1_cached or _SyntheticGateResult(gate_id=1, name="BUILD", passed=True, output="Carried from prior iteration")
                logger.info("Gate 1 already passed — skipping rebuild")
                iter_steps.append("GATE-1 (skipped — carried)")
            else:
                # Iteration 2+: stop server first so it releases the HSQLDB lock.
                if iteration > 1 and self.gate_checker.local_server.is_running():
                    logger.info("Stopping server before Gate 1 rebuild to release HSQLDB lock...")
                    self.log.log_step_start("SERVER-STOP", "Stopping server before rebuild")
                    stopped = self.gate_checker.local_server.stop_server()
                    self.log.log_step_done(
                        "SERVER-STOP", "hybrisserver.sh stop", stopped,
                        details="Server stopped" if stopped else "Stop command failed — may still be running",
                    )
                    iter_steps.append("SERVER-STOP")
                    if stopped:
                        logger.info("Waiting 15s for server ports to free...")
                        time.sleep(15)

                gate1_passed = False
                for attempt in range(self.max_gate_retries):
                    self.log.log_step_start("GATE-1", "ant clean all (Build)")
                    g1 = self.gate_checker.check_build()
                    self.log.log_gate_result(1, "BUILD", g1.passed, g1.output or g1.error or "")
                    iter_steps.append(f"GATE-1 (attempt {attempt+1})")

                    if g1.passed:
                        gate1_passed = True
                        gate1_ever_passed = True
                        g1_cached = g1
                        break

                    if attempt < self.max_gate_retries - 1:
                        fixed = self._repair_gate(g1, iter_fixes, iter_escalations, release)
                        if not fixed:
                            break
                    else:
                        self._escalate(
                            "GATE-1", g1.error or "",
                            f"Build still failing after {self.max_gate_retries} repair attempts",
                            iter_escalations,
                        )

                if not gate1_passed:
                    gates = AllGatesResult(gates=[g1])
                    result = IterationResult(
                        iteration=iteration,
                        gates=gates,
                        steps_attempted=iter_steps,
                        fixes_applied=iter_fixes,
                        escalations=iter_escalations,
                    )
                    all_iterations.append(result)
                    total_fixes.extend(iter_fixes)
                    total_escalations.extend(iter_escalations)
                    # Build is broken — no point checking server or system update
                    break

            # --- Gate 2: Server up ---
            # Auto-start the server on the first Gate 2 attempt if it's not running.
            if not self.gate_checker.local_server.is_running():
                self.log.log_step_start("SERVER-START", "Starting SAP Commerce server")
                logger.info("Server not running — attempting auto-start...")
                started = self.gate_checker.local_server.start_server(
                    timeout_minutes=self.gate_checker._cfg(
                        "thresholds", "server_startup_timeout_min", default=30
                    )
                )
                self.log.log_step_done(
                    "SERVER-START", "hybrisserver.sh start", started,
                    details="Server up" if started else "Server did not start in time",
                )
                iter_steps.append("SERVER-START")

            gate2_passed = False
            for attempt in range(self.max_gate_retries):
                self.log.log_step_start("GATE-2", "Local server health + HAC login")
                g2 = self.gate_checker.check_server()
                self.log.log_gate_result(2, "SERVER UP", g2.passed, g2.output or g2.error or "")
                iter_steps.append(f"GATE-2 (attempt {attempt+1})")

                if g2.passed:
                    gate2_passed = True
                    break

                if attempt < self.max_gate_retries - 1:
                    fixed = self._repair_gate(g2, iter_fixes, iter_escalations, release)
                    if not fixed:
                        # Wait a bit — server may be starting up
                        logger.info("Waiting 30s before retrying Gate 2...")
                        time.sleep(30)
                else:
                    self._escalate(
                        "GATE-2", g2.error or "",
                        f"Server still not healthy after {self.max_gate_retries} attempts",
                        iter_escalations,
                    )

            if not gate2_passed:
                gates = AllGatesResult(gates=[g1, g2])
                result = IterationResult(
                    iteration=iteration,
                    gates=gates,
                    steps_attempted=iter_steps,
                    fixes_applied=iter_fixes,
                    escalations=iter_escalations,
                )
                all_iterations.append(result)
                total_fixes.extend(iter_fixes)
                total_escalations.extend(iter_escalations)
                break

            # --- Gate 3: System Update ---
            gate3_passed = False
            for attempt in range(self.max_gate_retries):
                self.log.log_step_start("GATE-3", "HAC System Update")
                g3 = self.gate_checker.check_system_update()
                self.log.log_gate_result(3, "SYSTEM UPDATE", g3.passed, g3.output or g3.error or "")
                iter_steps.append(f"GATE-3 (attempt {attempt+1})")

                if g3.passed:
                    gate3_passed = True
                    break

                if attempt < self.max_gate_retries - 1:
                    fixed = self._repair_gate(g3, iter_fixes, iter_escalations, release)
                    if not fixed:
                        break
                else:
                    self._escalate(
                        "GATE-3", g3.error or "",
                        f"System Update still failing after {self.max_gate_retries} repair attempts",
                        iter_escalations,
                    )

            gates = AllGatesResult(gates=[g1, g2, g3])
            result = IterationResult(
                iteration=iteration,
                gates=gates,
                steps_attempted=iter_steps,
                fixes_applied=iter_fixes,
                escalations=iter_escalations,
            )
            all_iterations.append(result)
            total_fixes.extend(iter_fixes)
            total_escalations.extend(iter_escalations)

            if gates.all_pass:
                logger.info("✅ All 3 gates GREEN — upgrade pipeline complete")
                break

            logger.warning(f"Iteration {iteration} ended with failing gates — will retry")

        # -- Final summary --
        succeeded = any(it.succeeded for it in all_iterations)
        final_summary = all_iterations[-1].gates.summary if all_iterations else "No iterations completed"

        self.log.log_session_summary(
            gates_passed=succeeded,
            total_steps=sum(len(it.steps_attempted) for it in all_iterations),
            failed_steps=[e for it in all_iterations for e in it.escalations],
            fixes_applied=total_fixes,
        )

        return PipelineResult(
            succeeded=succeeded,
            iterations=all_iterations,
            total_fixes=total_fixes,
            total_escalations=total_escalations,
            final_gate_summary=final_summary,
        )
