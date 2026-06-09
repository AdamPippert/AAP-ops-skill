---
name: aap-ops
description: >-
  Operating manual for working with Red Hat Ansible Automation Platform (AAP) the
  contract-governed way. Use this skill WHENEVER the task touches AAP platform
  operations via its REST API (job templates, inventories, projects, credentials,
  RBAC on automation controller / platform gateway), building or pinning Execution
  Environments (ansible-builder, execution-environment.yml, requirements files), or
  AAP governance and compliance (secrets handling, content signing, least-privilege
  RBAC, change control, audit). Trigger even when the user does not say "contract"
  or "lint" — any AAP API call, EE build, or AAP policy question should load this
  skill so the work is checked against the rules in contracts/rules.toml. Each rule
  is a uniform, stably-identified contract that is BOTH the prompt Claude follows and
  a runnable check the linter (scripts/lint.py) enforces.
---

# AAP Ops — Contract-Governed Operating Manual

This skill governs all work against Red Hat Ansible Automation Platform across three
domains: **API ops** (driving automation controller / platform gateway over REST),
**EE/build** (Execution Environments via `ansible-builder`), and **governance**
(compliance controls). It is built on one idea:

> **A rule is a contract. The contract file is simultaneously the prompt and the linter.**

The same `contracts/rules.toml` you read to know *how to behave* is the file
`scripts/lint.py` parses to *prove behavior*. One source of truth, two consumers
(a language model and a checker). That is what makes the system repeatable and
auditable: a reviewer can point at a contract `id` in a failing build and at the
exact sentence in this manual that demanded it.

## Mental model (the visual)

Think of it as a layered casting mold, not a pile of advice:

```
  SKILL.md          ← operating manual: how to read + apply contracts (you are here)
      │ explains
      ▼
  contracts/rules.toml      ← SOURCE OF TRUTH: every base contract, uniform shape
      │ inherited by
      ▼
  contracts/extension.toml  ← DELTAS ONLY: override / disable / add, keyed by id
      │ merged + executed by
      ▼
  scripts/lint.py           ← the linter: same contracts, now as pass/fail evidence
```

Base contracts are the mold. An extension is a thin overlay that reshapes specific
cavities — it never re-pours the whole mold. The linter pours both together and
checks the casting.

## The contract data model

Every contract — base or extension — has the **same shape**. Uniformity is the
point: it makes the file scannable by a human, diffable in review, and iterable by
the linter without special cases. The full field reference is in
`references/contract-schema.md`; the essentials:

- `id` — stable, namespaced, never reused (e.g. `AAP-API-004`). The primary key.
- `domain` — `api` | `ee` | `governance`.
- `severity` — `error` | `warn` | `info`. Errors fail the build.
- `statement` — the normative rule, in imperative voice. This is the prompt text.
- `rationale` — *why* it exists, so the model (and the auditor) can reason about it.
- `check` — how the rule is verified. A typed, runnable check (see below).
- `fix` — the remediation to apply or recommend.
- `refs` — links to AAP docs or internal policy.

A contract with no automatable check uses `check.type = "manual"` and becomes a
required **attestation** — the linter reports it as `MANUAL` so it cannot silently
pass. Governance controls that depend on human judgment live here honestly rather
than being faked into green checkmarks.

## Check types the linter understands

| `check.type`      | What it does                                                            | Typical target        |
|-------------------|-------------------------------------------------------------------------|-----------------------|
| `regex_absent`    | Fails if `pattern` is found in `target` files                           | playbooks, scripts    |
| `regex_present`   | Fails if `pattern` is NOT found                                         | configs               |
| `file_present`    | Fails if no file matches `target` glob                                  | CODEOWNERS, EE files  |
| `yaml_key`        | Loads YAML, asserts `path` `op` `value` (e.g. `version == 3`)           | execution-environment |
| `requirements_pinned` | Fails if any entry in a requirements file lacks a version pin       | requirements.{yml,txt}|
| `command`         | Runs a shell command; pass on exit 0                                    | ansible-lint, builder |
| `http`            | GETs an AAP API endpoint, asserts on JSON (needs env creds)            | live controller       |
| `manual`          | No automation; emits an attestation line requiring sign-off            | governance            |

## How to USE this skill (workflow)

1. **Read the contracts that apply.** When a task lands, read `contracts/rules.toml`
   (and `contracts/extension.toml` if the repo has one). Filter to the relevant
   `domain`. These are your instructions — follow every `statement` whose `applies_to`
   matches what you are touching. Do not improvise around them.

2. **Do the work to spec.** Write the playbook / EE definition / API client so it
   would pass the contracts *before* you run the linter. The `statement` + `fix`
   fields tell you the target shape. Domain depth lives in `references/` —
   `api-ops.md`, `execution-environments.md`, `governance.md`. Read the relevant one
   when a contract points there.

3. **Lint to prove it.** Run the checker against the working tree:

   ```bash
   python scripts/lint.py --rules contracts/rules.toml \
       --extension contracts/extension.toml \
       --target /path/to/repo
   ```

   Add `--domain api` to scope a run, `--format json` for machine-readable output
   (CI), `--severity error` to gate only on errors. Live API checks (`http`) run
   only when `--live` is passed and `AAP_CONTROLLER_URL` + `AAP_TOKEN` are set.

4. **Resolve every finding.** For `FAIL`, apply the contract's `fix`. For `MANUAL`,
   record the attestation (who, when, evidence) — do not mark work done with
   unresolved manual controls. Re-run until the report is clean or every residual is
   a consciously `disable`d contract with a written reason in the extension.

5. **Report by id.** When you summarize work, cite contract ids
   (e.g. "passes AAP-EE-001..008, AAP-GOV-003 attested by …"). Ids are the audit trail.

## How to EXTEND (org/repo-specific deltas)

Never edit `rules.toml` to fit one repo — that corrupts the source of truth. Instead
add `contracts/extension.toml` with `inherits = "rules.toml"` and only the deltas:

- `[[override]]` by `id` to change fields (e.g. bump a `warn` to `error`).
- `[[disable]]` by `id` **with a mandatory `reason`** (recorded in the audit report).
- `[[contract]]` to add new contracts under your own namespace (e.g. `ACME-API-100`).

The linter deep-merges overrides onto base contracts, drops disabled ones (while
logging them), and appends new ones. See `contracts/extension.toml` for a worked
example and `references/contract-schema.md` for merge semantics.

## Authoring new contracts (keep them honest)

When you add a contract, make it earn its place:

- Give it a **runnable check** if at all possible. A contract that can only be
  `manual` is weaker than one with a `regex_absent` or `command` check — reach for
  automation first, fall back to attestation only when human judgment is genuinely
  required.
- Write the `rationale` so a smart engineer who disagrees could be persuaded. Rules
  without a "why" rot into cargo-cult checkboxes.
- Pick the **smallest** `pattern`/`target` that captures the intent. Overbroad checks
  create false positives, which train people to ignore the linter — the worst outcome.
- Never reuse a retired `id`. Stable ids are what make findings comparable over time.

## AAP version note

Contracts assume AAP 2.5+ unified platform (gateway at `/api/gateway/v1`, automation
controller at `/api/controller/v2`). The base path is a `[config]` value
(`controller_api`, `gateway_api`) so a 2.4 repo can override it in its extension
rather than forking the rules. Keep version-specific assumptions in `[config]`, not
hardcoded in `statement`s or `pattern`s.
