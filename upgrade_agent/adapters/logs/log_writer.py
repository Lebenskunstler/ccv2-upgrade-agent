"""
LogWriter — appends structured entries to upgrade-LOG.md.

Every agent action (step start, step done, gate result, fix applied,
escalation, finding) is written as a timestamped row in the log.

Format matches the existing upgrade-LOG.md: markdown table rows +
narrative blocks for detail, so a human reading the file sees the same
style as the entries written during real CCv2 upgrade runs.
"""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class LogWriter:
    """
    Thread-safe append-only writer for upgrade-LOG.md.

    All writes go to the 'Agent Session Log' section at the bottom,
    which is created on first write if it doesn't exist.
    """

    _SECTION_HEADER = "## Agent Session Log"
    _TABLE_HEADER = (
        "| Timestamp (UTC) | Step | Status | Details |\n"
        "|----------------|------|--------|---------|\n"
    )

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self._session_start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        self._ensure_section()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _ensure_section(self):
        """Create the agent session section if it doesn't exist yet."""
        if not self.log_path.exists():
            logger.warning(f"Log file not found — will create: {self.log_path}")
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(
                f"# JDK 21 Migration Log\n\n"
                f"{self._SECTION_HEADER}\n\n"
                f"**Session started:** {self._session_start} UTC\n\n"
                f"{self._TABLE_HEADER}"
            )
            return

        text = self.log_path.read_text()
        if self._SECTION_HEADER not in text:
            with self.log_path.open("a") as f:
                f.write(
                    f"\n\n---\n\n"
                    f"{self._SECTION_HEADER}\n\n"
                    f"**Session started:** {self._session_start} UTC\n\n"
                    f"{self._TABLE_HEADER}"
                )

    def _append_row(self, step: str, status: str, details: str):
        """Append a single markdown table row."""
        clean = details.replace("|", "\\|").replace("\n", " ").strip()
        row = f"| {self._ts()} | {step} | {status} | {clean[:300]} |\n"
        with self.log_path.open("a") as f:
            f.write(row)

    def _append_block(self, content: str):
        """Append a freeform markdown block (narrative / code block)."""
        with self.log_path.open("a") as f:
            f.write("\n" + content.rstrip() + "\n")

    # ------------------------------------------------------------------
    # Public logging API
    # ------------------------------------------------------------------

    def log_step_start(self, step_id: str, title: str):
        self._append_row(step_id, "⏳ RUNNING", title)
        logger.debug(f"LOG: step start {step_id} — {title}")

    def log_step_done(
        self,
        step_id: str,
        title: str,
        passed: bool,
        details: str = "",
        fix_applied: str = "",
    ):
        status = "✅ PASS" if passed else "❌ FAIL"
        note = details
        if fix_applied:
            note = f"{details} | Fix: {fix_applied}"
        self._append_row(step_id, status, f"{title} — {note}")

    def log_gate_result(self, gate_id: int, gate_name: str, passed: bool, detail: str):
        status = "✅ PASS" if passed else "❌ FAIL"
        self._append_row(f"GATE-{gate_id}", status, f"[{gate_name}] {detail}")

    def log_fix_applied(self, step_id: str, rule_id: str, action_taken: str):
        self._append_row(step_id, "🔧 FIX", f"[{rule_id}] {action_taken}")

    def log_finding(self, finding: str, step_id: str = ""):
        self._append_row(step_id or "FINDING", "ℹ️ INFO", finding)

    def log_escalation(self, step_id: str, error_text: str, suggested_action: str):
        self._append_row(step_id, "🚨 ESCALATE", f"Human action required: {suggested_action}")
        self._append_block(
            f"\n### Escalation — {step_id} — {self._ts()}\n\n"
            f"**Error:**\n```\n{error_text[:1500]}\n```\n\n"
            f"**Suggested action:** {suggested_action}\n"
        )

    def log_version_fix_found(self, error_text: str, fix_version: str):
        short_error = re.sub(r'\s+', ' ', error_text).strip()[:100]
        self._append_row(
            "VERSION-CHECK", "⚠️ NOTE",
            f"Error '{short_error}' is fixed in {fix_version} — consider upgrading platform version",
        )

    def log_code_navigation(self, step_id: str, error_type: str, file_paths: list[str]):
        files = ", ".join(file_paths[:5]) + ("..." if len(file_paths) > 5 else "")
        self._append_row(step_id, "🔍 NAV", f"[{error_type}] Inspect: {files}")

    def log_session_summary(
        self,
        gates_passed: bool,
        total_steps: int,
        failed_steps: list[str],
        fixes_applied: list[str],
    ):
        status = "✅ UPGRADE COMPLETE" if gates_passed else "❌ UPGRADE INCOMPLETE"
        detail = (
            f"Steps: {total_steps} | "
            f"Failed: {failed_steps or 'none'} | "
            f"Fixes: {fixes_applied or 'none'}"
        )
        self._append_row("SESSION", status, detail)

        if gates_passed:
            self._append_block(
                f"\n### ✅ Session Complete — {self._ts()}\n\n"
                f"All 3 gates passed. Upgrade pipeline finished successfully.\n"
                f"- Total steps: {total_steps}\n"
                f"- Fixes applied: {len(fixes_applied)}\n"
            )
        else:
            self._append_block(
                f"\n### ❌ Session Ended — Gates Not All Green — {self._ts()}\n\n"
                f"- Failing gates / steps: {failed_steps}\n"
                f"- Fixes applied this session: {fixes_applied}\n"
                f"- Resume from the first failing step.\n"
            )
