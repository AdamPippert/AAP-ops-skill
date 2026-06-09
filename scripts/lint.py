#!/usr/bin/env python3
"""
aap-ops contract linter
=======================

The other half of the contract idea: the SAME contracts/rules.toml that Claude
reads as instructions is parsed here and executed as pass/fail checks. One source
of truth, two consumers.

Pipeline:  load base -> merge [config] -> apply [[override]] -> apply [[disable]]
           -> append [[contract]] -> run each contract's check -> report.

Usage:
    python scripts/lint.py --rules contracts/rules.toml \\
        --extension contracts/extension.toml --target /path/to/repo
    python scripts/lint.py --target . --domain ee --format json
    python scripts/lint.py --target . --live          # also run http checks

Exit code: 1 if any `error`-severity contract FAILs (or any MANUAL is unresolved
when --strict-manual is set); 0 otherwise. Always 2 on a config/usage error.

Stdlib-only by design. PyYAML is used if present (better yaml_key checks) but the
linter degrades gracefully without it.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- TOML loader (stdlib tomllib on 3.11+, fall back to tomli) -----------------
try:
    import tomllib as _toml  # py311+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        sys.stderr.write(
            "ERROR: need Python 3.11+ (tomllib) or `pip install tomli`.\n"
        )
        raise SystemExit(2)

try:
    import yaml as _yaml  # optional
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

# --- result model --------------------------------------------------------------
PASS, FAIL, MANUAL, SKIP, DISABLED, ERROR = (
    "PASS", "FAIL", "MANUAL", "SKIP", "DISABLED", "ERROR")

SEVERITIES = ("error", "warn", "info")


@dataclass
class Result:
    contract_id: str
    domain: str
    severity: str
    title: str
    status: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.contract_id,
            "domain": self.domain,
            "severity": self.severity,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


# --- loading & merge -----------------------------------------------------------
def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return _toml.load(fh)


def _index(contracts: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in contracts:
        cid = c.get("id")
        if not cid:
            raise ValueError(f"contract missing id: {c!r}")
        if cid in out:
            raise ValueError(f"duplicate contract id in base rules: {cid}")
        out[cid] = c
    return out


def _deep_merge(base: dict, delta: dict) -> dict:
    out = dict(base)
    for k, v in delta.items():
        if k == "id":
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build_ruleset(rules_path: Path, ext_path: Path | None) -> tuple[dict, list[dict], list[dict]]:
    """Return (config, active_contracts, disabled_records)."""
    base = _load_toml(rules_path)
    config = dict(base.get("config", {}))
    contracts = _index(list(base.get("contract", [])))
    disabled_records: list[dict] = []

    if ext_path:
        ext = _load_toml(ext_path)
        # config overlay (shallow)
        config.update(ext.get("config", {}))
        # overrides
        for ov in ext.get("override", []):
            cid = ov.get("id")
            if cid not in contracts:
                raise ValueError(
                    f"override targets unknown contract id '{cid}' "
                    f"(typo or drift from base rules)")
            contracts[cid] = _deep_merge(contracts[cid], ov)
        # disables
        for ds in ext.get("disable", []):
            cid = ds.get("id")
            reason = ds.get("reason")
            if cid not in contracts:
                raise ValueError(f"disable targets unknown contract id '{cid}'")
            if not reason:
                raise ValueError(f"disable of '{cid}' requires a reason")
            disabled_records.append({"id": cid, "reason": reason,
                                     "title": contracts[cid].get("title", ""),
                                     "domain": contracts[cid].get("domain", "")})
            del contracts[cid]
        # new contracts
        for nc in ext.get("contract", []):
            cid = nc.get("id")
            if not cid:
                raise ValueError(f"extension contract missing id: {nc!r}")
            if cid in contracts:
                raise ValueError(
                    f"extension contract '{cid}' collides with a base id; "
                    f"use your own namespace")
            contracts[cid] = nc

    return config, list(contracts.values()), disabled_records


# --- ${config.*} interpolation -------------------------------------------------
_INTERP = re.compile(r"\$\{config\.([a-zA-Z0-9_]+)\}")


def interp(value: Any, config: dict) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in config:
                return m.group(0)
            v = config[key]
            return ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        return _INTERP.sub(repl, value)
    return value


# --- file discovery ------------------------------------------------------------
_IGNORE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                "._aapops_build_context"}


def _expand_braces(pattern: str) -> list[str]:
    """Tiny brace expansion: a.{yml,yaml} -> [a.yml, a.yaml]. Single group only,
    which covers every target glob used in the contracts."""
    m = re.search(r"\{([^}]*)\}", pattern)
    if not m:
        return [pattern]
    pre, post = pattern[:m.start()], pattern[m.end():]
    return [f"{pre}{opt}{post}" for opt in m.group(1).split(",")]


def find_files(root: Path, glob: str) -> list[Path]:
    patterns = _expand_braces(glob)
    hits: set[Path] = set()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _IGNORE_DIRS for part in p.parts):
            continue
        rel = p.relative_to(root).as_posix()
        name = p.name
        for pat in patterns:
            norm = pat[3:] if pat.startswith("**/") else pat
            if (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, norm)
                    or fnmatch.fnmatch(name, norm)):
                hits.add(p)
                break
    return sorted(hits)


# --- yaml_key path resolver ----------------------------------------------------
def _resolve_path(data: Any, path: str) -> tuple[bool, Any]:
    cur = data
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if token.startswith("["):
            idx = int(token[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or token not in cur:
                return False, None
            cur = cur[token]
    return True, cur


def _cmp(actual: Any, op: str, expected: Any) -> bool:
    try:
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op == ">=":
            return actual >= expected
        if op == "<=":
            return actual <= expected
        if op == ">":
            return actual > expected
        if op == "<":
            return actual < expected
        if op == "in":
            return actual in expected
        if op == "contains":
            return expected in actual
        if op == "exists":
            return actual is not None
    except TypeError:
        return False
    return False


# --- individual check runners --------------------------------------------------
def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def check_regex(contract: dict, root: Path, config: dict, *, want_present: bool) -> Result:
    chk = contract["check"]
    pattern = interp(chk["pattern"], config)
    # Scan whole-file text (not line-by-line) so patterns that intentionally span
    # lines via [\s\S] (e.g. a uri: task whose url is on the next line) match. We
    # do NOT set DOTALL, so a bare `.` stays line-bounded and patterns using .*
    # with a negative lookahead behave as written. Line numbers come from offset.
    rx = re.compile(pattern)
    files = find_files(root, chk.get("target", "**/*"))
    matched: list[str] = []
    for f in files:
        text = _read_text(f)
        for m in rx.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            snippet = text[m.start():m.end()].replace("\n", "\\n").strip()[:120]
            matched.append(f"{f.relative_to(root)}:{line_no}: {snippet}")
    base = _mk(contract)
    if want_present:  # regex_present: must be found in at least one targeted file
        if not files:
            base.status, base.detail = SKIP, "no files matched target glob"
        elif matched:
            base.status = PASS
        else:
            base.status = FAIL
            base.detail = interp(chk.get("message", "required pattern not found"), config)
    else:  # regex_absent: must NOT be found
        if matched:
            base.status = FAIL
            base.detail = interp(chk.get("message", "forbidden pattern found"), config)
            base.evidence = matched[:20]
        else:
            base.status = PASS
    return base


def check_file_present(contract: dict, root: Path, config: dict) -> Result:
    chk = contract["check"]
    files = find_files(root, chk.get("target", ""))
    base = _mk(contract)
    if files:
        base.status = PASS
        base.evidence = [str(f.relative_to(root)) for f in files[:5]]
    else:
        base.status = FAIL
        base.detail = interp(chk.get("message", "required file not found"), config)
    return base


def check_yaml_key(contract: dict, root: Path, config: dict) -> Result:
    chk = contract["check"]
    files = find_files(root, chk.get("target", "**/*.yml"))
    base = _mk(contract)
    if not files:
        base.status, base.detail = SKIP, "no files matched target glob"
        return base
    path, op, expected = chk["path"], chk.get("op", "=="), chk.get("value")
    failures: list[str] = []
    for f in files:
        text = _read_text(f)
        if _HAVE_YAML:
            try:
                data = _yaml.safe_load(text)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{f.relative_to(root)}: YAML parse error: {exc}")
                continue
            found, actual = _resolve_path(data, path)
        else:
            # degraded: only top-level scalar keys via regex
            if "." in path or "[" in path:
                base.status = SKIP
                base.detail = "PyYAML not installed; nested path not checkable"
                return base
            m = re.search(rf"(?m)^{re.escape(path)}\s*:\s*(.+?)\s*$", text)
            found = bool(m)
            actual = None
            if m:
                raw = m.group(1)
                actual = int(raw) if raw.isdigit() else raw.strip("'\"")
        if not found:
            failures.append(f"{f.relative_to(root)}: key '{path}' missing")
        elif not _cmp(actual, op, expected):
            failures.append(f"{f.relative_to(root)}: {path}={actual!r}, want {op} {expected!r}")
    if failures:
        base.status = FAIL
        base.detail = interp(chk.get("message", "yaml_key assertion failed"), config)
        base.evidence = failures[:20]
    else:
        base.status = PASS
    return base


def check_requirements_pinned(contract: dict, root: Path, config: dict) -> Result:
    chk = contract["check"]
    files = find_files(root, chk.get("target", "**/requirements.{yml,txt}"))
    base = _mk(contract)
    if not files:
        base.status, base.detail = SKIP, "no requirements files matched"
        return base
    unpinned: list[str] = []
    for f in files:
        text = _read_text(f)
        if f.name.endswith((".yml", ".yaml")):
            unpinned += _galaxy_unpinned(f, text, root)
        else:
            unpinned += _pip_unpinned(f, text, root)
    if unpinned:
        base.status = FAIL
        base.detail = interp(chk.get("message", "unpinned dependency"), config)
        base.evidence = unpinned[:20]
    else:
        base.status = PASS
    return base


def _galaxy_unpinned(f: Path, text: str, root: Path) -> list[str]:
    out: list[str] = []
    if _HAVE_YAML:
        try:
            data = _yaml.safe_load(text) or {}
        except Exception:  # noqa: BLE001
            return [f"{f.relative_to(root)}: unparseable requirements.yml"]
        cols = data.get("collections", []) if isinstance(data, dict) else []
        for c in cols:
            if isinstance(c, str):
                out.append(f"{f.relative_to(root)}: collection '{c}' has no version")
            elif isinstance(c, dict) and not c.get("version"):
                out.append(f"{f.relative_to(root)}: collection '{c.get('name','?')}' has no version")
    else:
        # degraded heuristic: a `- name:` block without a sibling `version:`
        blocks = re.split(r"(?m)^\s*-\s+", text)
        for b in blocks:
            if "name:" in b and "version:" not in b:
                nm = re.search(r"name:\s*(\S+)", b)
                out.append(f"{f.relative_to(root)}: collection '{nm.group(1) if nm else '?'}' has no version")
    return out


def _pip_unpinned(f: Path, text: str, root: Path) -> list[str]:
    out: list[str] = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-r", "--", "git+", "http://", "https://")):
            continue
        if not re.search(r"[=~<>!]=|[<>]", line):
            out.append(f"{f.relative_to(root)}:{i}: '{line}' is not version-pinned")
    return out


def check_command(contract: dict, root: Path, config: dict) -> Result:
    chk = contract["check"]
    cmd = interp(chk["cmd"], config)
    base = _mk(contract)
    exe = cmd.split()[0]
    if not _which(exe):
        base.status = SKIP
        base.detail = f"command '{exe}' not on PATH"
        return base
    try:
        proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                              text=True, timeout=600)
    except subprocess.TimeoutExpired:
        base.status, base.detail = FAIL, "command timed out"
        return base
    if proc.returncode == 0:
        base.status = PASS
    else:
        base.status = FAIL
        base.detail = interp(chk.get("message", f"command failed (rc={proc.returncode})"), config)
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        base.evidence = tail
    return base


def check_http(contract: dict, root: Path, config: dict, *, live: bool) -> Result:
    chk = contract["check"]
    base = _mk(contract)
    if not live:
        base.status = SKIP
        base.detail = "live check; pass --live with AAP_CONTROLLER_URL + AAP_TOKEN"
        return base
    controller = os.environ.get("AAP_CONTROLLER_URL")
    token = os.environ.get("AAP_TOKEN")
    if not controller:
        base.status, base.detail = SKIP, "AAP_CONTROLLER_URL not set"
        return base
    endpoint = interp(chk["endpoint"], config)
    url = controller.rstrip("/") + endpoint
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, ""
    except Exception as exc:  # noqa: BLE001
        base.status, base.detail = FAIL, f"request failed: {exc}"
        return base
    op = chk.get("op", "status_eq")
    if op == "status_eq":
        ok = status == chk.get("value", 200)
        base.status = PASS if ok else FAIL
        base.detail = "" if ok else f"got HTTP {status}, want {chk.get('value')}"
    elif op == "json_path_eq":
        try:
            found, actual = _resolve_path(json.loads(body), chk["path"])
        except json.JSONDecodeError:
            base.status, base.detail = FAIL, "response not JSON"
            return base
        ok = found and actual == chk.get("value")
        base.status = PASS if ok else FAIL
        base.detail = "" if ok else f"{chk['path']}={actual!r}, want {chk.get('value')!r}"
    else:
        base.status, base.detail = ERROR, f"unknown http op '{op}'"
    base.evidence = [f"GET {url} -> {status}"]
    return base


def check_manual(contract: dict, root: Path, config: dict) -> Result:
    base = _mk(contract)
    base.status = MANUAL
    base.detail = interp(contract["check"].get("prompt", "manual attestation required"), config)
    return base


# --- helpers -------------------------------------------------------------------
def _mk(contract: dict) -> Result:
    return Result(
        contract_id=contract["id"],
        domain=contract.get("domain", "?"),
        severity=contract.get("severity", "warn"),
        title=contract.get("title", ""),
        status=SKIP,
    )


_WHICH_CACHE: dict[str, bool] = {}


def _which(exe: str) -> bool:
    if exe not in _WHICH_CACHE:
        _WHICH_CACHE[exe] = any(
            (Path(d) / exe).exists() for d in os.environ.get("PATH", "").split(os.pathsep))
    return _WHICH_CACHE[exe]


_DISPATCH = {
    "regex_absent": lambda c, r, cfg, live: check_regex(c, r, cfg, want_present=False),
    "regex_present": lambda c, r, cfg, live: check_regex(c, r, cfg, want_present=True),
    "file_present": lambda c, r, cfg, live: check_file_present(c, r, cfg),
    "yaml_key": lambda c, r, cfg, live: check_yaml_key(c, r, cfg),
    "requirements_pinned": lambda c, r, cfg, live: check_requirements_pinned(c, r, cfg),
    "command": lambda c, r, cfg, live: check_command(c, r, cfg),
    "http": lambda c, r, cfg, live: check_http(c, r, cfg, live=live),
    "manual": lambda c, r, cfg, live: check_manual(c, r, cfg),
}


def run_contract(contract: dict, root: Path, config: dict, live: bool) -> Result:
    chk = contract.get("check") or {}
    ctype = chk.get("type")
    runner = _DISPATCH.get(ctype)
    if runner is None:
        res = _mk(contract)
        res.status, res.detail = ERROR, f"unknown check type '{ctype}'"
        return res
    try:
        return runner(contract, root, config, live)
    except Exception as exc:  # noqa: BLE001
        res = _mk(contract)
        res.status, res.detail = ERROR, f"check raised: {exc}"
        return res


# --- reporting -----------------------------------------------------------------
_GLYPH = {PASS: "✓", FAIL: "✗", MANUAL: "✋", SKIP: "–",
          DISABLED: "⊘", ERROR: "!"}


def render_text(results: list[Result], disabled: list[dict], config: dict) -> str:
    lines = ["", "AAP-OPS CONTRACT REPORT", "=" * 60]
    order = {"api": 0, "ee": 1, "governance": 2}
    for dom in sorted({r.domain for r in results}, key=lambda d: order.get(d, 9)):
        lines.append(f"\n[{dom.upper()}]")
        for r in [x for x in results if x.domain == dom]:
            g = _GLYPH.get(r.status, "?")
            head = f"  {g} {r.status:<8} {r.contract_id:<14} {r.title}  ({r.severity})"
            lines.append(head)
            if r.detail:
                lines.append(f"        -> {r.detail}")
            for ev in r.evidence:
                lines.append(f"           {ev}")
    if disabled:
        lines.append("\n[DISABLED via extension]")
        for d in disabled:
            lines.append(f"  ⊘ {d['id']:<14} {d['title']}")
            lines.append(f"        reason: {d['reason']}")
    # summary
    counts = {s: sum(1 for r in results if r.status == s)
              for s in (PASS, FAIL, MANUAL, SKIP, ERROR)}
    err_fail = sum(1 for r in results if r.status == FAIL and r.severity == "error")
    lines += ["", "-" * 60,
              f"PASS {counts[PASS]}  FAIL {counts[FAIL]} "
              f"(error-severity {err_fail})  MANUAL {counts[MANUAL]}  "
              f"SKIP {counts[SKIP]}  ERROR {counts[ERROR]}  "
              f"DISABLED {len(disabled)}", ""]
    return "\n".join(lines)


def render_json(results: list[Result], disabled: list[dict], config: dict) -> str:
    err_fail = sum(1 for r in results if r.status == FAIL and r.severity == "error")
    return json.dumps({
        "config": config,
        "results": [r.to_dict() for r in results],
        "disabled": disabled,
        "summary": {
            "pass": sum(1 for r in results if r.status == PASS),
            "fail": sum(1 for r in results if r.status == FAIL),
            "fail_error_severity": err_fail,
            "manual": sum(1 for r in results if r.status == MANUAL),
            "skip": sum(1 for r in results if r.status == SKIP),
            "error": sum(1 for r in results if r.status == ERROR),
            "disabled": len(disabled),
        },
    }, indent=2)


# --- main ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="aap-ops contract linter")
    here = Path(__file__).resolve().parent
    ap.add_argument("--rules", type=Path,
                    default=here.parent / "contracts" / "rules.toml")
    ap.add_argument("--extension", type=Path, default=None,
                    help="optional extension.toml with deltas")
    ap.add_argument("--target", type=Path, default=Path.cwd(),
                    help="repo/dir to check (default: cwd)")
    ap.add_argument("--domain", choices=["api", "ee", "governance"], default=None)
    ap.add_argument("--severity", choices=SEVERITIES, default=None,
                    help="only run contracts at/above this severity gate")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--live", action="store_true",
                    help="run http checks (needs AAP_CONTROLLER_URL/AAP_TOKEN)")
    ap.add_argument("--strict-manual", action="store_true",
                    help="treat unresolved MANUAL controls as failures for exit code")
    args = ap.parse_args(argv)

    if not args.rules.exists():
        sys.stderr.write(f"ERROR: rules file not found: {args.rules}\n")
        return 2
    ext = args.extension if (args.extension and args.extension.exists()) else None
    if args.extension and not ext:
        sys.stderr.write(f"ERROR: extension not found: {args.extension}\n")
        return 2

    try:
        config, contracts, disabled = build_ruleset(args.rules, ext)
    except ValueError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2

    sev_rank = {"error": 3, "warn": 2, "info": 1}
    gate = sev_rank.get(args.severity, 0)

    results: list[Result] = []
    for c in contracts:
        if args.domain and c.get("domain") != args.domain:
            continue
        if gate and sev_rank.get(c.get("severity", "warn"), 0) < gate:
            continue
        results.append(run_contract(c, args.target, config, args.live))

    out = (render_json if args.format == "json" else render_text)(results, disabled, config)
    print(out)

    err_fail = any(r.status == FAIL and r.severity == "error" for r in results)
    any_error = any(r.status == ERROR for r in results)
    manual_block = args.strict_manual and any(r.status == MANUAL for r in results)
    return 1 if (err_fail or any_error or manual_block) else 0


if __name__ == "__main__":
    raise SystemExit(main())
