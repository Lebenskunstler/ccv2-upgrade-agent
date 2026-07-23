"""
Local SAP Commerce server management — ant build + server startup check.

Replaces the former CCv2Client. All operations are against the local
SAP Commerce server (localhost), not the cloud portal.
"""
import subprocess
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    success: bool
    output: str
    error: Optional[str] = None


class LocalServer:
    """
    Manages the local SAP Commerce server.

    - Runs ant builds (clean all) in the platform directory.
    - Checks whether the local server is up and responding.
    """

    def __init__(
        self,
        hybris_dir: str,
        hac_url: str = "https://localhost:9002",
        verify_ssl: bool = False,
    ):
        self.hybris_dir = Path(hybris_dir) if hybris_dir else Path(".")
        self.platform_dir = self.hybris_dir / "bin" / "platform"
        self.hac_url = hac_url.rstrip("/")
        self.verify_ssl = verify_ssl

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def run_ant(self, target: str = "clean all", timeout_seconds: int = 7200) -> BuildResult:
        """
        Run an ant target inside the platform directory.

        Sources setantenv.sh first so Java/Ant paths are correct.

        Returns BuildResult with success=True only on BUILD SUCCESSFUL.
        """
        setantenv = self.platform_dir / "setantenv.sh"
        if not setantenv.exists():
            return BuildResult(
                success=False,
                output="",
                error=(
                    f"setantenv.sh not found at {setantenv}. "
                    f"Check that hybris_dir is set correctly in config or HYBRIS_HOME is exported."
                ),
            )

        cmd = f"cd '{self.platform_dir}' && . ./setantenv.sh && ant {target}"
        logger.info(f"Running: ant {target} in {self.platform_dir}")

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            output = (result.stdout or "") + (result.stderr or "")
            tail = output[-5000:]  # last 5 KB for reporting

            if result.returncode == 0 and "BUILD SUCCESSFUL" in output:
                logger.info("ant: BUILD SUCCESSFUL")
                return BuildResult(success=True, output=tail)

            error_lines = [
                line for line in output.splitlines()
                if "ERROR" in line or "BUILD FAILED" in line
            ]
            return BuildResult(
                success=False,
                output=tail,
                error="\n".join(error_lines[-20:]) or f"BUILD FAILED (exit code {result.returncode})",
            )

        except subprocess.TimeoutExpired:
            return BuildResult(
                success=False,
                output="",
                error=f"ant {target} timed out after {timeout_seconds}s",
            )
        except Exception as e:
            return BuildResult(success=False, output="", error=str(e))

    # ------------------------------------------------------------------
    # Server health
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """Return True if the local server login page responds (< 500)."""
        try:
            resp = requests.get(
                f"{self.hac_url}/hac/login",
                verify=self.verify_ssl,
                timeout=10,
                allow_redirects=True,
            )
            return resp.status_code < 500
        except requests.RequestException:
            return False

    def wait_for_server(self, timeout_minutes: int = 30, poll_interval: int = 30) -> bool:
        """
        Poll until the local server is responding or timeout expires.

        Returns True when server is up, False on timeout.
        """
        deadline = time.time() + timeout_minutes * 60
        logger.info(f"Waiting for local server at {self.hac_url}…")

        while time.time() < deadline:
            if self.is_running():
                logger.info("Local server is responding")
                return True
            logger.info(f"Server not yet up, retrying in {poll_interval}s…")
            time.sleep(poll_interval)

        logger.error(f"Server did not come up within {timeout_minutes}min")
        return False

    def start_server(self, timeout_minutes: int = 30) -> bool:
        """
        Start the local SAP Commerce server in background and wait for it to be up.

        Runs hybrisserver.sh start in background. Returns True when HAC responds.
        No-op and returns True if the server is already running.
        """
        if self.is_running():
            logger.info("Server already running — nothing to start")
            return True

        hybrisserver = self.platform_dir / "hybrisserver.sh"
        if not hybrisserver.exists():
            logger.error(f"hybrisserver.sh not found at {hybrisserver}")
            return False

        logger.info(f"Starting SAP Commerce server in background…")
        try:
            subprocess.Popen(
                [str(hybrisserver), "start"],
                cwd=str(self.platform_dir),
                stdout=open("/tmp/hybrisserver.log", "w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            logger.error(f"Failed to start server: {e}")
            return False

        return self.wait_for_server(timeout_minutes=timeout_minutes)

    def stop_server(self) -> bool:
        """
        Stop the local SAP Commerce server.

        First tries hybrisserver.sh stop. If the server is still responding
        afterwards (e.g. PID file mismatch from start_new_session), falls back
        to SIGTERM on the wrapper PID file.
        """
        hybrisserver = self.platform_dir / "hybrisserver.sh"
        pid_file = self.platform_dir / "tomcat" / "bin" / "hybrisPlatform.pid"

        # 1. Try the normal stop script
        if hybrisserver.exists():
            try:
                subprocess.run(
                    [str(hybrisserver), "stop"],
                    cwd=str(self.platform_dir),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except Exception as e:
                logger.warning(f"hybrisserver.sh stop raised: {e}")

        # 2. Verify — if HAC is still up, kill via PID file
        time.sleep(3)
        if not self.is_running():
            logger.info("Server stopped")
            return True

        logger.warning("Server still up after hybrisserver.sh stop — attempting PID-file kill")
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                import os, signal
                os.kill(pid, signal.SIGTERM)
                logger.info(f"Sent SIGTERM to wrapper PID {pid}")
                time.sleep(10)
            except Exception as e:
                logger.error(f"PID file kill failed: {e}")

        if not self.is_running():
            logger.info("Server stopped (via PID file)")
            return True

        logger.error("Server is still responding after stop attempts")
        return False

    def run_installer(
        self,
        recipe: str = "cx",
        task: str = "setup",
        admin_password: str = "Admin1234",
        extra_args: Optional[list] = None,
        timeout_seconds: int = 1800,
    ) -> BuildResult:
        """
        Run the SAP Commerce Gradle installer for a given recipe and task.

        Example: run_installer("cx", "setup", admin_password="Admin1234")
        runs: ./install.sh -r cx setup -A initAdminPassword=Admin1234
        """
        installer_dir = self.hybris_dir.parent / "installer"
        install_sh = installer_dir / "install.sh"

        if not install_sh.exists():
            return BuildResult(
                success=False, output="",
                error=f"install.sh not found at {install_sh}. Check hybris_dir path.",
            )

        cmd_parts = [str(install_sh), "-r", recipe, task, f"-A initAdminPassword={admin_password}"]
        if extra_args:
            cmd_parts.extend(extra_args)

        cmd = " ".join(cmd_parts)
        logger.info(f"Running installer: {cmd}")

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(installer_dir),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            output = (result.stdout or "") + (result.stderr or "")
            tail = output[-5000:]

            if result.returncode == 0:
                return BuildResult(success=True, output=tail)

            error_lines = [l for l in output.splitlines() if "ERROR" in l or "FAILED" in l]
            return BuildResult(
                success=False, output=tail,
                error="\n".join(error_lines[-20:]) or f"Installer failed (exit {result.returncode})",
            )
        except subprocess.TimeoutExpired:
            return BuildResult(
                success=False, output="",
                error=f"Installer {task} timed out after {timeout_seconds}s",
            )
