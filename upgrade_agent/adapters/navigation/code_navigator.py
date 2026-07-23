"""
CodeNavigator — maps error/bug type to custom extension files.

Two responsibilities:
  1. Given an error, return the likely custom code files to inspect/fix.
  2. Given an error, cross-reference release notes to see if SAP already
     fixed it in a later version (so you can upgrade instead of patching).

Navigation map uses generic SAP Commerce upgrade patterns. Replace placeholder extension names with your own project paths.
"""
import re
import glob
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from upgrade_agent.adapters.parser.release_note_parser import ParsedRelease

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class NavigationHit:
    error_category: str
    description: str
    files: list[str]
    fix_hint: str
    priority: str = "medium"   # high / medium / low


# ------------------------------------------------------------------
# Error → custom code mapping
# ------------------------------------------------------------------

# Each entry: (regex pattern, category label, glob patterns relative to custom_code_root, hint, priority)
_NAVIGATION_MAP = [
    (
        re.compile(r'HandlerInterceptorAdapter', re.I),
        "spring_interceptor",
        ["**/interceptors/*.java", "**/interceptors/**/*.java"],
        "Change 'extends HandlerInterceptorAdapter' to 'implements HandlerInterceptor' (Spring 6 removed the adapter class)",
        "high",
    ),
    (
        re.compile(r'BeanCreationException.*fluent setter|BeanWrapperImpl.*fluent|spring4shell.*patch', re.I),
        "spring4shell_fluent_setter",
        [],
        "Remove Spring4Shell patch properties from Cloud Portal (ccv2.file.override.spring4shell-patch.*). Cannot be fixed in code.",
        "high",
    ),
    (
        re.compile(r'BeanCreationException(?!.*fluent)', re.I),
        "bean_creation_generic",
        ["**/resources/**/*.xml", "**/web/webroot/WEB-INF/**/*.xml"],
        "Inspect the Spring XML file that wires the failing bean. Common cause: missing bean definition or incompatible Spring 6 change.",
        "high",
    ),
    (
        re.compile(r'javax\.servlet\.jsp\.tagext\.TagLibraryValidator|jstl.*ClassNotFoundException', re.I),
        "jstl_taglib_validator",
        ["**/WEB-INF/lib/jstl*.jar", "**/WEB-INF/lib/javax.servlet*.jar"],
        "Remove old javax-based JSTL JARs (jstl-1.2.jar, jstl-impl-1.2.jar, javax.servlet.jsp.jstl-1.2.jar). They contain TLD references broken on Tomcat 10.1/Jakarta EE 9.",
        "high",
    ),
    (
        re.compile(r'javax\.servlet|javax\.annotation|javax\.validation', re.I),
        "jakarta_migration",
        ["**/*.java", "**/*.jsp", "**/*.xml", "**/*.groovy"],
        "Migrate javax.* imports to jakarta.* — use grep to find remaining javax references.",
        "high",
    ),
    (
        re.compile(r'OAuthClientVoter|OAuth2Authentication.*cast|spring-security-oauth2', re.I),
        "oauth2_voter",
        [
            "**/oauth2/**/*.java",
            "**/security/**/*.java",
            "custom-webservices/**/*.java",
            "custom-webservices/**/spring/*.xml",
        ],
        "Replace removed OAuth2Authentication / spring-security-oauth2 classes with Spring Security 6 equivalents (JwtAuthenticationToken or BearerTokenAuthentication).",
        "high",
    ),
    (
        re.compile(r'Oauth2AccessTokenConverter|AccessTokenConverter', re.I),
        "oauth2_token_converter",
        ["**/oauth2/**/*.java", "custom-webservices/**/*.java"],
        "OAuthAccessTokenConverter was removed with spring-security-oauth2. Check if the bean is still wired — may be safely deletable if unused.",
        "medium",
    ),
    (
        re.compile(r'CustomOAuth2BearerInterceptor|ClientHttpRequestInterceptor.*OAuth|OAuth2RestTemplate', re.I),
        "oauth2_bearer_interceptor",
        [
            "custom-integrations/src/**/*.java",
            "custom-integrations/resources/**/*.xml",
        ],
        "Review the custom OAuth2 bearer interceptor and token caching logic.",
        "medium",
    ),
    (
        re.compile(r'XorCsrfTokenRequestAttributeHandler.*bare bean|CsrfTokenRequestAttributeHandler.*Spring XML', re.I),
        "csrf_bare_bean",
        [
            "custom-accaddon/resources/custom-accaddon/web/spring/*.xml",
            "custom-commorgaddon/resources/custom-commorgaddon/web/spring/*.xml",
        ],
        "Remove bare XorCsrfTokenRequestAttributeHandler bean from accaddon + commorgaddon Spring XML. Spring Security 6 registers it automatically — a bare bean override breaks it.",
        "high",
    ),
    (
        re.compile(r'invalid-session-url|SessionManagementFilter.*logout|session.*management.*invalid', re.I),
        "session_management",
        ["**/spring-security-config.xml"],
        "Remove 'invalid-session-url=/login' from <security:session-management>. Tomcat 10 no longer auto-clears JSESSIONID, causing logout flash messages to be swallowed.",
        "high",
    ),
    (
        re.compile(r'@Controller.*missing|RequestMappingHandlerMapping.*isHandler|404.*register|register.*404', re.I),
        "missing_controller_annotation",
        ["**/*Controller*.java", "**/*PageController*.java", "**/web/spring/*.xml"],
        "Add @Controller annotation to controller classes defined via Spring XML bean override. Spring 6 RequestMappingHandlerMapping.isHandler() requires @Controller — @RequestMapping alone is no longer sufficient.",
        "high",
    ),
    (
        re.compile(r'trailing.?slash|useTrailingSlashMatch|301.*trailing|trailing.*301', re.I),
        "trailing_slash",
        [
            "**/*.jsp",
            "**/spring-security-config.xml",
            "custom-storefront/web/src/**/*Controller*.java",
            "custom-storefront/web/webroot/WEB-INF/**/*.xml",
        ],
        "Spring 6: useTrailingSlashMatch=false by default. Fix JSP spring:url values to remove trailing slash. Fix intercept-url patterns in spring-security-config.xml. Fix controller RequestMapping paths.",
        "medium",
    ),
    (
        re.compile(r'Content-Security-Policy.*frame-src|CSP.*smartedit|frame-src.*domain', re.I),
        "csp_frame_src",
        ["custom-storefront/project.properties", "**/project.properties"],
        "Add 'smartedit.response.header.Content-Security-Policy' to project.properties listing all storefront domains in frame-src. SAP docs don't mention custom domains.",
        "medium",
    ),
    (
        re.compile(r'pbkdf2.*password|pbkdf2PasswordEncoder.*not found|DisableLoginForImportedUserInterceptor.*pbkdf2', re.I),
        "pbkdf2_passwords",
        [],
        "Re-encode employee passwords from pbkdf2 → bcrypt/argon2 on jdk17 BEFORE Migrate Data deploy. Run the re-encoding Groovy script in HAC on the current jdk17 environment.",
        "high",
    ),
    (
        re.compile(r'SAPOAuth2Authorization.*invalid|type code.*SAPOAuth2|SAPOAuth2.*not found', re.I),
        "sap_oauth2_type_missing",
        [],
        "Run HAC System Update (Update Running System). The authorizationserver extension's essential data registers SAPOAuth2Authorization in the type system.",
        "high",
    ),
    (
        re.compile(r'manifest.*oauth2.*authorizationserver|oauth2.*contextPath.*authorizationserver', re.I),
        "manifest_oauth2_webapp",
        ["core-customize/manifest.json"],
        'Fix manifest.json: replace {"name": "oauth2", "contextPath": "/authorizationserver"} with {"name": "authorizationserver", "contextPath": "/authorizationserver"} in accstorefront + api aspects.',
        "high",
    ),
    (
        re.compile(r'luceneMatchVersion|solrconfig.*lucene|lucene.*version.*mismatch', re.I),
        "solr_lucene_version",
        ["**/solrconfig.xml", "**/solr/**/*.xml"],
        "Update luceneMatchVersion in solrconfig.xml to match the Solr version bundled with the target platform (e.g. 9.12 for jdk21.9).",
        "low",
    ),
    (
        re.compile(r'GUIDCookieStrategy|StoredHttpSession.*guid|GUID.*overwrite', re.I),
        "guid_cookie_strategy",
        [
            "custom-storefront/web/src/**/*GUIDCookieStrategy*.java",
            "custom-storefront/web/src/**/*Filter*.java",
        ],
        "Guard GUID overwrite in DefaultGUIDCookieStrategy — CCV2 StoredHttpSession + Spring Security 6 double-call causes session loss.",
        "high",
    ),
    (
        re.compile(r'validationModelPatch|MethodInvokingBean.*setExposedContext|hacHealthCheckFacade.*fluent', re.I),
        "method_invoking_bean_workaround",
        ["custom-overrides/web/webroot/WEB-INF/**/*.xml"],
        "MethodInvokingBean overrides for validationModelPatch / hacHealthCheckFacade / hacJmxFacade must be present. Spring 6.2.x cloud artifact still has unpatched spring-beans.",
        "high",
    ),
    (
        re.compile(r'LegacyOauthClientsMigrator|No attribute.*public.*OAuthClientDetails', re.I),
        "legacy_oauth_migrator",
        [],
        "Use 'No migration required' deploy mode instead of 'Migrate Data' — LegacyOauthClientsMigrator crashes on jdk21 because OAuthClientDetails.public attribute was added.",
        "high",
    ),
    (
        re.compile(r'generateUnitPaths|OrgUnitAfterInitializationEndEventListener', re.I),
        "org_unit_rebuild",
        [],
        "Not an error. B2B org unit hierarchy rebuild after System Update. Normal for large B2B orgs. Can take up to 2h on large databases.",
        "low",
    ),
    (
        re.compile(r'hacSqlServerErrorObjectMapper|HAC.*web.*context.*BeanCreation', re.I),
        "hac_sql_server_mapper",
        [],
        "Caused by Spring4Shell patch properties in Cloud Portal. Removing the ccv2.file.override.spring4shell-patch.* keys from Cloud Portal resolves this without any code change.",
        "high",
    ),
]


# ------------------------------------------------------------------
# Code Navigator
# ------------------------------------------------------------------

class CodeNavigator:
    """
    Maps an error string to the custom extension files most likely to need inspection.

    Also cross-references release notes to detect if SAP has already fixed
    the root cause in a later version.
    """

    def __init__(self, custom_code_root: str):
        self.custom_code_root = Path(custom_code_root) if custom_code_root else None

    def find_files_for_error(self, error_text: str) -> list[NavigationHit]:
        """
        Return NavigationHit objects for all patterns that match error_text.

        Each hit contains the error category, fix hint, and resolved file paths
        (expanded globs relative to custom_code_root).
        """
        hits: list[NavigationHit] = []
        already_matched: set[str] = set()

        for pattern, category, file_globs, hint, priority in _NAVIGATION_MAP:
            if not pattern.search(error_text):
                continue
            if category in already_matched:
                continue
            already_matched.add(category)

            files = self._resolve_globs(file_globs)
            hits.append(NavigationHit(
                error_category=category,
                description=hint,
                files=files,
                fix_hint=hint,
                priority=priority,
            ))

        if not hits:
            logger.debug(f"No navigation match for error: {error_text[:100]}")

        return sorted(hits, key=lambda h: {"high": 0, "medium": 1, "low": 2}.get(h.priority, 3))

    def _resolve_globs(self, patterns: list[str]) -> list[str]:
        """Expand glob patterns relative to custom_code_root."""
        if not self.custom_code_root or not self.custom_code_root.exists():
            return patterns  # return the pattern strings as-is (informational)

        resolved: list[str] = []
        for pattern in patterns:
            full = str(self.custom_code_root / pattern)
            matches = glob.glob(full, recursive=True)
            if matches:
                resolved.extend(sorted(matches)[:10])  # max 10 per pattern
            else:
                resolved.append(f"[no match] {pattern}")

        return resolved

    def find_version_that_fixes(
        self,
        error_text: str,
        release: "ParsedRelease",
    ) -> Optional[str]:
        """
        Cross-check error against parsed release notes.

        If the error text matches the summary of a known fixed issue,
        return the version that introduced the fix.

        Example: 'validationModelPatch BeanWrapper fluent setter' matches
        CXEC-59056 "Validation framework failure on system update and startup"
        → returns '2211-jdk21.9'
        """
        if release is None:
            return None

        version = release.find_version_fixing_error(error_text)
        if version:
            logger.info(f"Error may be fixed in {version} (matched release notes)")
        return version

    def suggest_for_step(self, step_type: str, component: str) -> list[str]:
        """
        Suggest file globs for a planned step (before an error occurs).

        Used by the pipeline to tell the agent where to look BEFORE executing a step.
        """
        suggestions: dict[str, list[str]] = {
            "spring_interceptor": ["**/interceptors/*.java"],
            "csrf": [
                "custom-accaddon/resources/custom-accaddon/web/spring/*.xml",
                "custom-commorgaddon/resources/custom-commorgaddon/web/spring/*.xml",
            ],
            "oauth2": [
                "**/oauth2/**/*.java",
                "custom-webservices/**/*.java",
                "custom-integrations/src/**/*.java",
            ],
            "jakarta": ["**/*.java", "**/*.jsp", "**/*.xml"],
            "manifest": ["core-customize/manifest.json"],
            "security_config": ["**/spring-security-config.xml"],
            "trailing_slash": ["**/*.jsp", "**/spring-security-config.xml"],
            "storefront": ["custom-storefront/web/src/**/*.java", "custom-storefront/**/*.jsp"],
            "properties": ["**/project.properties", "custom-storefront/project.properties"],
        }

        key = f"{step_type}_{component}".lower().replace("-", "_")
        if key in suggestions:
            return self._resolve_globs(suggestions[key])

        # Fuzzy match on component name
        for k, v in suggestions.items():
            if k in component.lower() or component.lower() in k:
                return self._resolve_globs(v)

        return []
