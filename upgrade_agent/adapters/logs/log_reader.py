"""
Local SAP Commerce log reader.

Reads hybris log files from the local server filesystem.
Provides the same interface as the former OpenSearchClient so the
orchestrator and healer need no structural changes.

SAP Commerce log location (default): <hybris>/data/log/*.log
"""
import re
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Log patterns that are always non-critical (suppress from error lists)
_DEFAULT_IGNORE = [
    "OrgUnitAfterInitializationEndEventListener",
    "generateUnitPaths",
    "Could not pre-initialize",
]

# Approximate lines to read per minute of history
_LINES_PER_MINUTE = 200


class LocalLogReader:
    """
    Reads SAP Commerce local log files and provides error-search helpers
    with the same interface as the former OpenSearchClient.
    """

    def __init__(self, log_dir: str, verify_ssl: bool = False):
        # verify_ssl kept for interface compatibility — unused here
        self.log_dir = Path(log_dir) if log_dir else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_log_files(self) -> list[Path]:
        if not self.log_dir or not self.log_dir.exists():
            logger.debug(f"Log directory not found or not set: {self.log_dir}")
            return []
        files = sorted(
            [f for f in self.log_dir.glob("*.log") if f.is_file()],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return files[:5]  # 5 most-recently-modified log files

    def _read_recent_lines(self, minutes: int) -> list[str]:
        """Return an approximate tail of log content covering the last N minutes."""
        files = self._get_log_files()
        if not files:
            return []

        all_lines: list[str] = []
        for f in files[:2]:  # scan the 2 most recent files
            try:
                with open(f, errors="replace") as fh:
                    all_lines.extend(fh.readlines()[-20_000:])
            except OSError as e:
                logger.warning(f"Cannot read {f}: {e}")

        # Tail to approximate the time window
        keep = min(len(all_lines), minutes * _LINES_PER_MINUTE)
        return all_lines[-keep:]

    # ------------------------------------------------------------------
    # Public API (mirrors OpenSearchClient)
    # ------------------------------------------------------------------

    def query_errors_in_last_minutes(
        self,
        minutes: int = 15,
        aspect: Optional[str] = None,
        exclude_patterns: Optional[list] = None,
    ) -> list[dict]:
        """Find ERROR-level lines in recent logs."""
        lines = self._read_recent_lines(minutes)
        ignore = list(_DEFAULT_IGNORE) + (exclude_patterns or [])

        results = []
        for line in lines:
            if "ERROR" not in line:
                continue
            if any(p in line for p in ignore):
                continue
            results.append({"message": line.strip(), "@timestamp": ""})
        return results[:100]

    def search_pattern_in_logs(
        self,
        pattern: str,
        minutes: int = 30,
        aspect: Optional[str] = None,
    ) -> list[dict]:
        """Search for a text / regex pattern in recent log lines."""
        lines = self._read_recent_lines(minutes)
        results = []
        for line in lines:
            if re.search(pattern, line, re.IGNORECASE):
                results.append({"message": line.strip()})
        return results[:50]

    def check_startup_logs_clean(self, aspect: str, minutes: int = 15) -> dict:
        """Check whether recent startup logs contain real errors."""
        errors = self.query_errors_in_last_minutes(minutes=minutes)
        return {
            "clean": len(errors) == 0,
            "error_count": len(errors),
            "errors": [e["message"][:300] for e in errors[:10]],
        }

    def check_azure_integration_reachable(self, minutes: int = 10) -> dict:
        """Look for outbound HTTP connection errors in recent logs."""
        connection_errors = [
            "Connection refused",
            "SocketTimeoutException",
            "Connection timed out",
            "UnknownHostException",
        ]
        issues = []
        for pat in connection_errors:
            hits = self.search_pattern_in_logs(pat, minutes=minutes)
            if hits:
                issues.append(f"{pat}: {len(hits)} hit(s)")
        return {"reachable": len(issues) == 0, "issues": issues}

    def get_recent_log_summary(self, aspect: str, minutes: int = 5) -> str:
        errors = self.query_errors_in_last_minutes(minutes=minutes)
        if not errors:
            return f"No errors in local logs (last {minutes}min)"
        lines = [f"{len(errors)} error(s) in last {minutes}min:"]
        for e in errors[:5]:
            lines.append(f"  {e['message'][:200]}")
        return "\n".join(lines)

    def wait_for_pattern(
        self,
        pattern: str,
        timeout_seconds: int = 1800,
        poll_interval: int = 30,
        aspect: Optional[str] = None,
    ) -> bool:
        """Wait until pattern appears in log files or timeout expires."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.search_pattern_in_logs(pattern, minutes=2):
                return True
            time.sleep(poll_interval)
        return False
