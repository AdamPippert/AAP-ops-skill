# AAP Ops Skill — Overview for Platform Owners

## What this is

A Claude Code **skill** that turns your AAP operating standards into machine-readable
**contracts** that govern how an AI assistant (and your engineers) work against
Red Hat Ansible Automation Platform. It covers three domains:

- **API ops** — driving automation controller / platform gateway over REST
- **EE / build** — Execution Environments via `ansible-builder` (v3 definitions)
- **Governance** — secrets handling, RBAC scoping, content signing, audit controls

## The core architectural idea

> **A rule is a contract. The contract file is simultaneously the prompt and the linter.**

Most teams write AI guidance in one place (a prompt or wiki page) and enforcement in
another (CI scripts, lint configs). The two drift apart, and nobody can prove the AI
actually followed the guidance.

This skill collapses them into one file: `contracts/rules.toml`.

- **Claude reads it as instructions.** Each contract's `statement` is an imperative
  rule the model follows while writing playbooks, EE definitions, or API clients.
- **The linter executes it as checks.** `scripts/lint.py` parses the *same file* and
  runs each contract's `check` (regex scan, YAML assertion, pin verification, shell
  command, or live API probe) against the working tree, producing pass/fail evidence.

One source of truth, two consumers. The model can't quietly ignore a rule, because
the same rule re-materializes as a failing check — and a reviewer can trace any
finding back to a stable contract ID (e.g. `AAP-API-004`) and its written rationale.

## Anatomy of a contract

Every rule has a uniform shape, which makes the file scannable, diffable in review,
and mechanically enforceable:

| Field | Purpose |
|---|---|
| `id` | Stable, never-reused key (`AAP-EE-002`) — the audit trail |
| `severity` | `error` (fails the build), `warn`, or `info` |
| `statement` | The normative rule — this is the prompt text |
| `rationale` | The *why*, so engineers and auditors can reason about it |
| `check` | A typed, runnable verification |
| `fix` | The remediation to apply when it fails |

Example contracts shipped in the base set: no hardcoded secrets in API calls, TLS
verification always on, EE base images pinned (never `:latest`) and pulled only from
approved registries, all Galaxy/Python dependencies version-pinned, CODEOWNERS
review gating, least-privilege service accounts, audit logs shipped to a SIEM.

## Honest handling of human-judgment controls

Not everything is automatable. Controls like "service accounts hold no global
System Administrator role" use `check.type = "manual"`: the linter reports them as
`MANUAL` attestations requiring sign-off rather than faking a green checkmark.
Governance gaps stay visible instead of silently passing.

## Extension model — adapt without forking

Org- or repo-specific needs go in `contracts/extension.toml`, which holds **deltas
only** against the base rules:

- `[[override]]` by ID — e.g. promote unpinned Python deps from `warn` to `error`
- `[[disable]]` by ID — **requires a written reason**, which is recorded in the
  audit report (e.g. "audit forwarding is verified centrally per runbook RB-114")
- `[[contract]]` — add new rules under your own namespace (`ACME-API-100`)
- `[config]` overrides — e.g. swap in your internal container registry

The base contract set is never edited per-repo, so every team inherits upstream
improvements while keeping their local policy explicit and reviewable.

## How it fits a Claude Code + AAP workflow

1. An engineer asks Claude Code to do AAP work (build an EE, write a playbook that
   manages job templates, script against the gateway API).
2. The skill auto-loads; Claude reads the applicable contracts *before* writing code
   and works to spec — secrets from vault, pinned images, collection modules instead
   of raw `uri` calls, AAP 2.5 versioned API paths.
3. Claude runs the linter to **prove** compliance:
   ```bash
   python scripts/lint.py --rules contracts/rules.toml \
       --extension contracts/extension.toml --target /path/to/repo
   ```
   The same command drops into CI (`--format json`, `--severity error`).
4. Work is summarized **by contract ID** — "passes AAP-EE-001..007, AAP-GOV-004
   attested by X" — giving you a durable, comparable audit trail across runs,
   repos, and time.

## What a platform owner gets out of this

- **Consistency** — AI-assisted and human work converge on one written standard
  instead of tribal knowledge.
- **Auditability** — every requirement has a stable ID, a rationale, and machine
  evidence of pass/fail; disabled rules carry recorded justifications.
- **Safe AI adoption** — Claude Code operates against AAP inside guardrails you
  author and can verify, rather than on the model's general judgment alone.
- **Low maintenance** — policy changes are TOML edits, reviewed like code; version
  assumptions (AAP 2.4 vs 2.5 API roots) live in `[config]`, not scattered through
  rules.

## Repository layout

```
SKILL.md                      operating manual — how to read and apply contracts
contracts/rules.toml          base contracts (single source of truth)
contracts/extension.toml      worked example of an org overlay (deltas only)
scripts/lint.py               the enforcement engine (stdlib Python, ~650 lines)
references/                   domain depth: API ops, EE builds, governance, schema
```
