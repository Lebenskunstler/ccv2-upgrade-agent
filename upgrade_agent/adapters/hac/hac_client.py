"""
HAC REST client — Groovy execution, ImpEx import, System Update, init log polling.

Authentication: Spring Security form login with CSRF token management.
"""
import re
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class GroovyResult:
    success: bool
    output: str
    execution_result: str
    error: Optional[str] = None


@dataclass
class ImpExResult:
    success: bool
    output: str
    error: Optional[str] = None


@dataclass
class SystemUpdateResult:
    triggered: bool
    error: Optional[str] = None


class HACClient:
    """
    Wraps SAP Commerce HAC REST endpoints.

    Authentication flow:
        1. GET /hac/login → extract CSRF token from HTML
        2. POST /hac/j_spring_security_check with credentials + CSRF → session cookie
        3. All subsequent requests include session cookie + refreshed CSRF
    """

    def __init__(self, base_url: str, username: str, password: str, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self._csrf_token: Optional[str] = None
        self._authenticated = False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _get_csrf_token(self, url: str) -> Optional[str]:
        resp = self.session.get(url, verify=self.verify_ssl, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        tag = soup.find("input", {"name": "_csrf"})
        if tag:
            return tag.get("value")
        # Try meta tag fallback
        meta = soup.find("meta", {"name": "_csrf"})
        if meta:
            return meta.get("content")
        return None

    def login(self) -> bool:
        login_url = f"{self.base_url}/hac/login"
        try:
            csrf = self._get_csrf_token(login_url)
            if not csrf:
                logger.warning("CSRF token not found on login page, attempting without")
                csrf = ""

            # Check the direct POST response (without following redirects) to determine success.
            # Spring Security redirects to /login?error on bad credentials, or to the target URL on success.
            post_resp = self.session.post(
                f"{self.base_url}/hac/j_spring_security_check",
                data={
                    "j_username": self.username,
                    "j_password": self.password,
                    "_csrf": csrf,
                },
                allow_redirects=False,
                verify=self.verify_ssl,
                timeout=20,
            )

            location = post_resp.headers.get("Location", "")
            if post_resp.status_code == 302 and ("?error" in location or "/login?error" in location):
                logger.error("HAC login failed — bad credentials (redirected to /login?error)")
                return False

            # Follow redirects to establish the session cookie on all paths
            if post_resp.status_code == 302 and location:
                self.session.get(
                    location if location.startswith("http") else f"{self.base_url}{location}",
                    verify=self.verify_ssl,
                    timeout=20,
                    allow_redirects=True,
                )

            self._authenticated = True
            logger.info("HAC login successful")
            return True

        except requests.RequestException as e:
            logger.error(f"HAC login error: {e}")
            return False

    def _ensure_authenticated(self):
        if not self._authenticated:
            if not self.login():
                raise RuntimeError("HAC authentication failed")

    def _refresh_csrf(self) -> str:
        """Fetch a fresh CSRF token for form submissions."""
        try:
            csrf = self._get_csrf_token(f"{self.base_url}/hac/console/groovy/index")
            return csrf or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Returns True if HAC responds with 200."""
        try:
            resp = requests.get(
                f"{self.base_url}/hac/login",
                verify=self.verify_ssl,
                timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    # ------------------------------------------------------------------
    # Groovy execution
    # ------------------------------------------------------------------

    def run_groovy(self, script: str, commit: bool = False) -> GroovyResult:
        """
        Execute a Groovy script in the HAC console.

        Args:
            script: The Groovy script text.
            commit: Whether to commit the transaction (default False — safe mode).

        Returns:
            GroovyResult with output and execution_result.
        """
        self._ensure_authenticated()
        csrf = self._refresh_csrf()

        try:
            resp = self.session.post(
                f"{self.base_url}/hac/console/groovy/execute",
                data={
                    "script": script,
                    "commit": "true" if commit else "false",
                    "_csrf": csrf,
                },
                verify=self.verify_ssl,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

            output = data.get("outputText", "")
            execution_result = data.get("executionResult", "")
            exception_text = data.get("exceptionMsg", "")

            if exception_text:
                return GroovyResult(
                    success=False,
                    output=output,
                    execution_result=execution_result,
                    error=exception_text,
                )

            return GroovyResult(
                success=True,
                output=output,
                execution_result=execution_result,
            )

        except requests.RequestException as e:
            return GroovyResult(success=False, output="", execution_result="", error=str(e))

    # ------------------------------------------------------------------
    # ImpEx import
    # ------------------------------------------------------------------

    def run_impex(self, impex_content: str, validation_mode: str = "IMPORT_STRICT") -> ImpExResult:
        """
        Import ImpEx content via HAC.

        Args:
            impex_content: The ImpEx script text.
            validation_mode: IMPORT_STRICT, IMPORT_RELAXED, or EXPORT.
        """
        self._ensure_authenticated()
        csrf = self._refresh_csrf()

        try:
            resp = self.session.post(
                f"{self.base_url}/hac/impex/import",
                data={
                    "scriptContent": impex_content,
                    "encoding": "UTF-8",
                    "validationEnum": validation_mode,
                    "maxThreads": "1",
                    "enableCodeExecution": "true",
                    "_csrf": csrf,
                },
                verify=self.verify_ssl,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("hasError") or data.get("errorCount", 0) > 0:
                error_lines = data.get("errorLines", [])
                return ImpExResult(
                    success=False,
                    output=str(data),
                    error="\n".join(str(e) for e in error_lines),
                )

            return ImpExResult(success=True, output=str(data))

        except requests.RequestException as e:
            return ImpExResult(success=False, output="", error=str(e))

    # ------------------------------------------------------------------
    # System Update
    # ------------------------------------------------------------------

    def _fetch_update_page_config(self, update_running_system_only: bool = True) -> tuple[str, dict]:
        """
        Build the exact payload that HAC UI sends when clicking "Update".

        Uses two AJAX endpoints (both JS-rendered, not in static HTML):
          - GET /hac/platform/init/pendingPatches  → patches dict
          - GET /hac/platform/init/data            → allParameters (projectDatas)

        update_running_system_only=True  → essential=false, localizeTypes=false,
                                           createProjectData=false (patches only)
        update_running_system_only=False → essential=true,  localizeTypes=true,
                                           createProjectData=true
        """
        # 1. GET update page — only needed for CSRF meta tag
        resp = self.session.get(
            f"{self.base_url}/hac/platform/update",
            verify=self.verify_ssl,
            timeout=15,
        )
        resp.raise_for_status()
        import re as _re
        m = _re.search(r'<meta name="_csrf" content="([^"]+)"', resp.text)
        csrf = m.group(1) if m else ""

        headers = {
            "Accept": "application/json",
            "X-CSRF-TOKEN": csrf,
        }

        # 2. pendingPatches → patches dict {extName: [hash, ...]}
        patches: dict = {}
        try:
            pr = self.session.get(
                f"{self.base_url}/hac/platform/init/pendingPatches",
                headers=headers,
                verify=self.verify_ssl,
                timeout=15,
            )
            if pr.status_code == 200:
                raw = pr.json()
                # raw: {extName: [{hash, name, ...}, ...]}
                for ext_name, patch_list in raw.items():
                    hashes = [p["hash"] for p in patch_list if "hash" in p]
                    if hashes:
                        patches[ext_name] = hashes
        except Exception as exc:
            logger.warning(f"Could not fetch pendingPatches: {exc}")

        # 3. init/data → allParameters
        # Each projectData entry: {name, parameter: [{name, values: {val: isSelected}, legacy, ...}]}
        # Key format: "{extName}_{paramName}", value: [selected_val]
        # Selected val = first key with True; fallback = first key (legacy/unset)
        all_parameters: dict = {}
        try:
            dr = self.session.get(
                f"{self.base_url}/hac/platform/init/data",
                headers=headers,
                verify=self.verify_ssl,
                timeout=15,
            )
            if dr.status_code == 200:
                init_data = dr.json()
                for pd in init_data.get("projectDatas", []):
                    ext_name = pd.get("name", "")
                    for param in pd.get("parameter", []):
                        param_name = param.get("name", "")
                        values: dict = param.get("values", {})
                        # Find the selected value (True) or fall back to first key
                        selected = next(
                            (v for v, selected in values.items() if selected),
                            next(iter(values), None),
                        )
                        if param_name and selected is not None:
                            all_parameters[f"{ext_name}_{param_name}"] = [selected]
        except Exception as exc:
            logger.warning(f"Could not fetch init/data: {exc}")

        payload = {
            "dropTables": False,
            "clearHMC": False,
            "createEssentialData": False,
            "localizeTypes": False,
            "createProjectData": False,
            "allParameters": all_parameters,
            "patches": patches,
            "initMethod": "UPDATE",
        }

        # For each extension with pending patches, add "<extName>_sample": "true"
        # to allParameters — this triggers SystemSetup to run for that extension,
        # which then applies the pending patches.
        for ext_name in patches:
            payload["allParameters"][f"{ext_name}_sample"] = "true"

        pending_patches = sum(len(v) for v in patches.values())
        logger.info(
            f"Update page config — updateRunningSystemOnly={update_running_system_only}, "
            f"pending patches={pending_patches}, "
            f"patch sample triggers={list(patches.keys())}"
        )
        return csrf, payload

    def dump_configuration(self, update_running_system_only: bool = True) -> dict:
        """
        POST /hac/platform/dumpConfiguration with the live page state.
        Returns the JSON the HAC UI would show when clicking 'Dump configuration'.
        Useful for logging/verification before triggering the actual update.
        """
        self._ensure_authenticated()
        csrf, payload = self._fetch_update_page_config(update_running_system_only=update_running_system_only)
        resp = self.session.post(
            f"{self.base_url}/hac/platform/dumpConfiguration",
            json=payload,
            headers={
                "X-CSRF-TOKEN": csrf,
                "Accept": "application/json",
            },
            verify=self.verify_ssl,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"dumpConfiguration returned {resp.status_code}"}

    def trigger_system_update(self, update_running_system_only: bool = True) -> SystemUpdateResult:
        """
        Trigger a HAC System Update using the live page state.

        Fetches GET /hac/platform/update, parses all checked patches and
        extension flags (mirrors prepareInitUpdateData() in init.js), then
        POSTs the exact same payload to /hac/platform/init/execute.

        This ensures pending patches are included and no hardcoded flags are used.
        """
        self._ensure_authenticated()

        try:
            csrf, payload = self._fetch_update_page_config(update_running_system_only=update_running_system_only)
        except Exception as exc:
            return SystemUpdateResult(triggered=False, error=f"Failed to parse update page: {exc}")

        # Log the config before executing
        try:
            dump = self.session.post(
                f"{self.base_url}/hac/platform/dumpConfiguration",
                json=payload,
                headers={"X-CSRF-TOKEN": csrf, "Accept": "application/json"},
                verify=self.verify_ssl,
                timeout=30,
            )
            if dump.status_code == 200:
                import json as _json
                logger.info(f"System Update config: {_json.dumps(dump.json(), indent=2)[:800]}")
        except Exception:
            pass  # dump is optional — don't block on it

        try:
            resp = self.session.post(
                f"{self.base_url}/hac/platform/init/execute",
                json=payload,
                headers={"X-CSRF-TOKEN": csrf, "Accept": "application/json"},
                verify=self.verify_ssl,
                timeout=120,
            )

            if resp.status_code in (200, 302):
                logger.info("System Update triggered successfully")
                return SystemUpdateResult(triggered=True)
            else:
                return SystemUpdateResult(
                    triggered=False,
                    error=f"Unexpected status {resp.status_code}: {resp.text[:500]}",
                )

        except requests.Timeout:
            logger.info("POST timed out — System Update likely started; will poll init log")
            return SystemUpdateResult(triggered=True)
        except requests.RequestException as e:
            return SystemUpdateResult(triggered=False, error=str(e))

    # ------------------------------------------------------------------
    # Init log polling
    # ------------------------------------------------------------------

    def poll_init_log_until_done(self, timeout_minutes: int = 120, poll_interval: int = 30) -> dict:
        """
        Poll the HAC /platform/log endpoint until System Update is complete.

        During System Update, HAC redirects all pages to /platform/init.
        The /platform/log endpoint returns JSON with the current init log lines.
        When the init log is empty, the update is done.

        Returns:
            dict with keys: completed (bool), timeout (bool), error_detected (bool), last_log (str)
        """
        deadline = time.time() + timeout_minutes * 60
        last_log = ""
        consecutive_empty = 0

        logger.info(f"Polling /platform/log (timeout: {timeout_minutes}min)...")

        # Wait for the init lock to set in before polling.
        # The platform takes a few seconds to start the init process and begin
        # redirecting /platform/update to /platform/init. Probing immediately
        # returns 200 (page loads normally) and causes a false-positive "done".
        logger.info("Waiting 20s for System Update lock to set in...")
        time.sleep(20)

        while time.time() < deadline:
            try:
                # Re-authenticate if session expired
                if not self.health_check():
                    logger.warning("Server not responding during init poll — retrying...")
                    time.sleep(poll_interval)
                    continue

                # Probe whether init is still locked: /platform/update returns 302 during init.
                # Use allow_redirects=False to inspect the redirect destination:
                #   - 200            → init done (authenticated session)
                #   - 302 → /login  → init done but session expired; re-login and re-probe
                #   - 302 → /platform/init → init still running
                probe = self.session.get(
                    f"{self.base_url}/hac/platform/update",
                    verify=self.verify_ssl,
                    timeout=15,
                    allow_redirects=False,
                )

                if probe.status_code == 200:
                    logger.info("HAC /platform/update accessible (200) — System Update complete")
                    return {
                        "completed": True,
                        "timeout": False,
                        "error_detected": False,
                        "last_log": last_log,
                    }

                redirect_to = probe.headers.get("Location", "")
                if probe.status_code == 302 and "/login" in redirect_to:
                    # Session expired but init is done — force re-login and confirm
                    logger.info("Session expired after System Update — re-authenticating")
                    self._authenticated = False
                    if self.login():
                        recheck = self.session.get(
                            f"{self.base_url}/hac/platform/update",
                            verify=self.verify_ssl,
                            timeout=15,
                            allow_redirects=False,
                        )
                        if recheck.status_code == 200:
                            logger.info("System Update confirmed complete after re-auth")
                            return {
                                "completed": True,
                                "timeout": False,
                                "error_detected": False,
                                "last_log": last_log,
                            }
                    # Re-auth succeeded but still redirecting — keep polling
                    time.sleep(poll_interval)
                    continue

                # Still redirecting to /platform/init — init still running, check log
                self._ensure_authenticated()
                log_resp = self.session.get(
                    f"{self.base_url}/hac/platform/log",
                    verify=self.verify_ssl,
                    timeout=15,
                )

                # /platform/log returns JSON array of log lines when init is running.
                # Skip HTML responses (redirected login/init pages) — only log JSON content.
                if log_resp.status_code == 200:
                    log_content = ""
                    ct = log_resp.headers.get("Content-Type", "")
                    if "json" in ct or log_resp.text.lstrip().startswith("["):
                        try:
                            import json as _json
                            entries = _json.loads(log_resp.text)
                            log_content = "\n".join(
                                str(e.get("message", e)) if isinstance(e, dict) else str(e)
                                for e in (entries if isinstance(entries, list) else [])
                            )
                        except Exception:
                            pass  # not valid JSON — ignore

                    if log_content.strip() and log_content != last_log:
                        logger.info(f"Init log: {log_content[-300:]}")
                        last_log = log_content

                    # Empty log = done
                    if not log_content.strip():
                        consecutive_empty += 1
                        if consecutive_empty >= 2:
                            logger.info("Init log empty × 2 — System Update complete")
                            return {
                                "completed": True,
                                "timeout": False,
                                "error_detected": False,
                                "last_log": last_log,
                            }
                    else:
                        consecutive_empty = 0
                        if re.search(r"\bERROR\b", log_content, re.IGNORECASE):
                            logger.warning(f"ERROR in init log: {log_content[-500:]}")
                            return {
                                "completed": False,
                                "timeout": False,
                                "error_detected": True,
                                "last_log": log_content,
                            }

            except Exception as e:
                logger.warning(f"Init log poll error: {e}")

            time.sleep(poll_interval)

        logger.error(f"Init log poll timed out after {timeout_minutes}min")
        return {
            "completed": False,
            "timeout": True,
            "error_detected": False,
            "last_log": last_log,
        }

    # ------------------------------------------------------------------
    # FlexSearch
    # ------------------------------------------------------------------

    def run_flexsearch(self, query: str) -> dict:
        """Run a FlexSearch query via Groovy and return results."""
        script = f"""
import de.hybris.platform.servicelayer.search.FlexibleSearchQuery
def q = new FlexibleSearchQuery("{query}")
def r = flexibleSearchService.search(q)
return r.result
"""
        result = self.run_groovy(script, commit=False)
        return {
            "success": result.success,
            "result": result.execution_result,
            "output": result.output,
            "error": result.error,
        }

    def count_employees_with_pbkdf2(self) -> int:
        """Count employees with pbkdf2-encoded passwords (pre-migration check)."""
        script = """
import de.hybris.platform.servicelayer.search.FlexibleSearchQuery
def q = new FlexibleSearchQuery("SELECT count({pk}) FROM {Employee} WHERE {encodedPassword} LIKE :prefix")
q.addQueryParameter("prefix", "pbkdf2%")
def r = flexibleSearchService.search(q)
return r.result[0]
"""
        result = self.run_groovy(script, commit=False)
        try:
            val = result.execution_result.strip()
            return int(val)
        except (ValueError, AttributeError):
            return -1

    def count_sap_oauth2_authorizations(self) -> int:
        """Check if SAPOAuth2Authorization type is registered."""
        script = """
try {
    import de.hybris.platform.servicelayer.search.FlexibleSearchQuery
    def q = new FlexibleSearchQuery("SELECT count({pk}) FROM {SAPOAuth2Authorization}")
    def r = flexibleSearchService.search(q)
    return r.result[0]
} catch (Exception e) {
    return "ERROR: " + e.message
}
"""
        result = self.run_groovy(script, commit=False)
        try:
            val = result.execution_result.strip()
            if val.startswith("ERROR"):
                return -1
            return int(val)
        except (ValueError, AttributeError):
            return -1

    def get_product_counts_per_catalog(self, catalog_versions: list) -> dict:
        """
        Get product count for each catalog version.

        Args:
            catalog_versions: List of "catalogId/catalogVersion" strings, e.g. ["ozexportProductCatalog/Online"]

        Returns:
            dict mapping catalog_version → count
        """
        counts = {}
        for cv_str in catalog_versions:
            parts = cv_str.split("/")
            if len(parts) != 2:
                continue
            catalog_id, version = parts
            script = f"""
import de.hybris.platform.servicelayer.search.FlexibleSearchQuery
def q = new FlexibleSearchQuery(
    "SELECT count({{p.pk}}) FROM {{Product AS p JOIN CatalogVersion AS cv ON {{p.catalogVersion}} = {{cv.pk}} JOIN Catalog AS c ON {{cv.catalog}} = {{c.pk}}}} WHERE {{c.id}} = :cid AND {{cv.version}} = :ver"
)
q.addQueryParameter("cid", "{catalog_id}")
q.addQueryParameter("ver", "{version}")
def r = flexibleSearchService.search(q)
return r.result[0]
"""
            result = self.run_groovy(script, commit=False)
            try:
                counts[cv_str] = int(result.execution_result.strip())
            except (ValueError, AttributeError):
                counts[cv_str] = -1
        return counts

    def trigger_solr_full_reindex(self) -> dict:
        """Trigger full Solr reindex for all indexes via cronJobService."""
        script = """
import de.hybris.platform.servicelayer.search.FlexibleSearchQuery
def q = new FlexibleSearchQuery("SELECT {pk} FROM {SolrIndexerCronJob} WHERE {code} LIKE '%full%'")
def r = flexibleSearchService.search(q)
def results = []
r.result.each { cj ->
    try {
        cronJobService.performCronJob(cj, true)
        results << "Triggered: ${cj.code}"
    } catch (e) {
        results << "Error triggering ${cj.code}: ${e.message}"
    }
}
return results.join("\\n")
"""
        result = self.run_groovy(script, commit=False)
        return {
            "success": result.success,
            "output": result.execution_result or result.output,
            "error": result.error,
        }

    def inspect_oauth_client_details(self, client_id: str) -> dict:
        """Inspect OAuthClientDetails for a given clientId."""
        script = f"""
import de.hybris.platform.servicelayer.search.FlexibleSearchQuery
def q = new FlexibleSearchQuery("SELECT {{pk}} FROM {{OAuthClientDetails}} WHERE {{clientId}} = :id")
q.addQueryParameter("id", "{client_id}")
def r = flexibleSearchService.search(q)
if (!r.result) return "NOT FOUND"
def c = r.result[0]
return [
    clientId: c.clientId,
    authorities: c.authorities,
    authorizedGrantTypes: c.authorizedGrantTypes,
    registeredRedirectUri: c.registeredRedirectUri,
    requireProofKey: c.requireProofKey,
    scope: c.scope
].toString()
"""
        result = self.run_groovy(script, commit=False)
        return {
            "success": result.success,
            "data": result.execution_result,
            "error": result.error,
        }
