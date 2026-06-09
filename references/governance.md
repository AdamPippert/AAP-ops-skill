# Domain Reference: AAP Governance & Compliance

Depth behind the `domain = "governance"` contracts. These translate platform policy
into auditable controls. Some are automatable; the rest are honest `manual`
attestations rather than fake green checkmarks.

## The principle

Automation is privileged code with production blast radius. Governance contracts
make three questions answerable from the repo and the controller: *who can change
it*, *where do its secrets come from*, and *how little can it do if compromised*.

## Review gating — `AAP-GOV-001`

A `CODEOWNERS` file plus branch protection means no AAP change merges unreviewed.
Map the automation paths to the accountable team:

```
# .github/CODEOWNERS
/playbooks/    @org/platform-team
/execution-environments/  @org/platform-team
*.controller.yml          @org/platform-team
```

## Secrets hygiene — `AAP-GOV-002` (and `AAP-API-002`)

`AAP-API-002` guards API call sites; `AAP-GOV-002` sweeps the whole repo —
`group_vars`, `host_vars`, `.env`, CI configs, committed private keys. Everything is
vaulted or externally injected. If a secret is found: move it to ansible-vault or an
AAP credential, **purge it from git history**, and **rotate it** — exposure in
history is permanent until rotation.

## Survey field types — `AAP-GOV-003`

A survey question that collects a secret must use the `password` type. A secret
captured as `text` lands in job arguments and the database unencrypted, leaking on
every launch:

```yaml
- question_name: "Target API token"
  variable: api_token
  type: password        # not "text"
  required: true
```

## Least privilege — `AAP-GOV-004` (manual)

Every automation/service account is scoped to one organization or team and holds no
global **System Administrator** role. A pipeline credential with platform-wide admin
turns any compromised job into total control-plane compromise. The attestation
records account names and their roles so an auditor can verify scope.

## Content signing — `AAP-GOV-005` (manual; some orgs upgrade to error)

Projects and collections are verified: signed collections from automation hub and/or
GPG signature verification on project sync. Unverified content lets a compromised
mirror or git remote inject code into privileged runs. The example extension shows
an org upgrading this to `error`.

## Audit logging — `AAP-GOV-006` (manual)

The controller's activity stream is forwarded to a tamper-resistant external store
(SIEM). Logs that live only on the platform can be erased by whoever compromises it.
The example extension `disable`s this with a reason — a legitimate pattern when the
control is owned and proven centrally rather than per-repo.

## Ownership & traceability — `AAP-GOV-007`

An `OWNERS` / ownership file names the accountable team and the change-control
reference. When an audit asks "who approved this and why," the answer lives in the
repo, not in someone's memory.

## How attestations show up

`manual` contracts report as `MANUAL` with the attestation prompt. They never auto-
pass. In a gated pipeline, record each attestation (who, when, evidence link) and,
if you want the build to block until they are resolved, run the linter with
`--strict-manual` so unresolved manual controls count against the exit code.
