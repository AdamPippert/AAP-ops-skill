# Contract Schema & Merge Semantics

This is the data model for AAP-ops contracts. Every contract — in `rules.toml` or
added via `extension.toml` — conforms to it. The linter (`scripts/lint.py`) depends
on these field names exactly.

## Top-level keys

```toml
schema_version = 1            # integer; bump only on breaking shape changes
inherits = "rules.toml"       # extension files only; relative to this file

[meta]
name = "aap-ops"              # identifier
version = "1.0.0"             # semver of this rule set
owner = "platform-team"       # accountable team (audit)

[config]                      # values interpolated into checks as ${config.<key>}
controller_api = "/api/controller/v2"
gateway_api    = "/api/gateway/v1"
ee_schema_version = 3
ansible_lint_profile = "production"
max_page_size = 200
approved_registries = ["registry.redhat.io", "registry.access.redhat.com"]
```

## A contract

```toml
[[contract]]
id        = "AAP-API-004"          # REQUIRED. Stable primary key. Namespaced. Never reused.
domain    = "api"                  # REQUIRED. api | ee | governance
title     = "TLS verification stays on"   # REQUIRED. Short human label.
severity  = "error"                # REQUIRED. error | warn | info
statement = "All AAP API calls MUST verify TLS; never disable certificate validation."
rationale = "Disabling validation turns an authenticated control-plane call into an MITM target; one job run can leak a controller token."
applies_to = ["playbook", "script"]   # which artifact kinds this governs (free tags)
refs = ["https://docs.redhat.com/.../controller-api-auth-methods"]

  [contract.check]
  type   = "regex_absent"          # see check types table below
  target = "**/*.{yml,yaml,sh,py}" # glob, relative to --target root
  pattern = "(?i)(validate_certs:\\s*false|verify\\s*=\\s*False|curl[^\\n]*\\s-k\\b|--insecure)"
  message = "Found TLS verification disabled."

  [contract.fix]
  text = "Set validate_certs: true (module) / verify=True (requests). Trust the CA via the EE or system trust store instead of skipping verification."
```

### Field reference

| Field         | Required | Notes                                                              |
|---------------|----------|--------------------------------------------------------------------|
| `id`          | yes      | Unique across base + extension. Format `NAMESPACE-DOMAIN-NNN`.     |
| `domain`      | yes      | `api`, `ee`, or `governance`.                                       |
| `title`       | yes      | One line.                                                           |
| `severity`    | yes      | `error` fails the build; `warn`/`info` are reported only.           |
| `statement`   | yes      | Imperative normative text. This is the prompt the model follows.    |
| `rationale`   | yes      | The "why". Keep it persuasive, not decorative.                      |
| `applies_to`  | no       | Tags describing the artifact kind. Default: all.                    |
| `refs`        | no       | URLs to AAP docs or internal policy.                                |
| `check`       | yes      | Typed check table (below).                                          |
| `fix`         | yes      | `text` remediation. May add `command` to auto-apply.               |

## `check` by type

All checks share `type`, optional `target` (glob), and optional `message`.

```toml
# Fail if pattern is found
[contract.check]
type = "regex_absent"
target = "**/*.yml"
pattern = "..."

# Fail if pattern is NOT found
[contract.check]
type = "regex_present"
target = "config/*.yml"
pattern = "..."

# Fail if no file matches the glob
[contract.check]
type = "file_present"
target = "{CODEOWNERS,.github/CODEOWNERS,docs/CODEOWNERS}"

# Load YAML and assert on a dotted/indexed path
[contract.check]
type = "yaml_key"
target = "**/execution-environment.yml"
path = "version"          # supports a.b.c and a.b[0]
op = "=="                 # == != >= <= > < in contains exists
value = 3

# Every entry in a requirements file must be version-pinned
[contract.check]
type = "requirements_pinned"
target = "**/requirements.{yml,txt}"
# galaxy (requirements.yml: collections[].version) and pip (==, ~=, >=) both handled

# Run a command at the target root; pass on exit 0
[contract.check]
type = "command"
cmd = "ansible-lint --profile ${config.ansible_lint_profile} -q"
# ${config.*} is interpolated before execution

# Live API assertion (only with --live and env creds)
[contract.check]
type = "http"
endpoint = "${config.controller_api}/ping/"
op = "json_path_eq"
path = "ha"               # asserts response JSON ha == value (example)
value = true

# Human attestation — no automation possible/appropriate
[contract.check]
type = "manual"
prompt = "Confirm automation service accounts are scoped to a single org/team with no global System Administrator role."
```

## Merge semantics (how extension deltas apply)

`scripts/lint.py` builds the effective rule set in this order:

1. **Load base** `rules.toml` → list of contracts keyed by `id`, plus `[config]`.
2. **Merge `[config]`** from the extension over base (shallow key overwrite).
3. **Apply `[[override]]`** — for each, deep-merge its fields onto the base contract
   with the same `id`. Only listed fields change; everything else is inherited. An
   override targeting an unknown id is a hard error (catches typos / drift).
4. **Apply `[[disable]]`** — remove the contract with that `id` from execution but
   record it in the report as `DISABLED — <reason>`. A `reason` is mandatory; a
   disable without one is a hard error.
5. **Append `[[contract]]`** — new contracts from the extension. Their ids must not
   collide with base ids (use your own namespace).

The result is a single, flat, deterministic contract set. Same inputs → same report,
which is what makes CI gating trustworthy.

### Extension delta shapes

```toml
inherits = "rules.toml"

[config]
ansible_lint_profile = "production"   # overrides base config value

[[override]]
id = "AAP-EE-002"
severity = "error"                    # was "warn" in base

[[disable]]
id = "AAP-GOV-006"
reason = "Audit log shipping owned by central SIEM team; out of scope for this repo."

[[contract]]
id = "ACME-API-100"                   # org namespace, new rule
domain = "api"
title = "Internal CA only"
severity = "error"
statement = "API clients MUST pin the ACME internal CA bundle."
rationale = "Public CAs are not trusted for internal control-plane traffic."
  [contract.check]
  type = "regex_present"
  target = "**/*.py"
  pattern = "ACME_CA_BUNDLE"
  [contract.fix]
  text = "Pass verify=os.environ['ACME_CA_BUNDLE'] to every requests call."
```
