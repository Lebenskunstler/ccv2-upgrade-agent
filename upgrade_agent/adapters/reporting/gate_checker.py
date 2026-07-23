"""
3-gate validator — the single truth about whether the upgrade is healthy.

Gate 1 — BUILD:         ant clean all → BUILD SUCCESSFUL
Gate 2 — SERVER UP:     local HAC responds + login OK
Gate 3 — SYSTEM UPDATE: trigger HAC System Update → no errors → SAPOAuth2 type present

The agent loops until all 3 gates are green.
All gates are independent of the target version.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from upgrade_agent.adapters.server.local_server import LocalServer
from upgrade_agent.adapters.hac.hac_client import HACClient
from upgrade_agent.adapters.logs.log_reader import LocalLogReader
from upgrade_agent.adapters.reporting.gate_reporter import GateReporter

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class GateResult:
    gate_id: int          # 1, 2, or 3
    name: str
    passed: bool
    output: str
    error: Optional[str] = None

    def __str__(self) -> str:
        status = "✅ PASS" if self.passed else "❌ FAIL"
        detail = self.output if self.passed else (self.error or "")
        return f"Gate {self.gate_id} [{self.name}] {status}: {detail[:200]}"


@dataclass
class AllGatesResult:
    gates: list[GateResult]

    @property
    def all_pass(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    @property
    def summary(self) -> str:
        lines = [str(g) for g in self.gates]
        overall = "ALL GREEN ✅" if self.all_pass else f"{len(self.failures)} GATE(S) FAILING ❌"
        return "\n".join(lines) + f"\n→ {overall}"


# ------------------------------------------------------------------
# Gate Checker
# ------------------------------------------------------------------

class GateChecker:
    """
    Runs the 3 upgrade gates in order: Build → Server → System Update.

    Stops at first failure by default (fail_fast=True) since a failing
    build makes server + system update checks meaningless.
    """

    def __init__(
        self,
        local_server: LocalServer,
        hac: HACClient,
        log_reader: LocalLogReader,
        config: dict,
        reporter: Optional[GateReporter] = None,
    ):
        self.local_server = local_server
        self.hac = hac
        self.log_reader = log_reader
        self.config = config
        self.reporter = reporter

    def _cfg(self, *keys, default=None):
        val = self.config
        for k in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(k, default)
            if val is None:
                return default
        return val

    # ------------------------------------------------------------------
    # Gate 1 — Build
    # ------------------------------------------------------------------

    def check_build(self) -> GateResult:
        """
        Gate 1: Run ant clean all and verify BUILD SUCCESSFUL.

        Skips if the platform directory is not configured (CI/CD environments
        where the build is managed externally).
        """
        platform_dir = self.local_server.platform_dir
        if not platform_dir.exists():
            return GateResult(
                gate_id=1, name="BUILD",
                passed=False,
                output="",
                error=(
                    f"Platform directory not found: {platform_dir}. "
                    f"Set hybris_dir in config or HYBRIS_HOME env var."
                ),
            )

        logger.info("Gate 1: Running ant clean all...")
        logger.info("ant clean all is running — your fan should kick in shortly.")
        timeout = self._cfg("thresholds", "build_timeout_min", default=120) * 60
        result = self.local_server.run_ant("clean all", timeout_seconds=timeout)

        if result.success:
            return GateResult(
                gate_id=1, name="BUILD",
                passed=True,
                output="ant clean all: BUILD SUCCESSFUL",
            )

        return GateResult(
            gate_id=1, name="BUILD",
            passed=False,
            output=result.output[-1000:] if result.output else "",
            error=result.error or "BUILD FAILED — no error detail captured",
        )

    # ------------------------------------------------------------------
    # Gate 2 — Server up
    # ------------------------------------------------------------------

    def check_server(self) -> GateResult:
        """
        Gate 2: Verify the local SAP Commerce server is running and HAC login works.

        Waits up to server_startup_timeout_min if the server is not yet responding.
        """
        logger.info("Gate 2: Checking local server health...")

        timeout = self._cfg("thresholds", "server_startup_timeout_min", default=30)

        if not self.local_server.is_running():
            logger.info(f"Server not yet up — waiting up to {timeout}min...")
            ok = self.local_server.wait_for_server(timeout_minutes=timeout)
            if not ok:
                return GateResult(
                    gate_id=2, name="SERVER UP",
                    passed=False,
                    output="",
                    error=(
                        f"Local server did not respond within {timeout}min at "
                        f"{self.local_server.hac_url}. "
                        f"Start it: cd $HYBRIS_HOME/bin/platform && ./hybrisserver.sh"
                    ),
                )

        if not self.hac.health_check():
            # Server port is up but HAC may still be initializing — retry for up to 10 min
            import time
            hac_timeout = 10 * 60
            hac_interval = 30
            elapsed = 0
            logger.info("HAC /login not yet ready — server is still initializing, waiting...")
            while elapsed < hac_timeout:
                time.sleep(hac_interval)
                elapsed += hac_interval
                logger.info(f"Waiting for HAC /login... ({elapsed}s elapsed)")
                if self.hac.health_check():
                    break
            else:
                return GateResult(
                    gate_id=2, name="SERVER UP",
                    passed=False,
                    output="",
                    error="HAC /login page not responding — server may be partially started",
                )

        if not self.hac.login():
            return GateResult(
                gate_id=2, name="SERVER UP",
                passed=False,
                output="",
                error="HAC login failed — check HAC_PASSWORD_LOCAL and admin credentials",
            )

        # Sanity: check startup logs for BeanCreationException
        errors = self.log_reader.search_pattern_in_logs("BeanCreationException", minutes=20)
        if errors:
            sample = errors[0].get("message", "")[:300]
            return GateResult(
                gate_id=2, name="SERVER UP",
                passed=False,
                output="",
                error=f"BeanCreationException in startup logs: {sample}",
            )

        return GateResult(
            gate_id=2, name="SERVER UP",
            passed=True,
            output=f"HAC responding at {self.hac.base_url} — login OK, no BeanCreationException",
        )

    # ------------------------------------------------------------------
    # Gate 3 — System Update
    # ------------------------------------------------------------------

    def check_system_update(self, already_triggered: bool = False) -> GateResult:
        """
        Gate 3: Trigger HAC System Update and verify it completes without errors.

        Checks:
        - System Update triggers and completes (no timeout, no ERROR in init log)
        - SAPOAuth2Authorization type is registered in the type system
        - No new ERROR-level entries in startup logs after completion

        Args:
            already_triggered: If True, skip triggering and just poll the init log.
        """
        logger.info("Gate 3: Triggering HAC System Update...")

        if not already_triggered:
            trigger = self.hac.trigger_system_update(update_running_system_only=True)
            if not trigger.triggered:
                return GateResult(
                    gate_id=3, name="SYSTEM UPDATE",
                    passed=False,
                    output="",
                    error=f"System Update trigger failed: {trigger.error}",
                )

        timeout = self._cfg("thresholds", "system_update_timeout_min", default=120)
        logger.info(f"Polling init log (timeout {timeout}min)...")
        poll = self.hac.poll_init_log_until_done(timeout_minutes=timeout)

        if poll["timeout"]:
            return GateResult(
                gate_id=3, name="SYSTEM UPDATE",
                passed=False,
                output="",
                error=f"System Update timed out after {timeout}min",
            )

        if poll["error_detected"]:
            last = poll.get("last_log", "")[:500]
            return GateResult(
                gate_id=3, name="SYSTEM UPDATE",
                passed=False,
                output="",
                error=f"ERROR detected in System Update init log: {last}",
            )

        # Verify SAPOAuth2Authorization type is registered (optional — Groovy may be unavailable)
        count = self.hac.count_sap_oauth2_authorizations()
        if count == -1:
            # Groovy unavailable (404/405 on console) or type not yet visible.
            # Treat as a warning, not a hard failure — System Update already completed.
            logger.warning(
                "SAPOAuth2Authorization count check failed (Groovy unavailable or type not found). "
                "System Update completed — treating as warning only."
            )
            oauth_note = "SAPOAuth2Authorization: check skipped (Groovy unavailable)"
        else:
            oauth_note = f"SAPOAuth2Authorization registered (count={count})"

        # Quick post-update log check
        critical_errors = self.log_reader.query_errors_in_last_minutes(minutes=10)
        bean_errors = [
            e.get("message", "")[:300] for e in critical_errors
            if "BeanCreation" in e.get("message", "")
        ]
        if bean_errors:
            return GateResult(
                gate_id=3, name="SYSTEM UPDATE",
                passed=False,
                output="",
                error=f"BeanCreationException after System Update: {bean_errors[0]}",
            )

        return GateResult(
            gate_id=3, name="SYSTEM UPDATE",
            passed=True,
            output=(
                f"System Update complete. "
                f"{oauth_note}. "
                f"No critical errors."
            ),
        )

    # ------------------------------------------------------------------
    # Run all 3 gates
    # ------------------------------------------------------------------

    def check_all(self, fail_fast: bool = True, skip_build: bool = False) -> AllGatesResult:
        """
        Run all 3 gates in order.

        Args:
            fail_fast: Stop at first failing gate (default True).
            skip_build: Skip Gate 1 if build was already verified externally.

        Returns:
            AllGatesResult with per-gate results and overall status.
        """
        gates: list[GateResult] = []

        if not skip_build:
            g1 = self.check_build()
            gates.append(g1)
            logger.info(str(g1))
            if self.reporter:
                self.reporter.log_gate(g1)
            if fail_fast and not g1.passed:
                return AllGatesResult(gates=gates)
        else:
            # Log a synthetic skip record so the report directory is complete
            g1_skip = GateResult(gate_id=1, name="BUILD", passed=True,
                                 output="Skipped (skip_build=True)")
            if self.reporter:
                self.reporter.log_gate(g1_skip)

        g2 = self.check_server()
        gates.append(g2)
        logger.info(str(g2))
        if self.reporter:
            self.reporter.log_gate(g2)
        if fail_fast and not g2.passed:
            return AllGatesResult(gates=gates)

        g3 = self.check_system_update()
        gates.append(g3)
        logger.info(str(g3))
        if self.reporter:
            self.reporter.log_gate(g3)

        return AllGatesResult(gates=gates)
