"""
SAP Commerce release note parser — version-independent.

Input:  .md or .txt file containing SAP release notes (any version)
Output: ParsedRelease with structured steps the agent can execute

The agent is abstract: give it jdk21.9 notes today, jdk21.11 notes tomorrow —
it extracts what changed, what requires action, and what custom code to inspect.
"""
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class FixedIssue:
    """A bug/security fix listed in the release notes."""
    key: str           # e.g. CXEC-59056
    priority: str      # Very High / High / Medium / Low
    component: str     # platform / smartedit / oauth2 / etc.
    summary: str
    action_required: bool = False
    fixed_in_versions: list[str] = field(default_factory=list)


@dataclass
class ActionStep:
    """A step the upgrader must perform explicitly."""
    id: str
    title: str
    description: str
    step_type: str     # code_change / config_change / groovy / impex / manual / verification
    component: str
    action_required: bool = True
    files_hint: list[str] = field(default_factory=list)
    groovy_script: Optional[str] = None
    impex_template: Optional[str] = None
    property_key: Optional[str] = None
    property_value: Optional[str] = None


@dataclass
class SpringBeanChange:
    bean_id: str
    change_type: str   # restructured / removed / renamed / added


@dataclass
class LibraryChange:
    artifact: str
    change_type: str   # added / removed / upgraded
    from_version: Optional[str] = None
    to_version: Optional[str] = None


@dataclass
class ParsedRelease:
    """Everything extracted from a release notes file."""
    source_file: str
    target_version: str
    fixed_issues: list[FixedIssue] = field(default_factory=list)
    action_steps: list[ActionStep] = field(default_factory=list)
    spring_bean_changes: list[SpringBeanChange] = field(default_factory=list)
    library_changes: list[LibraryChange] = field(default_factory=list)
    raw_text: str = ""

    def get_action_required_steps(self) -> list[ActionStep]:
        return [s for s in self.action_steps if s.action_required]

    def get_steps_for_component(self, component: str) -> list[ActionStep]:
        return [s for s in self.action_steps if component.lower() in s.component.lower()]

    def find_issue(self, key: str) -> Optional[FixedIssue]:
        for issue in self.fixed_issues:
            if issue.key.upper() == key.upper():
                return issue
        return None

    def find_version_fixing_error(self, error_text: str) -> Optional[str]:
        """
        Given an error string, look for a fixed issue whose summary matches.
        Returns the earliest version that contains the fix, or None.
        """
        error_lower = error_text.lower()
        for issue in self.fixed_issues:
            keywords = issue.summary.lower().split()
            # At least 3 significant keywords must match
            hits = sum(1 for w in keywords if len(w) > 4 and w in error_lower)
            if hits >= 2:
                if issue.fixed_in_versions:
                    return issue.fixed_in_versions[0]
        return None


# ------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------

class ReleaseNoteParser:
    """
    Parses SAP Commerce release notes into a ParsedRelease.

    Handles both the structured markdown table format and the older
    plain-text multi-column export from SAP Help Portal.
    """

    # Pattern: CXEC-12345 or SAP note number like 3618495
    _BUG_KEY = re.compile(r'\b(CXEC-\d{4,6})\b')
    _SAP_NOTE = re.compile(r'\b(3\d{6})\b')

    # Section headers seen in real SAP release notes
    _SECTION_MARKERS = {
        "fixed_issues": re.compile(
            r'^[-=]+\s*Fixed Issues\s*[-=]*|^---\s*Fixed Issues\s*---|^Fixed Issues\s*$',
            re.IGNORECASE | re.MULTILINE,
        ),
        "action_required": re.compile(
            r'ACTION REQUIRED|action required|Manual Steps|MANUAL STEPS',
            re.IGNORECASE,
        ),
        "spring_changes": re.compile(
            r'Spring Framework Changes|Spring Bean Changes',
            re.IGNORECASE,
        ),
        "library_changes": re.compile(
            r'Library Changes|Third-Party Libraries|Deleted:|New:|Upgraded',
            re.IGNORECASE,
        ),
        "deprecations": re.compile(
            r'Deprecation|deprecated',
            re.IGNORECASE,
        ),
    }

    # Known patterns that require action in custom code
    _ACTION_PATTERNS = [
        # (regex on note text, step_type, component, files_hint, description)
        (
            re.compile(r'HandlerInterceptorAdapter', re.I),
            "code_change", "storefront",
            ["**/interceptors/*.java"],
            "Replace HandlerInterceptorAdapter with HandlerInterceptor (removed in Spring 6)",
        ),
        (
            re.compile(r'javax\.servlet|javax\.annotation|javax\.validation', re.I),
            "code_change", "all",
            ["**/*.java", "**/*.jsp", "**/*.xml"],
            "Migrate javax.* imports to jakarta.* (Jakarta EE 9+)",
        ),
        (
            re.compile(r'jstl.*jar|TagLibraryValidator|jakarta\.servlet\.jsp\.jstl', re.I),
            "code_change", "storefront",
            ["**/WEB-INF/lib/jstl*.jar", "**/WEB-INF/lib/javax.servlet*.jar"],
            "Remove old javax-based JSTL JARs, add jakarta.servlet.jsp.jstl:3.0.1",
        ),
        (
            re.compile(r'OAuthClientVoter|OAuth2Authentication|spring-security-oauth2', re.I),
            "code_change", "webservices",
            ["**/oauth2/*.java", "**/*Oauth*.java", "**/*OAuth*.java"],
            "Replace removed spring-security-oauth2 classes with Spring Security 6 equivalents",
        ),
        (
            re.compile(r'csrf.*bare bean|XorCsrfTokenRequestAttributeHandler', re.I),
            "code_change", "accaddon/commorgaddon",
            ["**/spring/*.xml", "**/web/spring/*.xml"],
            "Remove bare XorCsrfTokenRequestAttributeHandler bean — breaks CSRF in Spring 6",
        ),
        (
            re.compile(r'BeanWrapperImpl.*fluent|fluent setter|spring4shell.*patch', re.I),
            "config_change", "cloud_portal",
            [],
            "Remove Spring4Shell patch Cloud Portal properties — breaks Spring 6 fluent setters",
        ),
        (
            re.compile(r'pbkdf2.*password|pbkdf2PasswordEncoder', re.I),
            "groovy", "hac",
            [],
            "Re-encode employee passwords from pbkdf2 to bcrypt/argon2 before Migrate Data deploy",
        ),
        (
            re.compile(r'OAuthClientDetails.*smartedit|requireProofKey|PKCE', re.I),
            "impex", "smartedit",
            [],
            "Update SmartEdit OAuthClientDetails for PKCE/requireProofKey in HAC",
        ),
        (
            re.compile(r'luceneMatchVersion|solr.*lucene', re.I),
            "config_change", "solr",
            ["**/solrconfig.xml"],
            "Update luceneMatchVersion to match bundled Solr version (e.g. 9.12)",
        ),
        (
            re.compile(r'manifest\.json.*oauth2|\"name\".*oauth2.*authorizationserver', re.I),
            "config_change", "manifest",
            ["core-customize/manifest.json"],
            'Fix manifest.json: replace "name": "oauth2" with "name": "authorizationserver" in webapps',
        ),
        (
            re.compile(r'invalid-session-url|SessionManagementFilter|session.*management', re.I),
            "code_change", "storefront",
            ["**/spring-security-config.xml"],
            "Review invalid-session-url in session-management — Spring Security 6 behaviour differs",
        ),
        (
            re.compile(r'@Controller.*annotation|RequestMappingHandlerMapping.*isHandler', re.I),
            "code_change", "storefront",
            ["**/*Controller*.java", "**/*PageController*.java"],
            "Add @Controller annotation — Spring 6 requires it alongside @RequestMapping",
        ),
        (
            re.compile(r'trailing.?slash|useTrailingSlashMatch|setUseTrailingSlashMatch', re.I),
            "code_change", "storefront",
            ["**/*.jsp", "**/spring-security-config.xml", "**/*Controller*.java"],
            "Fix trailing slash URLs — Spring 6 strict matching (useTrailingSlashMatch=false by default)",
        ),
        (
            re.compile(r'Content-Security-Policy|frame-src|CSP.*smartedit|smartedit.*CSP', re.I),
            "config_change", "storefront",
            ["**/project.properties"],
            "Add smartedit.response.header.Content-Security-Policy to allow storefront domains in frame-src",
        ),
    ]

    def __init__(self, release_notes_path: str):
        self.path = Path(release_notes_path)
        if not self.path.exists():
            raise FileNotFoundError(f"Release notes not found: {self.path}")

    def parse(self) -> ParsedRelease:
        text = self.path.read_text(errors="replace")
        version = self._detect_version(text)

        result = ParsedRelease(
            source_file=str(self.path),
            target_version=version,
            raw_text=text,
        )

        result.fixed_issues = self._parse_fixed_issues(text, version)
        result.action_steps = self._extract_action_steps(text, result.fixed_issues)
        result.spring_bean_changes = self._parse_spring_changes(text)
        result.library_changes = self._parse_library_changes(text)

        logger.info(
            f"Parsed release notes: version={version}, "
            f"fixed_issues={len(result.fixed_issues)}, "
            f"action_steps={len(result.action_steps)}, "
            f"spring_changes={len(result.spring_bean_changes)}, "
            f"library_changes={len(result.library_changes)}"
        )
        return result

    # ------------------------------------------------------------------
    # Version detection
    # ------------------------------------------------------------------

    def _detect_version(self, text: str) -> str:
        patterns = [
            re.compile(r'Update Release\s+(2211-jdk\d+\.\d+)', re.I),
            re.compile(r'(2211-jdk\d+\.\d+)'),
            re.compile(r'commerceSuiteVersion[:\s]+([\w.\-]+)'),
            re.compile(r'platform[:\s]+([\w.\-]+jdk\d+)', re.I),
        ]
        for pat in patterns:
            m = pat.search(text)
            if m:
                return m.group(1).strip()
        return "UNKNOWN"

    # ------------------------------------------------------------------
    # Fixed issues
    # ------------------------------------------------------------------

    def _parse_fixed_issues(self, text: str, version: str) -> list[FixedIssue]:
        issues: list[FixedIssue] = []
        seen_keys: set[str] = set()

        # Strategy 1: markdown pipe table | Priority | Key | Component | Summary |
        table_row = re.compile(
            r'\|\s*(Very High|High|Medium|Low)\s*\|\s*(CXEC-\d+)\s*\|\s*([\w\-]+)\s*\|\s*(.+?)\s*\|',
            re.IGNORECASE,
        )
        for m in table_row.finditer(text):
            key = m.group(2).upper()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            summary = m.group(4).strip()
            issues.append(FixedIssue(
                key=key,
                priority=m.group(1).strip(),
                component=m.group(3).strip(),
                summary=summary,
                action_required=bool(re.search(r'ACTION REQUIRED', summary, re.I)),
                fixed_in_versions=[version],
            ))

        # Strategy 2: multi-line SAP Help Portal export (column values on separate lines)
        # Version\nPriority\nBug fix\nCXEC-XXXXX\nComponent\nSummary
        multiline_block = re.compile(
            r'(2211-jdk[\d.]+)\s*\n\s*(Very High|High|Medium|Low)\s*\n\s*Bug fix\s*\n\s*(CXEC-\d+)\s*\n\s*([\w\-]+)\s*\n\s*(.+?)(?=\n\s*2211-jdk|\Z)',
            re.DOTALL | re.IGNORECASE,
        )
        for m in multiline_block.finditer(text):
            key = m.group(3).upper()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            summary = re.sub(r'\s+', ' ', m.group(5)).strip()
            fix_version = m.group(1).strip()
            issues.append(FixedIssue(
                key=key,
                priority=m.group(2).strip(),
                component=m.group(4).strip(),
                summary=summary,
                fixed_in_versions=[fix_version],
            ))

        return issues

    # ------------------------------------------------------------------
    # Action steps
    # ------------------------------------------------------------------

    def _extract_action_steps(self, text: str, fixed_issues: list[FixedIssue]) -> list[ActionStep]:
        steps: list[ActionStep] = []
        step_counter = [0]

        def next_id(prefix: str) -> str:
            step_counter[0] += 1
            return f"{prefix}-{step_counter[0]:02d}"

        # 1. Pattern-based: scan full text against known action patterns
        for pattern, step_type, component, files_hint, description in self._ACTION_PATTERNS:
            if pattern.search(text):
                steps.append(ActionStep(
                    id=next_id("ACT"),
                    title=description.split(" — ")[0][:80],
                    description=description,
                    step_type=step_type,
                    component=component,
                    action_required=True,
                    files_hint=files_hint,
                ))

        # 2. Explicit ACTION REQUIRED blocks in the release notes
        action_block = re.compile(
            r'ACTION REQUIRED[:\s]*([^\n]{5,200})',
            re.IGNORECASE,
        )
        for m in action_block.finditer(text):
            title = m.group(1).strip()[:80]
            if any(s.title == title for s in steps):
                continue
            steps.append(ActionStep(
                id=next_id("REQ"),
                title=title,
                description=m.group(0).strip(),
                step_type="manual",
                component="general",
                action_required=True,
            ))

        # 3. Property toggles (features in rollout phases)
        prop_pattern = re.compile(
            r'([\w.]+=[^\s\n]+)\s*\n?\s*(.*?(?:toggle|activate|deactivate|enable|disable)[^\n]*)',
            re.IGNORECASE,
        )
        for m in prop_pattern.finditer(text):
            kv = m.group(1).strip()
            if "." not in kv or len(kv) > 120:
                continue
            try:
                key, value = kv.split("=", 1)
            except ValueError:
                continue
            steps.append(ActionStep(
                id=next_id("PROP"),
                title=f"Evaluate property: {key}",
                description=f"{kv} — {m.group(2).strip()[:200]}",
                step_type="config_change",
                component="properties",
                action_required=False,
                property_key=key.strip(),
                property_value=value.strip(),
            ))

        # 4. Deprecation notices (low priority, FYI)
        dep_pattern = re.compile(
            r'^[ \t]*[-•]\s+([\w()#.]+(?:\s*[→→]\s*[\w()#.]+)?[^\n]{0,200})',
            re.MULTILINE,
        )
        in_dep_section = False
        for line in text.splitlines():
            if re.search(r'^Deprecations?:', line, re.I):
                in_dep_section = True
                continue
            if in_dep_section:
                if line.strip() == "" or re.match(r'^[A-Z]', line.strip()):
                    in_dep_section = False
                    continue
                m = dep_pattern.match(line)
                if m:
                    steps.append(ActionStep(
                        id=next_id("DEP"),
                        title=f"Deprecation: {m.group(1)[:80]}",
                        description=m.group(1).strip(),
                        step_type="verification",
                        component="general",
                        action_required=False,
                    ))

        return steps

    # ------------------------------------------------------------------
    # Spring bean changes
    # ------------------------------------------------------------------

    def _parse_spring_changes(self, text: str) -> list[SpringBeanChange]:
        changes = []
        in_section = False
        pattern = re.compile(
            r'(\d+)\.\s+([\w]+)\s*[-—]\s*(restructured|removed|renamed|added)',
            re.IGNORECASE,
        )
        for line in text.splitlines():
            if re.search(r'Spring Framework Changes|Spring Bean Changes', line, re.I):
                in_section = True
                continue
            if in_section:
                if line.strip() == "" and len(changes) > 3:
                    break
                m = pattern.match(line.strip())
                if m:
                    changes.append(SpringBeanChange(
                        bean_id=m.group(2).strip(),
                        change_type=m.group(3).strip().lower(),
                    ))
        return changes

    # ------------------------------------------------------------------
    # Library changes
    # ------------------------------------------------------------------

    def _parse_library_changes(self, text: str) -> list[LibraryChange]:
        changes = []
        in_section = False
        current_type = ""

        for line in text.splitlines():
            stripped = line.strip()
            if re.search(r'^(?:Library Changes|Deleted:|New:|Upgraded)', stripped, re.I):
                in_section = True
                if "Deleted" in stripped:
                    current_type = "removed"
                elif "New" in stripped:
                    current_type = "added"
                elif "Upgraded" in stripped:
                    current_type = "upgraded"
                continue

            if not in_section:
                continue

            if re.match(r'^-{3,}|^={3,}', stripped):
                break

            if re.match(r'^(Deleted|New|Upgraded)[: ]', stripped, re.I):
                if "Deleted" in stripped:
                    current_type = "removed"
                elif "New" in stripped:
                    current_type = "added"
                elif "Upgraded" in stripped:
                    current_type = "upgraded"
                continue

            # "org.springframework:spring-beans  6.2.11 → 6.2.12"
            m = re.match(r'([\w.\-]+:[\w.\-]+)\s+([\d.]+)\s*[→>→]+\s*([\d.]+)', stripped)
            if m:
                changes.append(LibraryChange(
                    artifact=m.group(1),
                    change_type="upgraded",
                    from_version=m.group(2),
                    to_version=m.group(3),
                ))
                continue

            # "Platform: some-artifact version"
            m = re.match(r'(?:Platform|SAP)[:\s]+([\w.\-:]+)\s+([\d.]+)', stripped)
            if m and current_type:
                changes.append(LibraryChange(
                    artifact=m.group(1),
                    change_type=current_type,
                    to_version=m.group(2) if current_type != "removed" else None,
                    from_version=m.group(2) if current_type == "removed" else None,
                ))

        return changes


# ------------------------------------------------------------------
# Quick CLI test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    path = sys.argv[1] if len(sys.argv) > 1 else "knowledge/release-notes.txt"
    parser = ReleaseNoteParser(path)
    release = parser.parse()

    print(f"\nVersion: {release.target_version}")
    print(f"Fixed issues: {len(release.fixed_issues)}")
    print(f"Action steps: {len(release.action_steps)}")
    print(f"  Action-required: {len(release.get_action_required_steps())}")
    print(f"Spring bean changes: {len(release.spring_bean_changes)}")
    print(f"Library changes: {len(release.library_changes)}")

    print("\n--- ACTION-REQUIRED STEPS ---")
    for step in release.get_action_required_steps():
        print(f"  [{step.id}] [{step.step_type}] {step.title}")

    print("\n--- FIXED ISSUES (first 10) ---")
    for issue in release.fixed_issues[:10]:
        print(f"  {issue.key} [{issue.priority}] {issue.component}: {issue.summary[:80]}")
