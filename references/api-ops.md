# Domain Reference: AAP API Ops

Depth behind the `domain = "api"` contracts. Read when writing playbooks or scripts
that drive automation controller / platform gateway.

## Topology (AAP 2.5+ unified platform)

The platform gateway fronts everything. Two roots matter:

- `${config.gateway_api}` → `/api/gateway/v1` — authentication, settings, RBAC,
  organizations/teams, service index. (`AAP-API-001`)
- `${config.controller_api}` → `/api/controller/v2` — the automation-execution
  objects: job templates, inventories, projects, credentials, jobs, schedules.

Discover the live root instead of assuming: `GET /api/` returns `current_version`.
Legacy `/api/v2` still resolves on some upgrades but is the wrong contract to code
against — that is exactly what `AAP-API-001` guards.

## Authentication (`AAP-API-002`, `AAP-API-004`)

Prefer, in order:

1. An **AAP credential** injected into the job at runtime — the playbook never sees
   the secret material, only the controller does.
2. An **OAuth2 token** sourced from a vault lookup and sent as
   `Authorization: Bearer <token>`. Scope it to the minimum (read unless you write).
3. Vaulted basic auth only where tokens are unavailable.

Never put the secret in the file. Never pass `-k` / `--insecure` / `validate_certs:
false`; trust the CA via the EE or system store instead. A leaked controller token
is full control-plane access until someone notices and rotates it.

## Idempotent object management (`AAP-API-005`)

Use the supported collections, not raw `uri`:

```yaml
- name: Ensure a job template exists
  ansible.controller.job_template:
    name: deploy-web
    project: web-platform
    playbook: site.yml
    inventory: prod
    execution_environment: web-ee-2.16
    controller_host: "{{ aap_host }}"
    controller_oauthtoken: "{{ vault_aap_token }}"
    validate_certs: true
    state: present
```

The module handles existence, drift, pagination, and version differences. A
hand-rolled `uri` POST reimplements all of that and breaks silently on upgrade.

## Pagination (`AAP-API-003`)

The controller clamps/rejects `page_size > 200`. Page properly:

```python
url = f"{base}{controller_api}/hosts/?page_size=200"
while url:
    r = session.get(url, verify=ca_bundle).json()
    yield from r["results"]
    url = r["next"]   # absolute or None
```

Assuming one big page either errors or silently truncates — wrong inventories,
partial reconciliation.

## Health gating (`AAP-API-006`)

Before a batch of writes, gate on `GET ${config.controller_api}/ping/` returning
200 with the expected instances active. Refuse to push state into a degraded or
HA-split controller; partial writes are worse than no run.

## Live linting

`AAP-API-006` is an `http` check. It runs only with `--live` and:

```bash
export AAP_CONTROLLER_URL=https://aap.example.com
export AAP_TOKEN=<vaulted token, exported at runtime>
python scripts/lint.py --target . --domain api --live
```

Without `--live` it reports `SKIP` so static CI stays hermetic.
