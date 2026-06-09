# Domain Reference: Execution Environments (ansible-builder v3)

Depth behind the `domain = "ee"` contracts. Read when authoring or reviewing an
`execution-environment.yml`.

## Why EEs are governed like build artifacts

An Execution Environment is the container every job runs inside. If it is not
reproducible, your automation is not reproducible — the same playbook can behave
differently because the EE underneath it drifted. The EE contracts exist to make
the image a deterministic, attributable, supply-chain-clean artifact.

## Minimal compliant definition

```yaml
version: 3                                    # AAP-EE-001

images:
  base_image:
    name: registry.redhat.io/ansible-automation-platform-25/ee-minimal-rhel9:1.0.0-577
                                              # AAP-EE-002 (pinned), AAP-EE-005 (approved registry)

dependencies:
  ansible_core:
    package_pip: ansible-core==2.16.6         # pinned
  ansible_runner:
    package_pip: ansible-runner==2.4.0
  galaxy: requirements.yml                    # AAP-EE-003 checks pins here
  python: requirements.txt                    # AAP-EE-004 checks pins here
  system: bindep.txt

additional_build_steps:
  prepend_final:
    - RUN pip install --no-cache-dir distro==1.9.0   # pinned -> passes AAP-EE-006
  append_final:
    - LABEL maintainer="platform-team"
```

The seven top-level v3 sections are `version`, `images`, `dependencies`, `options`,
`additional_build_files`, `additional_build_steps`, and (build-time) overrides.
Omitting `version: 3` makes ansible-builder assume v1 and silently drop v3-only
sections — the trap `AAP-EE-001` catches.

## requirements.yml (galaxy) — `AAP-EE-003`

```yaml
collections:
  - name: ansible.controller
    version: ">=4.5.0,<5.0.0"     # pin REQUIRED
  - name: community.general
    version: "9.2.0"
```

A bare `- name: foo` with no `version` fails the check: it drifts between builds.

## requirements.txt (pip) — `AAP-EE-004`

```
requests==2.32.3
jmespath~=1.0
```

Unbounded entries (`requests`) re-resolve every build; a transitive bump can break a
collection at runtime with zero change to your repo.

## Pinning & registries — `AAP-EE-002`, `AAP-EE-005`

Pin the base image to a tag, ideally a `@sha256:` digest. Pull only from approved
registries (`${config.approved_registries}` — override in your extension if you
mirror internally). `:latest` and `docker.io/...` both fail: they import drift and
unaudited supply-chain risk into every run.

## Build steps — `AAP-EE-006`

Inside `additional_build_steps`, never run `dnf/yum/apt update` or `pip install`
without versions — that smuggles drift back in past all your pinning. Pin exact
versions, or move the dependency into the declarative `dependencies` section so it is
tracked and diffable.

## Build proof — `AAP-EE-007`

The cheapest end-to-end check is that ansible-builder can produce a build context:

```bash
ansible-builder create -f execution-environment.yml -c ./context
```

The linter runs this (`command` check) and reads the traceback on failure. It
`SKIP`s cleanly if `ansible-builder` is not installed in the linting environment.
