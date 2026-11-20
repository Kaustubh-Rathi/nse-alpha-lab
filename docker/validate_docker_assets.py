"""Offline validation of the Docker/CI assets (no Docker daemon required)."""
from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
failures: list[str] = []
notes: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


print("== 1. YAML parse (yaml.safe_load, strict-ish) ==")
parsed = {}
for rel in ("docker/compose.yaml", ".github/workflows/ci.yml", ".github/workflows/docker.yml"):
    p = ROOT / rel
    check(p.is_file(), f"{rel} exists")
    try:
        parsed[rel] = yaml.safe_load(p.read_text(encoding="utf-8"))
        check(isinstance(parsed[rel], dict), f"{rel} parses to a mapping (keys={list(parsed[rel])})")
    except yaml.YAMLError as exc:  # pragma: no cover
        check(False, f"{rel} YAML error: {exc}")

print()
print("== 2. Anchors/aliases resolve (no merge surprises) ==")
c = parsed["docker/compose.yaml"]
svcs = c["services"]
check(c.get("name") == "nse-alpha-lab", f"compose project name = {c.get('name')!r}")
check(set(svcs) == {"lab", "test", "cli", "fetch", "pdf"}, f"services = {sorted(svcs)}")

print()
print("== 3. No Postgres, no Redis (the measured requirement) ==")
blob = (ROOT / "docker/compose.yaml").read_text(encoding="utf-8").lower()
svc_blob = yaml.safe_dump(svcs).lower()
check("postgres" not in svcs and "redis" not in svcs, "no such *service names*")
check("image: postgres" not in blob and "image: redis" not in blob, "no postgres/redis base images")
check("5432" not in blob and "6379" not in blob, "no 5432/6379 ports")
check("psycopg" not in svc_blob and "sqlalchemy" not in svc_blob, "no postgres client deps")

print()
print("== 4. compose semantics ==")
lab = svcs["lab"]
check(lab.get("ports") == ["8501:8501"], f"lab publishes 8501 -> {lab.get('ports')}")
check(lab.get("build", {}).get("context") == "..", "lab build context is the repo root")
check(lab.get("build", {}).get("dockerfile") == "docker/Dockerfile", "lab Dockerfile path")
check("../data:/app/data" in lab.get("volumes", []), f"lab mounts ./data -> /app/data: {lab.get('volumes')}")
check("streamlit" in " ".join(map(str, lab.get("command", []))) and "8501" in " ".join(map(str, lab.get("command", []))),
      "lab runs streamlit on 8501")
check(lab.get("restart") == "unless-stopped", f"lab restart={lab.get('restart')!r}")
check(svcs["fetch"].get("profiles") == ["fetch"], f"fetch profiles={svcs['fetch'].get('profiles')}")
check(svcs["pdf"].get("profiles") == ["pdf"], f"pdf profiles={svcs['pdf'].get('profiles')}")
check(svcs["pdf"].get("build", {}).get("args", {}).get("UV_EXTRA") == "pdf", "pdf builds with --extra pdf")
check(svcs["test"].get("profiles") == ["test"], f"test profiles={svcs['test'].get('profiles')}")
check(svcs["test"].get("build", {}).get("target") == "test", "test service builds the `test` target")
check(svcs["cli"].get("build", {}).get("target") == "test", "cli service builds the `test` target (pytest available)")
check(svcs["fetch"].get("entrypoint") == ["python", "-m", "nse_pipeline", "fetch"],
      f"fetch lane entrypoint is the CLI fetch subcommand: {svcs['fetch'].get('entrypoint')}")
check(svcs["test"].get("command", [])[-2:] == ["pytest", "-q"]
      or "pytest" in " ".join(map(str, svcs["test"].get("command", []))),
      f"test service runs pytest: {svcs['test'].get('command')}")
for name in ("cli", "fetch", "pdf"):
    check("../data:/app/data" in svcs[name].get("volumes", []), f"{name} mounts ./data -> /app/data")
check(lab["build"]["args"]["PYTHON_BASE"] == "${PYTHON_BASE:-python:3.14-slim}", "version-pinned base override")
for name, svc in svcs.items():
    check("no-new-privileges:true" in str(svc.get("security_opt")), f"{name} has no-new-privileges")
    check("ALL" in str(svc.get("cap_drop")), f"{name} drops all capabilities")
check(lab["healthcheck"]["test"] == ["CMD", "python", "-m", "nse_pipeline", "--help"],
      f"lab healthcheck exercises the real CLI: {lab['healthcheck']['test']}")

default_on = [n for n, s in svcs.items() if not s.get("profiles")]
print(f"  note   services started by a bare `up`: {default_on}")
notes.append(f"default-on services: {default_on}")

print()
print("== 5. Dockerfile structure ==")
df = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
checks = {
    "multi-stage builder": "FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder" in df,
    "runtime stage": re.search(r"^FROM \$\{PYTHON_BASE\} AS runtime", df, re.M) is not None,
    "test stage (builder + dev group)": re.search(r"^FROM builder AS test", df, re.M) is not None,
    "UV_COMPILE_BYTECODE=1": "UV_COMPILE_BYTECODE=1" in df,
    "UV_LINK_MODE=copy": "UV_LINK_MODE=copy" in df,
    "pyproject+uv.lock copied first": df.index("COPY pyproject.toml uv.lock") < df.index("COPY src/"),
    "uv sync --locked --no-dev": "uv sync --locked --no-install-project --no-dev" in df,
    "test stage syncs dev group": "uv sync --locked --no-install-project --dev" in df,
    "PYTHONPATH=/app/src": "PYTHONPATH=/app/src" in df,
    "data volume /app/data": 'VOLUME ["/app/data"]' in df,
    "HEALTHCHECK present": "HEALTHCHECK" in df,
    "non-root uid 1000": "--uid \"${APP_UID}\"" in df and 'ARG APP_UID=1000' in df and "USER app:app" in df,
    "copies src/ examples/ research/ docs/": all(f"COPY {d}/" in df for d in ("src", "examples", "research", "docs")),
    "syntax directive": df.startswith("# syntax=docker/dockerfile:1"),
}
for k, v in checks.items():
    check(v, f"Dockerfile: {k}")

# tests/ must be baked into the `test` stage only - never into the runtime image.
test_stage_at = df.index("AS test")
runtime_stage_at = df.index("AS runtime")
tests_copy_at = df.index("COPY tests/")
check(test_stage_at < tests_copy_at < runtime_stage_at,
      f"Dockerfile: tests/ copied in the test stage only (test@{test_stage_at}, "
      f"copy@{tests_copy_at}, runtime@{runtime_stage_at})")
check("*.internal.csv" in df, "Dockerfile: build asserts internal CSVs are absent from the context")
stages = re.findall(r"^FROM\s+(\S+)\s+AS\s+(\S+)", df, re.M)
print(f"  note   stages: {stages}")

print()
print("== 6. .dockerignore ==")
di = (ROOT / ".dockerignore").read_text(encoding="utf-8")
patterns = [ln.strip() for ln in di.splitlines() if ln.strip() and not ln.strip().startswith("#")]
required = ["data/", ".git/", ".venv/", "**/__pycache__/", "**/*.pdf", "**/*.zip", "**/*.parquet",
            "verification/", "**/*.internal.csv"]
for req in required:
    check(req in patterns, f".dockerignore excludes {req!r}")
check("tests/" not in patterns,
      ".dockerignore does NOT exclude tests/ (the test build target needs it)")

print()
print("== 7. Workflows ==")
ci = parsed[".github/workflows/ci.yml"]
# YAML 1.1 turns bare `on:` into True; handle both spellings.
trig = ci.get("on", ci.get(True))
check(isinstance(trig, dict) and "push" in trig and "pull_request" in trig, f"ci triggers = {list(trig or {})}")
ci_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
check("runs-on: ubuntu-latest" in ci_text, "ci runs on ubuntu-latest")
check("astral-sh/setup-uv" in ci_text, "ci uses astral-sh/setup-uv")
check("uv sync --locked" in ci_text, "ci runs uv sync --locked")
check("uv run pytest -q" in ci_text, "ci runs uv run pytest -q")

dk = parsed[".github/workflows/docker.yml"]
dtrig = dk.get("on", dk.get(True))
check("push" in (dtrig or {}), f"docker triggers = {list(dtrig or {})}")
dk_text = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
check("tags: [\"v*.*.*\"]" in dk_text, "docker workflow fires on v*.*.* tags")
check("${{ github.repository }}" in dk_text, "docker workflow pushes to ghcr.io/${{ github.repository }}")
check("packages: write" in dk_text, "docker workflow has packages: write")
check("docker/login-action" in dk_text and "docker/build-push-action" in dk_text, "login + buildx push actions")
check("file: docker/Dockerfile" in dk_text and "context: ." in dk_text, "builds docker/Dockerfile from repo root")

print()
print("== RESULT ==")
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(f"ALL CHECKS PASSED ({len(notes)} notes)")
for n in notes:
    print("  -", n)
