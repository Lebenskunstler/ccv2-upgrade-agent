# SAP Commerce Upgrade Agent Template

A release-note-driven, self-healing pipeline that automates SAP Commerce upgrade work. It runs a 3-gate loop (build → server up → system update), classifies failures against a known-error map, applies fixes automatically, and escalates only when human judgment is required.

This repository is a template. Adapt all local paths, environment values, and config entries to your own project before using it.

---

## How it works

```
SAP release notes (.txt)
        │
        ▼
 ┌─────────────────────────────────┐
 │  AbstractUpgradePipeline        │
 │                                 │
 │  ── Parse release notes ────►  Action steps (manual tasks flagged)
 │                                 │
 │  For each iteration:            │
 │    Gate 1 — Build               │  ant clean all
 │      ├─ PASS ──────────────────►│
 │      └─ FAIL ──► Healer ───────►│  apply fix → retry (up to 3×)
 │                                 │
 │    Gate 2 — Server up           │  hybrisserver.sh start + HAC login
 │      ├─ PASS ──────────────────►│
 │      └─ FAIL ──► Healer ───────►│
 │                                 │
 │    Gate 3 — System Update       │  POST /platform/init/execute
 │      ├─ PASS ──► ✅ DONE        │  poll until /platform/update → 200
 │      └─ FAIL ──► Healer ───────►│
 └─────────────────────────────────┘
```

**Gate logic:**
- Gate 1 result is cached across iterations — no rebuild unless a fix changed source code
- Server is auto-started before Gate 2 if not running
- Server is stopped before any Gate 1 re-run (releases HSQLDB lock)
- System Update is detected complete by probing `GET /platform/update` with `allow_redirects=False`: `200` = done; `302 → /platform/init` = still running; `302 → /login` = session expired after completion (re-auth + recheck)

---

## Quick start

For a shorter internal guide, see [HOW-TO-USE.md](HOW-TO-USE.md).

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Requires Python 3.10+.

The agent works without an LLM API key. If `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is not set, it falls back to the built-in regex healing map and still runs the upgrade flow.

Treat config files as sensitive operational input: use them only with the right permissions, review every value before you run the agent, and prefer context engineering over hardcoding project-specific code.

### 2. Set up your environment file

```bash
cp .env.example .env.demo
# Edit .env.demo — set DEMO_ADMIN_PASSWORD and optionally ANTHROPIC_API_KEY
```

### 3. Create a config YAML

Use or copy one of the existing configs in `config/`:

```yaml
# config/demo-tobe.yaml
environment: demo-tobe
platform_version: 2211-jdk21.7
deploy_mode: local_ant_build

local_server:
  hybris_dir: /path/to/your/hybris   # replace with your local path
  hac_url: https://localhost:9012

hac:
  base_url: https://localhost:9012
  username: admin
  password_env: DEMO_ADMIN_PASSWORD   # name of the env var

thresholds:
  build_timeout_min: 60
  server_startup_timeout_min: 45
  system_update_timeout_min: 60

verify_ssl: false
```

### 4. Run the pipeline

```bash
export DEMO_ADMIN_PASSWORD=YourPassword
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"

python main.py \
  --env demo-tobe \
  --env-file .env.demo \
  --release-notes /path/to/sap-2211-jdk21-release-notes.txt \
  --upgrade-log /tmp/upgrade-LOG.md \
  --max-iterations 3
```

**Skip rebuild if already built:**
```bash
python main.py --env demo-tobe --env-file .env.demo \
  --release-notes ... --upgrade-log ... --skip-build
```

**Gates-only mode (no release notes needed):**
```bash
python main.py --env demo-tobe --env-file .env.demo --gates-only
```

---

## Demo setup

Use the local setup only as a reference. Replace every filesystem path and environment value with your own.

```bash
# 1. Extract the source and target platform ZIPs
unzip hybris-commerce-suite-<source>.zip -d /workspace/demo/source/
unzip hybris-commerce-suite-<target>.zip -d /workspace/demo/target/

# 2. Set up your source environment
cd /workspace/demo/source
./installer/install.sh -r cx setup -A initAdminPassword=Admin1234
cd hybris/bin/platform && . ./setantenv.sh
ant clean all
ant initialize   # creates the source database

# 3. Copy the database state to your target environment
cp -r /workspace/demo/source/hybris/data/hsqldb /workspace/demo/target/hybris/data/

# 4. Set up your target environment structure
cd /workspace/demo/target
./installer/install.sh -r cx setup -A initAdminPassword=Admin1234

# 5. Override ports to avoid conflicts when needed
echo "tomcat.http.port=9011" >> /workspace/demo/target/hybris/config/local.properties
echo "tomcat.ssl.port=9012" >> /workspace/demo/target/hybris/config/local.properties
echo "tomcat.ajp.port=8019" >> /workspace/demo/target/hybris/config/local.properties
echo "os.rmiregistry.port=2199" >> /workspace/demo/target/hybris/config/local.properties

# 6. Run the upgrade agent with your own notes and log path
python main.py --env demo-tobe --env-file .env.demo \
  --release-notes /path/to/release-notes.txt \
  --upgrade-log /path/to/upgrade-LOG.md --max-iterations 3
```

---

## Config reference

| Key | Description |
|-----|-------------|
| `environment` | Name used in log output |
| `platform_version` | SAP Commerce version string (informational) |
| `deploy_mode` | `local_ant_build` (local) or `ccv2` (cloud) |
| `local_server.hybris_dir` | Absolute path to the `hybris/` directory |
| `local_server.hac_url` | HAC base URL (no trailing slash) |
| `hac.base_url` | Same as `hac_url` (used by the HAC client) |
| `hac.username` | HAC admin username (usually `admin`) |
| `hac.password_env` | Name of the env var holding the admin password |
| `thresholds.build_timeout_min` | ant clean all timeout (default 60) |
| `thresholds.server_startup_timeout_min` | Server startup wait (default 30) |
| `thresholds.system_update_timeout_min` | System Update poll timeout (default 120) |
| `verify_ssl` | Set `false` for self-signed certs (local) |
| `known_active_catalogs` | List of `catalogId/version` to verify product counts after System Update |
| `log.dir` | Path to hybris log directory (used for BeanCreationException check) |

---

## Key files

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point — parses args, builds all clients, runs pipeline |
| `pipeline.py` | `AbstractUpgradePipeline` — the 3-gate iteration loop |
| `gate_checker.py` | Gate 1/2/3 check logic |
| `hac_client.py` | HAC HTTP client (login, Groovy, System Update trigger + poll) |
| `local_server.py` | ant build, hybrisserver.sh start/stop, health probes |
| `healer.py` | Error classifier + fix executor |
| `healing_map.yaml` | Known error → fix rules |
| `release_note_parser.py` | Parses SAP release notes into action steps |
| `log_writer.py` | Structured upgrade-LOG.md writer |
| `config/*.yaml` | Per-environment configuration |
| `.env.example` | Template for environment variables |

---

## How to contribute

1. Fork or clone the repository.
2. Create a branch for your change.
3. Keep extension names, local paths, and project-specific values out of the shared files — use `config/*.yaml` and `.env` files for environment-specific overrides.
4. If you add a new healing rule, add it to `healing_map.yaml` with a clear `id`, `pattern`, and `fix` block.
5. If you add a new config key, document it in the `Config reference` table above.
6. Open a merge request with a one-line summary of what changed and why.

Context engineering first: the template should remain generic. Project-specific adaptations belong in the user's own fork or config files, not in the shared codebase.

---

## Known limitations

- **Groovy console** (`/console/groovy/index`) returns 404 in some cx demo setups. The `SAPOAuth2Authorization` type count check is non-blocking — Gate 3 passes with a warning if Groovy is unavailable.
- **ANTHROPIC_API_KEY** is optional. Without it, Claude-based error suggestions are skipped; the `healing_map.yaml` regex classifier still handles known errors.
- **System Update response timing**: the server returns the 200 response at the END of the System Update (holds the HTTP connection). With a 120s client timeout, the POST may appear to time out while the update is still running — this is handled correctly (treated as "started", then polled).
- **CCv2 mode** (`deploy_mode: ccv2`) requires additional SAP Cloud Portal credentials — see `ccv2_client.py` and `config/d2.yaml` for reference.
