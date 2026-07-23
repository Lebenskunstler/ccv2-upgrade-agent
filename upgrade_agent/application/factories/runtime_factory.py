import os
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from upgrade_agent.adapters.hac.hac_client import HACClient
from upgrade_agent.adapters.server.local_server import LocalServer
from upgrade_agent.adapters.logs.log_reader import LocalLogReader
from upgrade_agent.adapters.healing.healer import ErrorClassifier, HealingExecutor, EscalationHandler
from upgrade_agent.adapters.reporting.gate_checker import GateChecker
from upgrade_agent.adapters.reporting.gate_reporter import GateReporter
from upgrade_agent.workflows.classic_pipeline import UpgradeContext

logger = logging.getLogger(__name__)


@dataclass
class RuntimeDependencies:
    hac: HACClient
    local_server: LocalServer
    log_reader: LocalLogReader
    classifier: ErrorClassifier
    healer: HealingExecutor
    escalation_handler: EscalationHandler
    context: UpgradeContext
    gate_checker: GateChecker


def build_runtime_dependencies(
    config: dict,
    env: str,
    manifest_path: str,
    healing_map_path: Path,
    upgrade_log_template_path: Path,
) -> RuntimeDependencies:
    hac, local_server, log_reader = _build_clients(config)
    context = UpgradeContext(env=env, config=config, manifest_path=manifest_path)

    migration_log_path = str(upgrade_log_template_path) if upgrade_log_template_path.exists() else None
    classifier = ErrorClassifier(
        healing_map_path=str(healing_map_path),
        migration_log_path=migration_log_path,
    )
    healer = HealingExecutor(hac, local_server, log_reader, config)
    escalation_handler = EscalationHandler(migration_log_path=migration_log_path)

    gate_checker = GateChecker(
        local_server,
        hac,
        log_reader,
        config,
        reporter=_build_reporter(config),
    )

    return RuntimeDependencies(
        hac=hac,
        local_server=local_server,
        log_reader=log_reader,
        classifier=classifier,
        healer=healer,
        escalation_handler=escalation_handler,
        context=context,
        gate_checker=gate_checker,
    )


def _build_clients(config: dict) -> tuple[HACClient, LocalServer, LocalLogReader]:
    hac_cfg = config.get("hac", {})
    local_cfg = config.get("local_server", {})
    log_cfg = config.get("log", {})

    hac_url = hac_cfg.get("base_url", "https://localhost:9002")
    hybris_dir = local_cfg.get("hybris_dir", os.environ.get("HYBRIS_HOME", ""))

    hac = HACClient(
        base_url=hac_url,
        username=hac_cfg.get("username", "admin"),
        password=os.environ.get(hac_cfg.get("password_env", "HAC_PASSWORD_LOCAL"), ""),
        verify_ssl=config.get("verify_ssl", False),
    )

    local_server = LocalServer(
        hybris_dir=hybris_dir,
        hac_url=local_cfg.get("hac_url", hac_url),
        verify_ssl=config.get("verify_ssl", False),
    )

    default_log_dir = os.path.join(hybris_dir, "data", "log") if hybris_dir else ""
    log_dir = log_cfg.get("dir", default_log_dir)
    log_reader = LocalLogReader(log_dir=log_dir)

    return hac, local_server, log_reader


def _build_reporter(config: dict) -> Optional[GateReporter]:
    reports_root = config.get("reports_root", "")
    if not reports_root:
        return None

    from_ver = config.get("from_version", "unknown")
    to_ver = config.get("platform_version", config.get("target_version", "unknown"))
    try:
        return GateReporter(
            reports_root=reports_root,
            from_version=from_ver,
            to_version=to_ver,
        )
    except Exception as exc:
        logger.warning(f"Could not initialise GateReporter: {exc}")
        return None
