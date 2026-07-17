"""
Error classifier and healing executor.

ErrorClassifier: uses an optional LLM to match raw error text against the healing map.
HealingExecutor: applies the fix for a matched healing rule.
"""
import re
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import os

import yaml

if TYPE_CHECKING:
    from orchestrator import UpgradeContext

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class HealingRule:
    id: str
    pattern: str
    fix: dict
    max_retries: int = 1
    needs_redeploy: bool = False
    note: str = ""
    groovy_hint: str = ""
    timeout_seconds: int = 0


@dataclass
class HealResult:
    healed: bool
    action_taken: str
    needs_redeploy: bool = False
    needs_escalation: bool = False
    escalation_reason: str = ""
    fix_id: str = ""


# ------------------------------------------------------------------
# Error Classifier
# ------------------------------------------------------------------

class ErrorClassifier:
    """
    Classifies raw error text against the healing map.

    Strategy:
        1. Fast regex pre-filter — check pattern against error text.
          2. If no match found, use an optional LLM to classify the error using
              the healing map + upgrade-log context.
          3. If the LLM cannot classify it, return None (trigger escalation).
    """

    def __init__(self, healing_map_path: str, migration_log_path: str = None):
        self.rules: list[HealingRule] = self._load_healing_map(healing_map_path)
        self.migration_log = ""
        if migration_log_path:
            try:
                with open(migration_log_path) as f:
                    self.migration_log = f.read()[:8000]  # first 8KB for context
            except OSError:
                pass
        self._claude = None  # created lazily — only if ANTHROPIC_API_KEY is set

    def _get_llm(self):
        """Return a litellm-compatible callable, or None if no LLM is configured."""
        if self._claude is not None:
            return self._claude
        model = os.environ.get("UPGRADE_AGENT_LLM_MODEL", "")
        if not model:
            # Auto-detect from available API keys
            if os.environ.get("ANTHROPIC_API_KEY"):
                model = "claude-opus-4-6"
            elif os.environ.get("OPENAI_API_KEY"):
                model = "gpt-4o"
            else:
                return None
        try:
            import litellm
            self._claude = lambda prompt, max_tokens: litellm.completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            ).choices[0].message.content.strip()
        except ImportError:
            logger.debug("litellm not installed — LLM classification disabled")
            return None
        return self._claude

    def _load_healing_map(self, path: str) -> list[HealingRule]:
        with open(path) as f:
            data = yaml.safe_load(f)
        rules = []
        for item in data.get("healing_map", []):
            rules.append(HealingRule(
                id=item["id"],
                pattern=item["pattern"],
                fix=item["fix"],
                max_retries=item.get("max_retries", 1),
                needs_redeploy=item.get("needs_redeploy", False),
                note=item.get("note", ""),
                groovy_hint=item.get("groovy_hint", ""),
                timeout_seconds=item.get("timeout_seconds", 0),
            ))
        return rules

    def classify(self, error_text: str) -> Optional[HealingRule]:
        """
        Match error text to a healing rule.

        Returns the matched HealingRule or None if no match found.
        """
        # 1. Fast regex scan
        for rule in self.rules:
            if re.search(rule.pattern, error_text, re.IGNORECASE | re.DOTALL):
                logger.info(f"Error classified (regex): {rule.id}")
                return rule

        # 2. LLM-assisted classification for ambiguous errors (optional)
        matched_rule = self._classify_with_llm(error_text)
        if matched_rule:
            logger.info(f"Error classified (LLM): {matched_rule.id}")
        else:
            logger.warning("Error not classified — will escalate")
        return matched_rule

    def _classify_with_llm(self, error_text: str) -> Optional[HealingRule]:
        """Ask the configured LLM to identify which healing rule matches the error."""
        llm = self._get_llm()
        if llm is None:
            logger.debug("No LLM configured — skipping AI classification")
            return None

        rule_ids = [r.id for r in self.rules]
        rule_descriptions = "\n".join(
            f"- {r.id}: pattern={r.pattern!r}, note={r.note!r}"
            for r in self.rules
        )
        prompt = f"""You are analyzing an error from a SAP Commerce 2211 jdk21 upgrade.

Known healing rules:
{rule_descriptions}

Error text to classify:
---
{error_text[:3000]}
---

Which healing rule ID best matches this error? Reply with ONLY the rule ID from the list above, or "UNKNOWN" if none match.
Rule IDs: {', '.join(rule_ids)}"""

        try:
            answer = llm(prompt, 50).upper()
            if answer == "UNKNOWN":
                return None
            for rule in self.rules:
                if rule.id == answer:
                    return rule
            for rule in self.rules:
                if answer in rule.id or rule.id in answer:
                    return rule
        except Exception as e:
            logger.warning(f"LLM classification failed: {e}")
        return None


# ------------------------------------------------------------------
# Healing Executor
# ------------------------------------------------------------------

class HealingExecutor:
    """
    Applies the fix for a matched HealingRule.
    """

    def __init__(self, hac_client, local_server, log_reader, env_config: dict):
        self.hac = hac_client
        self.local_server = local_server
        self.log_reader = log_reader
        self.env_config = env_config

    def execute(self, rule: HealingRule, context: "UpgradeContext") -> HealResult:
        """Apply the fix defined in the healing rule."""
        fix = rule.fix
        fix_type = fix.get("type", "")

        logger.info(f"Applying fix for {rule.id} (type={fix_type})")

        if fix_type == "restart_pod":
            return self._fix_restart_pod(rule, fix)

        elif fix_type == "run_system_update":
            return self._fix_run_system_update(rule, context)

        elif fix_type == "run_groovy":
            return self._fix_run_groovy(rule, fix)

        elif fix_type == "run_impex":
            return self._fix_run_impex(rule, fix, context)

        elif fix_type == "trigger_solr_reindex":
            return self._fix_solr_reindex(rule)

        elif fix_type == "wait":
            return self._fix_wait(rule, fix)

        elif fix_type == "needs_redeploy":
            return HealResult(
                healed=False,
                action_taken=f"Fix requires redeploy: {fix.get('action', '')}",
                needs_redeploy=True,
                fix_id=rule.id,
            )

        elif fix_type == "escalate":
            return HealResult(
                healed=False,
                action_taken=f"Escalation required",
                needs_escalation=True,
                escalation_reason=fix.get("reason", rule.note),
                fix_id=rule.id,
            )

        elif fix_type == "inspect_logs":
            return HealResult(
                healed=False,
                action_taken=fix.get("action", "Inspect logs manually"),
                needs_escalation=True,
                escalation_reason=rule.note,
                fix_id=rule.id,
            )

        else:
            return HealResult(
                healed=False,
                action_taken=f"Unknown fix type: {fix_type}",
                needs_escalation=True,
                escalation_reason=f"No handler for fix type '{fix_type}'",
                fix_id=rule.id,
            )

    def _fix_restart_pod(self, rule: HealingRule, fix: dict) -> HealResult:
        # On a local dev server there is no individual pod restart via API.
        # Escalate with clear instructions so the developer restarts manually.
        pod = fix.get("pod", "backoffice")
        return HealResult(
            healed=False,
            action_taken=f"Local server: cannot auto-restart '{pod}' pod",
            needs_escalation=True,
            escalation_reason=(
                f"Local dev server does not support per-pod restarts. "
                f"Stop and restart the local SAP Commerce server manually, "
                f"wait for it to fully start, then re-run the agent from "
                f"the failed phase (e.g. --start-phase 3).\n"
                f"  cd $HYBRIS_HOME/bin/platform && ./hybrisserver.sh stop\n"
                f"  ./hybrisserver.sh start"
            ),
            fix_id=rule.id,
        )

    def _fix_run_system_update(self, rule: HealingRule, context: "UpgradeContext") -> HealResult:
        result = self.hac.trigger_system_update(update_running_system_only=True)
        if not result.triggered:
            return HealResult(
                healed=False,
                action_taken=f"System Update trigger failed: {result.error}",
                needs_escalation=True,
                escalation_reason="Could not trigger System Update from HAC",
                fix_id=rule.id,
            )

        timeout = self.env_config.get("thresholds", {}).get("system_update_timeout_min", 120)
        poll_result = self.hac.poll_init_log_until_done(timeout_minutes=timeout)

        if poll_result["completed"]:
            return HealResult(
                healed=True,
                action_taken="System Update completed successfully",
                fix_id=rule.id,
            )
        elif poll_result["timeout"]:
            return HealResult(
                healed=False,
                action_taken="System Update triggered but timed out",
                needs_escalation=True,
                escalation_reason=f"System Update did not complete within {timeout}min",
                fix_id=rule.id,
            )
        else:
            return HealResult(
                healed=False,
                action_taken="System Update produced errors",
                needs_escalation=True,
                escalation_reason=f"ERROR in init log: {poll_result['last_log'][:500]}",
                fix_id=rule.id,
            )

    def _fix_run_groovy(self, rule: HealingRule, fix: dict) -> HealResult:
        script = fix.get("script", "") or rule.groovy_hint
        if not script:
            return HealResult(
                healed=False,
                action_taken="No Groovy script defined for this fix",
                needs_escalation=True,
                escalation_reason="Missing Groovy script in healing rule",
                fix_id=rule.id,
            )

        result = self.hac.run_groovy(script, commit=True)
        if result.success:
            return HealResult(
                healed=True,
                action_taken=f"Groovy executed: {result.output[:200]}",
                fix_id=rule.id,
            )
        return HealResult(
            healed=False,
            action_taken=f"Groovy failed: {result.error}",
            needs_escalation=True,
            escalation_reason=f"Groovy execution error: {result.error}",
            fix_id=rule.id,
        )

    def _fix_run_impex(self, rule: HealingRule, fix: dict, context: "UpgradeContext") -> HealResult:
        template = fix.get("impex_template", "")
        if not template:
            return HealResult(
                healed=False,
                action_taken="No ImpEx template defined",
                needs_escalation=True,
                escalation_reason="Missing ImpEx template in healing rule",
                fix_id=rule.id,
            )

        # Replace {accstorefront_url} placeholder
        accstorefront_url = self.env_config.get("endpoints", {}).get("accstorefront", "")
        impex = template.replace("{accstorefront_url}", accstorefront_url)

        result = self.hac.run_impex(impex)
        if result.success:
            return HealResult(
                healed=True,
                action_taken=f"ImpEx executed successfully",
                fix_id=rule.id,
            )
        return HealResult(
            healed=False,
            action_taken=f"ImpEx failed: {result.error}",
            needs_escalation=True,
            escalation_reason=f"ImpEx error: {result.error}",
            fix_id=rule.id,
        )

    def _fix_solr_reindex(self, rule: HealingRule) -> HealResult:
        result = self.hac.trigger_solr_full_reindex()
        if result["success"]:
            return HealResult(
                healed=True,
                action_taken=f"Solr reindex triggered: {result['output'][:200]}",
                fix_id=rule.id,
            )
        return HealResult(
            healed=False,
            action_taken=f"Solr reindex failed: {result['error']}",
            needs_escalation=True,
            escalation_reason=f"Solr reindex trigger error: {result['error']}",
            fix_id=rule.id,
        )

    def _fix_wait(self, rule: HealingRule, fix: dict) -> HealResult:
        import time
        wait_secs = fix.get("wait_seconds", 300)
        logger.info(f"Waiting {wait_secs}s (rule: {rule.id}, reason: {rule.note})")
        time.sleep(wait_secs)
        return HealResult(
            healed=True,
            action_taken=f"Waited {wait_secs}s ({rule.note})",
            fix_id=rule.id,
        )


# ------------------------------------------------------------------
# Escalation Handler
# ------------------------------------------------------------------

class EscalationHandler:
    """
    Generates a human-readable escalation report when the agent halts.
    """

    def __init__(self, migration_log_path: str = None):
        self.migration_log_path = migration_log_path
        self._claude = None  # lazy — requires ANTHROPIC_API_KEY

    def _get_llm(self):
        if self._claude is not None:
            return self._claude
        model = os.environ.get("UPGRADE_AGENT_LLM_MODEL", "")
        if not model:
            if os.environ.get("ANTHROPIC_API_KEY"):
                model = "claude-opus-4-6"
            elif os.environ.get("OPENAI_API_KEY"):
                model = "gpt-4o"
            else:
                return None
        try:
            import litellm
            self._claude = lambda prompt, max_tokens: litellm.completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            ).choices[0].message.content.strip()
        except ImportError:
            return None
        return self._claude

    def generate_report(
        self,
        step: str,
        error_text: str,
        fix_attempts: list[dict],
        suggested_action: str = "",
    ) -> str:
        """
        Generate a clear escalation report.

        Args:
            step: The pipeline step that failed (e.g., "3.2").
            error_text: The raw error output.
            fix_attempts: List of attempted fixes with results.
            suggested_action: Human-readable next action.

        Returns:
            Formatted escalation report as string.
        """
        attempts_text = "\n".join(
            f"  - {a.get('fix_id', 'unknown')}: {a.get('result', 'N/A')}"
            for a in fix_attempts
        ) or "  None"

        report = f"""
=== UPGRADE AGENT ESCALATION REPORT ===
Generated: {__import__('datetime').datetime.now().isoformat()}

STEP THAT FAILED: {step}

ERROR:
{error_text[:2000]}

FIX ATTEMPTS:
{attempts_text}

SUGGESTED NEXT ACTION:
{suggested_action or "Review the upgrade log for guidance."}

UPGRADE LOG REFERENCE:
See: {self.migration_log_path or 'knowledge/upgrade-log-template.md'}

=== END REPORT ===
"""
        return report.strip()

    def generate_ai_suggestion(self, step: str, error_text: str) -> str:
        """Use the optional LLM to generate a suggested fix based on upgrade-log context."""
        context = ""
        if self.migration_log_path:
            try:
                with open(self.migration_log_path) as f:
                    context = f.read()[:6000]
            except OSError:
                pass

        prompt = f"""You are an expert on SAP Commerce 2211 jdk21 upgrades.

Migration log context (summarized):
{context}

A step in the upgrade pipeline failed:
Step: {step}
Error: {error_text[:1500]}

Suggest a clear, specific next human action to resolve this. Be direct and technical. Max 3 sentences."""

        llm = self._get_llm()
        if llm is None:
            return "See the upgrade log for known fixes for this error type."

        try:
            return llm(prompt, 300)
        except Exception as e:
            logger.warning(f"LLM suggestion failed: {e}")

        return "See the upgrade log for known fixes for this error type."
