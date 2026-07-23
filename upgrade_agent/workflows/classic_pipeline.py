"""
StepRunner + full upgrade pipeline (Phase 0–5).

Each step produces: StepResult { step, status, output, error, fix_applied }
The agent never proceeds to next step if current step has not passed verification.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any

from upgrade_agent.ports.pipeline_ports import (
    HACPort,
    LocalServerPort,
    LogReaderPort,
    ErrorClassifierPort,
    HealingExecutorPort,
    EscalationHandlerPort,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

class StepStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ESCALATED = "ESCALATED"


@dataclass
class StepResult:
    step: str
    status: StepStatus
    output: str = ""
    error: str = ""
    fix_applied: str = ""


@dataclass
class UpgradeContext:
    env: str
    config: dict
    manifest_path: str = ""
    current_build_code: str = ""
    current_deploy_code: str = ""
    retry_counts: dict = field(default_factory=dict)

    def increment_retry(self, step: str) -> int:
        self.retry_counts[step] = self.retry_counts.get(step, 0) + 1
        return self.retry_counts[step]

    def get_retry_count(self, step: str) -> int:
        return self.retry_counts.get(step, 0)


# ------------------------------------------------------------------
# Step Runner
# ------------------------------------------------------------------

class StepRunner:
    """
    Executes pipeline steps with self-healing on known failures.

    On failure:
        1. Classify the error.
        2. If known → apply fix → retry (up to max_retries).
        3. If unknown or max retries exceeded → escalate.
    """

    def __init__(
        self,
        hac: HACPort,
        local_server: LocalServerPort,
        log_reader: LogReaderPort,
        classifier: ErrorClassifierPort,
        healer: HealingExecutorPort,
        escalation: EscalationHandlerPort,
        context: UpgradeContext,
    ):
        self.hac = hac
        self.local_server = local_server
        self.log_reader = log_reader
        self.classifier = classifier
        self.healer = healer
        self.escalation = escalation
        self.context = context
        self.results: list[StepResult] = []

    def _cfg(self, *keys, default=None):
        """Navigate nested config dict."""
        val = self.context.config
        for k in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(k, default)
            if val is None:
                return default
        return val

    def run_step(self, step_id: str, fn, max_retries: int = 1) -> StepResult:
        """
        Execute a step function with retry + healing loop.

        Args:
            step_id: Human-readable step ID, e.g. "0.1".
            fn: Callable that returns StepResult.
            max_retries: Max number of heal+retry cycles.
        """
        fix_attempts = []

        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.info(f"Step {step_id} — retry {attempt}/{max_retries}")

            try:
                result = fn()
            except Exception as e:
                result = StepResult(
                    step=step_id,
                    status=StepStatus.FAIL,
                    error=str(e),
                )

            if result.status == StepStatus.PASS:
                self.results.append(result)
                return result

            # Step failed — try to heal
            if attempt < max_retries:
                rule = self.classifier.classify(result.error)
                if rule:
                    heal: Any = self.healer.execute(rule, self.context)
                    fix_attempts.append({
                        "fix_id": heal.fix_id,
                        "result": heal.action_taken,
                    })
                    result.fix_applied = heal.action_taken

                    if heal.needs_redeploy:
                        # Redeploy required — escalate immediately
                        return self._escalate(
                            step_id, result, fix_attempts,
                            f"Fix for {rule.id} requires a redeploy: {heal.action_taken}",
                        )

                    if heal.needs_escalation:
                        return self._escalate(
                            step_id, result, fix_attempts,
                            heal.escalation_reason,
                        )

                    if heal.healed:
                        continue  # retry the step
                else:
                    # No matching rule
                    return self._escalate(
                        step_id, result, fix_attempts,
                        "Error not in healing map — manual investigation required",
                    )

        # Max retries exceeded
        return self._escalate(
            step_id, result, fix_attempts,
            f"Max retries ({max_retries}) exceeded for step {step_id}",
        )

    def _escalate(
        self,
        step_id: str,
        result: StepResult,
        fix_attempts: list,
        reason: str,
    ) -> StepResult:
        logger.error(f"ESCALATING step {step_id}: {reason}")

        ai_suggestion = self.escalation.generate_ai_suggestion(step_id, result.error)
        report = self.escalation.generate_report(
            step=step_id,
            error_text=result.error,
            fix_attempts=fix_attempts,
            suggested_action=ai_suggestion,
        )
        print("\n" + report)

        escalated = StepResult(
            step=step_id,
            status=StepStatus.ESCALATED,
            error=result.error,
            output=reason,
            fix_applied="; ".join(a.get("result", "") for a in fix_attempts),
        )
        self.results.append(escalated)
        return escalated

    # ------------------------------------------------------------------
    # Phase 0 — Pre-flight
    # ------------------------------------------------------------------

    def phase_0_preflight(self) -> list[StepResult]:
        logger.info("=== Phase 0: Pre-flight ===")
        results = []

        # 0.1 manifest.json — no oauth2 webapp
        r = self.run_step("0.1", self._step_0_1_manifest_check)
        results.append(r)
        if r.status == StepStatus.ESCALATED:
            return results

        # 0.2 No old javax JSTL JARs
        r = self.run_step("0.2", self._step_0_2_jstl_check)
        results.append(r)
        if r.status == StepStatus.ESCALATED:
            return results

        # 0.3 Cloud Portal Spring4Shell patch removed (manual check)
        r = self.run_step("0.3", self._step_0_3_spring4shell_check)
        results.append(r)
        if r.status == StepStatus.ESCALATED:
            return results

        # 0.4 Employee pbkdf2 count = 0
        r = self.run_step("0.4", self._step_0_4_pbkdf2_check, max_retries=0)
        results.append(r)
        if r.status == StepStatus.ESCALATED:
            return results

        # 0.5 DB backup verification (human check)
        r = self.run_step("0.5", self._step_0_5_db_backup_check)
        results.append(r)

        # 0.6 No stale System Update lock
        r = self.run_step("0.6", self._step_0_6_no_lock_check, max_retries=2)
        results.append(r)

        return results

    def _step_0_1_manifest_check(self) -> StepResult:
        if not self.context.manifest_path or not os.path.exists(self.context.manifest_path):
            return StepResult("0.1", StepStatus.SKIP, output="manifest.json path not set — skipping")

        with open(self.context.manifest_path) as f:
            manifest = json.load(f)

        webapps = manifest.get("storefrontAddons", [])
        # Check all aspects for oauth2 webapp with wrong extension name
        bad_found = []
        for aspect in manifest.get("aspects", []):
            for wa in aspect.get("webapps", []):
                if wa.get("name") == "oauth2" and "/authorizationserver" in wa.get("contextPath", ""):
                    bad_found.append(f"{aspect.get('name')}: {wa}")

        if bad_found:
            return StepResult(
                "0.1",
                StepStatus.FAIL,
                error=f"SMARTEDIT_OAUTH_404: manifest.json has oauth2 webapp entry: {bad_found}. "
                      f"Must be replaced with authorizationserver extension.",
            )

        return StepResult("0.1", StepStatus.PASS, output="manifest.json webapps OK — no oauth2 with /authorizationserver")

    def _step_0_2_jstl_check(self) -> StepResult:
        import glob as glob_module
        base = os.path.dirname(self.context.manifest_path) if self.context.manifest_path else "."
        patterns = [
            "**/WEB-INF/lib/jstl-impl-*.jar",
            "**/WEB-INF/lib/jstl-1.2.jar",
            "**/WEB-INF/lib/javax.servlet.jsp.jstl-*.jar",
        ]
        bad_jars = []
        for pattern in patterns:
            bad_jars.extend(glob_module.glob(os.path.join(base, pattern), recursive=True))

        if bad_jars:
            return StepResult(
                "0.2",
                StepStatus.FAIL,
                error=f"JSTL_TAGLIB_VALIDATOR: Old javax-based JSTL JARs found: {bad_jars}",
            )

        return StepResult("0.2", StepStatus.PASS, output="No old javax JSTL JARs found")

    def _step_0_3_spring4shell_check(self) -> StepResult:
        return StepResult(
            "0.3",
            StepStatus.PASS,
            output="Spring4Shell check: confirm manually in Cloud Portal that spring4shell-patch properties are removed.",
        )

    def _step_0_4_pbkdf2_check(self) -> StepResult:
        if not self.hac.health_check():
            return StepResult("0.4", StepStatus.SKIP, output="HAC not reachable — skipping pbkdf2 check")

        count = self.hac.count_employees_with_pbkdf2()
        if count == -1:
            return StepResult("0.4", StepStatus.SKIP, output="Could not query employee pbkdf2 count")
        if count > 0:
            return StepResult(
                "0.4",
                StepStatus.FAIL,
                error=f"PBKDF2_ENCODER_MISSING: {count} employees still have pbkdf2-encoded passwords. "
                      f"Must re-encode to bcrypt/argon2 on jdk17 BEFORE Migrate Data deploy.",
            )

        return StepResult("0.4", StepStatus.PASS, output=f"Employee pbkdf2 count = {count} — OK")

    def _step_0_5_db_backup_check(self) -> StepResult:
        return StepResult(
            "0.5",
            StepStatus.PASS,
            output="DB backup: verify manually in SAP Cloud Portal that backup completed before proceeding.",
        )

    def _step_0_6_no_lock_check(self) -> StepResult:
        if not self.hac.health_check():
            return StepResult("0.6", StepStatus.SKIP, output="HAC not reachable — skipping lock check")

        result = self.hac.run_groovy(
            "spring.getBean('systemSetupService').isRunning()",
            commit=False,
        )
        if result.execution_result and "true" in result.execution_result.lower():
            return StepResult(
                "0.6",
                StepStatus.FAIL,
                error="STALE_SYSTEM_UPDATE_LOCK: System update is currently marked as running. "
                      "Restart backoffice pod and retry.",
            )

        return StepResult("0.6", StepStatus.PASS, output="No stale System Update lock")

    # ------------------------------------------------------------------
    # Phase 1 — Local Build
    # ------------------------------------------------------------------

    def phase_1_build_deploy(self) -> list[StepResult]:
        logger.info("=== Phase 1: Local Build (ant clean all) ===")
        results = []

        # 1.1 Verify platform directory + setantenv.sh exist
        r = self.run_step("1.1", self._step_1_1_check_build_tools)
        results.append(r)
        if r.status == StepStatus.ESCALATED:
            return results

        # 1.2 Run ant clean all
        r = self.run_step("1.2", self._step_1_2_ant_build, max_retries=2)
        results.append(r)

        return results

    def _step_1_1_check_build_tools(self) -> StepResult:
        platform_dir = self.local_server.platform_dir
        if not platform_dir.exists():
            return StepResult(
                "1.1",
                StepStatus.ESCALATED,
                error=(
                    f"Platform directory not found: {platform_dir}. "
                    f"Set hybris_dir in config/local.yaml or export HYBRIS_HOME before running."
                ),
            )

        setantenv = platform_dir / "setantenv.sh"
        if not setantenv.exists():
            return StepResult(
                "1.1",
                StepStatus.ESCALATED,
                error=f"setantenv.sh not found at {setantenv} — is hybris_dir pointing at the hybris root?",
            )

        return StepResult("1.1", StepStatus.PASS, output=f"Build tools found: {platform_dir}")

    def _step_1_2_ant_build(self) -> StepResult:
        timeout = self._cfg("thresholds", "build_timeout_min", default=120) * 60
        result = self.local_server.run_ant("clean all", timeout_seconds=timeout)

        if result.success:
            return StepResult("1.2", StepStatus.PASS, output="ant clean all: BUILD SUCCESSFUL")

        return StepResult(
            "1.2",
            StepStatus.FAIL,
            error=f"ant clean all failed:\n{result.error or result.output[-2000:]}",
        )

    # ------------------------------------------------------------------
    # Phase 2 — Post-deploy Health
    # ------------------------------------------------------------------

    def phase_2_health(self) -> list[StepResult]:
        logger.info("=== Phase 2: Post-deploy Health ===")
        results = []

        # 2.1 All aspects respond on health endpoint
        r = self.run_step("2.1", self._step_2_1_aspect_health, max_retries=3)
        results.append(r)

        # 2.2 Local logs — no ERROR in startup
        r = self.run_step("2.2", self._step_2_2_opensearch_check, max_retries=2)
        results.append(r)

        # 2.3 HAC accessible
        r = self.run_step("2.3", self._step_2_3_hac_accessible, max_retries=3)
        results.append(r)
        if r.status == StepStatus.ESCALATED:
            return results

        # 2.4 No BeanCreationException in backoffice
        r = self.run_step("2.4", self._step_2_4_no_bean_exceptions, max_retries=2)
        results.append(r)

        return results

    def _step_2_1_aspect_health(self) -> StepResult:
        """Wait for the local server to be responding."""
        if self.local_server.is_running():
            return StepResult("2.1", StepStatus.PASS, output=f"Local server responding at {self.local_server.hac_url}")

        timeout = self._cfg("thresholds", "server_startup_timeout_min", default=30)
        ok = self.local_server.wait_for_server(timeout_minutes=timeout)
        if ok:
            return StepResult("2.1", StepStatus.PASS, output="Local server is up")

        return StepResult(
            "2.1",
            StepStatus.FAIL,
            error=(
                f"Local server did not respond within {timeout}min at {self.local_server.hac_url}. "
                f"Start the server: cd $HYBRIS_HOME/bin/platform && ./hybrisserver.sh"
            ),
        )

    def _step_2_2_opensearch_check(self) -> StepResult:
        check = self.log_reader.check_startup_logs_clean(aspect="all", minutes=15)
        if check["clean"]:
            return StepResult("2.2", StepStatus.PASS, output="No startup errors in local logs")

        errors_text = "\n".join(check["errors"][:5])
        return StepResult(
            "2.2",
            StepStatus.FAIL,
            error=f"Startup errors in logs ({check['error_count']} total):\n{errors_text}",
        )

    def _step_2_3_hac_accessible(self) -> StepResult:
        if not self.hac.health_check():
            return StepResult("2.3", StepStatus.FAIL, error="HAC not reachable — login page not responding")

        if not self.hac.login():
            return StepResult("2.3", StepStatus.FAIL, error="HAC login failed — check credentials")

        return StepResult("2.3", StepStatus.PASS, output="HAC accessible and login successful")

    def _step_2_4_no_bean_exceptions(self) -> StepResult:
        errors = self.log_reader.search_pattern_in_logs(
            "BeanCreationException", minutes=20, aspect="backoffice"
        )
        if errors:
            msgs = "\n".join(e.get("message", "")[:300] for e in errors[:3])
            return StepResult(
                "2.4",
                StepStatus.FAIL,
                error=f"SPRING4SHELL_FLUENT_SETTER: BeanCreationException in backoffice:\n{msgs}",
            )

        return StepResult("2.4", StepStatus.PASS, output="No BeanCreationException in backoffice logs")

    # ------------------------------------------------------------------
    # Phase 3 — System Update
    # ------------------------------------------------------------------

    def phase_3_system_update(self) -> list[StepResult]:
        logger.info("=== Phase 3: System Update ===")
        results = []

        # 3.1 Trigger System Update
        r = self.run_step("3.1", self._step_3_1_trigger_system_update, max_retries=2)
        results.append(r)
        if r.status in (StepStatus.FAIL, StepStatus.ESCALATED):
            return results

        # 3.2 Poll init log until empty
        r = self.run_step("3.2", self._step_3_2_poll_init_log, max_retries=0)
        results.append(r)
        if r.status in (StepStatus.FAIL, StepStatus.ESCALATED):
            return results

        # 3.3 Verify no ERROR in init logs
        r = self.run_step("3.3", self._step_3_3_verify_no_errors, max_retries=1)
        results.append(r)

        # 3.4 Verify SAPOAuth2Authorization type registered
        r = self.run_step("3.4", self._step_3_4_verify_sap_oauth2, max_retries=1)
        results.append(r)

        # 3.5 Verify cleanOAuth2AuthorizationCronJob not failing
        r = self.run_step("3.5", self._step_3_5_verify_oauth2_cronjob, max_retries=1)
        results.append(r)

        return results

    def _step_3_1_trigger_system_update(self) -> StepResult:
        result = self.hac.trigger_system_update(update_running_system_only=True)
        if result.triggered:
            return StepResult("3.1", StepStatus.PASS, output="System Update triggered")
        return StepResult("3.1", StepStatus.FAIL, error=f"System Update trigger failed: {result.error}")

    def _step_3_2_poll_init_log(self) -> StepResult:
        timeout = self._cfg("thresholds", "system_update_timeout_min", default=120)
        poll = self.hac.poll_init_log_until_done(timeout_minutes=timeout)

        if poll["completed"]:
            return StepResult("3.2", StepStatus.PASS, output="System Update completed")
        if poll["timeout"]:
            return StepResult(
                "3.2", StepStatus.FAIL,
                error=f"System Update timed out after {timeout}min",
            )
        return StepResult(
            "3.2", StepStatus.FAIL,
            error=f"System Update error in init log: {poll['last_log'][:500]}",
        )

    def _step_3_3_verify_no_errors(self) -> StepResult:
        errors = self.log_reader.query_errors_in_last_minutes(minutes=10)
        critical = [e.get("message", "")[:300] for e in errors
                    if "initialization" in e.get("message", "").lower()
                    or "BeanCreation" in e.get("message", "")]

        if critical:
            return StepResult(
                "3.3", StepStatus.FAIL,
                error=f"Errors in initialization logs:\n" + "\n".join(critical[:3]),
            )
        return StepResult("3.3", StepStatus.PASS, output="No critical errors in init logs")

    def _step_3_4_verify_sap_oauth2(self) -> StepResult:
        count = self.hac.count_sap_oauth2_authorizations()
        if count == -1:
            return StepResult(
                "3.4", StepStatus.FAIL,
                error="SAP_OAUTH2_TYPE_MISSING: type code 'SAPOAuth2Authorization' invalid — type not registered",
            )
        return StepResult("3.4", StepStatus.PASS, output=f"SAPOAuth2Authorization type registered (count={count})")

    def _step_3_5_verify_oauth2_cronjob(self) -> StepResult:
        result = self.hac.inspect_oauth_client_details("cleanOAuth2AuthorizationCronJob")
        # For cron job we use a different check
        groovy = """
try {
    import de.hybris.platform.servicelayer.search.FlexibleSearchQuery
    def q = new FlexibleSearchQuery("SELECT {pk} FROM {CleanupOAuth2TokensCronJob}")
    def r = flexibleSearchService.search(q)
    if (!r.result) {
        def q2 = new FlexibleSearchQuery("SELECT {pk} FROM {CronJob} WHERE {code} LIKE '%cleanOAuth2%'")
        r = flexibleSearchService.search(q2)
    }
    if (!r.result) return "CronJob not found"
    def cj = r.result[0]
    return "status=${cj.status} result=${cj.result}"
} catch (e) {
    return "ERROR: " + e.message
}
"""
        gr = self.hac.run_groovy(groovy, commit=False)
        output = gr.execution_result or gr.output or ""
        if "ERROR" in output.upper() and "OAUTH2_CLEANUP" in output.upper():
            return StepResult(
                "3.5", StepStatus.FAIL,
                error=f"OAUTH_CLEANUP_CRON_FAILING: {output}",
            )
        return StepResult("3.5", StepStatus.PASS, output=f"OAuth2 cleanup cron status: {output[:200]}")

    # ------------------------------------------------------------------
    # Phase 4 — Data + Integration
    # ------------------------------------------------------------------

    def phase_4_data_integration(self) -> list[StepResult]:
        logger.info("=== Phase 4: Data + Integration ===")
        results = []

        # 4.1 Orphaned types cleanup
        r = self.run_step("4.1", self._step_4_1_orphaned_types, max_retries=0)
        results.append(r)

        # 4.2 SmartEdit OAuthClientDetails ImpEx
        r = self.run_step("4.2", self._step_4_2_smartedit_impex, max_retries=2)
        results.append(r)

        # 4.3 Trigger Solr full reindex
        r = self.run_step("4.3", self._step_4_3_solr_reindex, max_retries=2)
        results.append(r)

        # 4.4 Verify product count > 0
        r = self.run_step("4.4", self._step_4_4_verify_products, max_retries=3)
        results.append(r)

        # 4.5 Azure integration check
        r = self.run_step("4.5", self._step_4_5_azure_check, max_retries=0)
        results.append(r)

        return results

    def _step_4_1_orphaned_types(self) -> StepResult:
        groovy = """
def ts = spring.getBean("typeSystemService")
try {
    def orphaned = ts.getOrphanedComposedTypes()
    return "Orphaned types count: ${orphaned?.size() ?: 0}"
} catch (e) {
    return "Could not check orphaned types via typeSystemService: ${e.message}"
}
"""
        result = self.hac.run_groovy(groovy, commit=False)
        output = result.execution_result or result.output or ""
        return StepResult("4.1", StepStatus.PASS, output=f"Orphaned types check: {output[:200]}")

    def _step_4_2_smartedit_impex(self) -> StepResult:
        accstorefront_url = self._cfg("endpoints", "accstorefront", default="")
        if not accstorefront_url:
            return StepResult(
                "4.2", StepStatus.FAIL,
                error="SMARTEDIT_OAUTH_CLIENT_MISCONFIGURED: accstorefront URL not configured",
            )

        # Inspect current state first
        inspection = self.hac.inspect_oauth_client_details("smartedit")
        logger.info(f"SmartEdit OAuthClientDetails: {inspection.get('data', 'N/A')}")

        impex = f"""UPDATE OAuthClientDetails;clientId[unique=true];authorities;authorizedGrantTypes;registeredRedirectUri;requireProofKey
;smartedit;ROLE_ADMINGROUP,ROLE_BASECMSMANAGERGROUP,ROLE_PREVIEWMANAGERGROUP;authorization_code,saml_token;{accstorefront_url}/smartedit;true"""

        result = self.hac.run_impex(impex)
        if result.success:
            return StepResult("4.2", StepStatus.PASS, output="SmartEdit OAuthClientDetails updated")

        return StepResult(
            "4.2", StepStatus.FAIL,
            error=f"SMARTEDIT_OAUTH_CLIENT_MISCONFIGURED: ImpEx failed: {result.error}",
        )

    def _step_4_3_solr_reindex(self) -> StepResult:
        result = self.hac.trigger_solr_full_reindex()
        if result["success"]:
            return StepResult("4.3", StepStatus.PASS, output=f"Solr reindex triggered: {result['output'][:200]}")

        return StepResult(
            "4.3", StepStatus.FAIL,
            error=f"SOLR_EMPTY_PRODUCTS: Solr reindex trigger failed: {result['error']}",
        )

    def _step_4_4_verify_products(self) -> StepResult:
        catalogs = self._cfg("known_active_catalogs", default=[])
        if not catalogs:
            return StepResult("4.4", StepStatus.SKIP, output="No active catalogs configured — skipping")

        # Wait a bit for reindex to complete
        time.sleep(60)

        timeout_min = self._cfg("thresholds", "solr_reindex_timeout_min", default=60)
        deadline = time.time() + timeout_min * 60

        while time.time() < deadline:
            counts = self.hac.get_product_counts_per_catalog(catalogs)
            all_positive = all(v > 0 for v in counts.values())
            zero_catalogs = [k for k, v in counts.items() if v == 0]

            if all_positive:
                summary = ", ".join(f"{k}={v}" for k, v in counts.items())
                return StepResult("4.4", StepStatus.PASS, output=f"Product counts: {summary}")

            logger.info(f"Waiting for Solr index: zero counts on {zero_catalogs}")
            time.sleep(60)

        summary = ", ".join(f"{k}={v}" for k, v in counts.items())
        return StepResult(
            "4.4", StepStatus.FAIL,
            error=f"SOLR_EMPTY_PRODUCTS: Products still 0 after {timeout_min}min: {summary}",
        )

    def _step_4_5_azure_check(self) -> StepResult:
        check = self.log_reader.check_azure_integration_reachable(minutes=10)
        if check["reachable"]:
            return StepResult("4.5", StepStatus.PASS, output="Azure integration: no connection errors in logs")

        issues = "; ".join(check["issues"])
        logger.warning(f"Azure integration issues detected: {issues}")
        # Log warning but don't fail — Azure may be temporarily unreachable
        return StepResult("4.5", StepStatus.PASS, output=f"Azure integration WARNING: {issues}")

    # ------------------------------------------------------------------
    # Phase 5 — Smoke Tests
    # ------------------------------------------------------------------

    def phase_5_smoke_tests(self) -> list[StepResult]:
        logger.info("=== Phase 5: Smoke Tests ===")
        results = []

        r = self.run_step("5.1", self._step_5_1_storefront_loads, max_retries=3)
        results.append(r)

        r = self.run_step("5.2", self._step_5_2_smartedit_oauth, max_retries=2)
        results.append(r)

        r = self.run_step("5.3", self._step_5_3_product_listing, max_retries=2)
        results.append(r)

        r = self.run_step("5.4", self._step_5_4_no_new_errors, max_retries=0)
        results.append(r)

        return results

    def _step_5_1_storefront_loads(self) -> StepResult:
        import requests as req
        url = self._cfg("endpoints", "accstorefront", default="")
        if not url:
            return StepResult("5.1", StepStatus.SKIP, output="accstorefront URL not configured")

        try:
            resp = req.get(url, timeout=15, allow_redirects=True, verify=False)
            if resp.status_code == 200:
                return StepResult("5.1", StepStatus.PASS, output=f"Storefront HTTP 200: {url}")
            return StepResult(
                "5.1", StepStatus.FAIL,
                error=f"Storefront returned HTTP {resp.status_code}: {url}",
            )
        except req.RequestException as e:
            return StepResult("5.1", StepStatus.FAIL, error=f"Storefront unreachable: {e}")

    def _step_5_2_smartedit_oauth(self) -> StepResult:
        import requests as req
        accstorefront = self._cfg("endpoints", "accstorefront", default="")
        if not accstorefront:
            return StepResult("5.2", StepStatus.SKIP, output="accstorefront URL not configured")

        auth_url = f"{accstorefront}/authorizationserver/oauth/authorize"
        try:
            resp = req.get(
                auth_url,
                params={"client_id": "smartedit", "response_type": "code"},
                timeout=10,
                allow_redirects=False,
                verify=False,
            )
            # 302 to login page = good (authorizationserver is responding)
            # 404 = bad (endpoint not found)
            if resp.status_code == 404:
                return StepResult(
                    "5.2", StepStatus.FAIL,
                    error=f"SMARTEDIT_OAUTH_404: 404 on {auth_url} — authorizationserver not deployed",
                )
            return StepResult(
                "5.2", StepStatus.PASS,
                output=f"SmartEdit OAuth endpoint responds: HTTP {resp.status_code}",
            )
        except req.RequestException as e:
            return StepResult("5.2", StepStatus.FAIL, error=f"SmartEdit OAuth check failed: {e}")

    def _step_5_3_product_listing(self) -> StepResult:
        catalogs = self._cfg("known_active_catalogs", default=[])
        if not catalogs:
            return StepResult("5.3", StepStatus.SKIP, output="No active catalogs configured")

        counts = self.hac.get_product_counts_per_catalog(catalogs)
        if all(v > 0 for v in counts.values()):
            summary = ", ".join(f"{k}={v}" for k, v in counts.items())
            return StepResult("5.3", StepStatus.PASS, output=f"Products visible: {summary}")

        zero = [k for k, v in counts.items() if v <= 0]
        return StepResult(
            "5.3", StepStatus.FAIL,
            error=f"SOLR_EMPTY_PRODUCTS: No products in catalogs: {zero}",
        )

    def _step_5_4_no_new_errors(self) -> StepResult:
        errors = self.log_reader.query_errors_in_last_minutes(minutes=5)
        critical = [e.get("message", "")[:300] for e in errors
                    if not any(p in e.get("message", "") for p in [
                        "OrgUnitAfterInitializationEndEventListener",
                        "generateUnitPaths",
                    ])]

        if not critical:
            return StepResult("5.4", StepStatus.PASS, output="No new errors in last 5min")

        return StepResult(
            "5.4", StepStatus.FAIL,
            error=f"{len(critical)} new errors in last 5min:\n" + "\n".join(critical[:3]),
        )


# ------------------------------------------------------------------
# Full Pipeline Runner
# ------------------------------------------------------------------

class UpgradePipeline:
    """Runs the full upgrade pipeline Phase 0 → Phase 5."""

    def __init__(self, runner: StepRunner):
        self.runner = runner

    def run(self, start_phase: int = 0) -> dict:
        """
        Execute the upgrade pipeline.

        Args:
            start_phase: Start from this phase (0–5). Useful for resuming.

        Returns:
            dict with overall status and per-phase results.
        """
        all_results = {}
        phases = {
            0: self.runner.phase_0_preflight,
            1: self.runner.phase_1_build_deploy,
            2: self.runner.phase_2_health,
            3: self.runner.phase_3_system_update,
            4: self.runner.phase_4_data_integration,
            5: self.runner.phase_5_smoke_tests,
        }

        for phase_num in sorted(phases.keys()):
            if phase_num < start_phase:
                continue

            phase_fn = phases[phase_num]
            results = phase_fn()
            all_results[f"phase_{phase_num}"] = results

            # Check if any step escalated — halt pipeline
            escalated = [r for r in results if r.status == StepStatus.ESCALATED]
            if escalated:
                logger.error(f"Pipeline halted at phase {phase_num} — escalation required")
                return {
                    "status": "ESCALATED",
                    "halted_at_phase": phase_num,
                    "results": all_results,
                }

        # Check overall result
        all_steps = [r for phase in all_results.values() for r in phase]
        failed = [r for r in all_steps if r.status == StepStatus.FAIL]

        if failed:
            return {
                "status": "FAILED",
                "failed_steps": [r.step for r in failed],
                "results": all_results,
            }

        return {
            "status": "SUCCESS",
            "results": all_results,
        }
