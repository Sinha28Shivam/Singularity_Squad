"""AI release gate (AI plan D4). Exit code 0 only when every gate passes.

    python scripts/ai_release_gate.py [--allow-missing] [--tolerance 0.0] [--report data/eval/reports/release_report.json]

Gates:
1. ``security_tests``: pytest over the required security/invariant suites. A missing suite fails the gate
   (loud warning) unless ``--allow-missing`` is passed explicitly; skipped tests are reported.
2. ``rag_benchmark``: offline RAG benchmark vs ``data/eval/baseline_rag_bench_v1.json`` (zero tolerance for
   scope/citation/authorization regressions; quality metrics within ``--tolerance``).
3. ``secret_scan``: tracked files (``git ls-files``) scanned for API keys, credentialed MongoDB URIs and
   private keys. Obvious placeholders/fixtures are ignored (see ``is_placeholder``).

Writes a JSON release report with each gate's pass/fail. Never prints secret values (only file:line).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
DEFAULT_REPORT = ROOT / "data" / "eval" / "reports" / "release_report.json"
BASELINE = ROOT / "data" / "eval" / "baseline_rag_bench_v1.json"
BENCH_OUT = ROOT / "data" / "eval" / "reports" / "release_benchmark.json"

REQUIRED_TEST_PATHS = (
    "tests/ai_security",
    "tests/policy_rag",
    "tests/test_rule_extraction.py",
    "tests/test_policy_authorization.py",
    "tests/test_no_external_execution.py",
    "tests/test_retrieval_degradation.py",
    "tests/test_rag_benchmark.py",
    "tests/test_ai_ops_api.py",
)

SECRET_PATTERNS = {
    "openai_style_api_key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "credentialed_mongodb_uri": re.compile(r"mongodb(?:\+srv)?://[^\s/:@'\"<>]+:[^\s@'\"<>]+@"),
    "private_key_block": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
}
EXCLUDED_PARTS = {"venv", ".venv", "node_modules", ".git", "__pycache__"}
EXCLUDED_NAMES = {".env.example"}
BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".woff", ".woff2", ".ttf", ".pyc"}
# Values that are obviously placeholders or test fixtures, not live credentials.
PLACEHOLDER_HINTS = ("example", "placeholder", "changeme", "your-", "your_", "xxx", "dummy", "fake",
                     "secret-user", "user:secret@", "user:pass@", "redact", "abcdefghijklmnop")


# ---------------------------------------------------------------------------------------- secret scan
_TOKEN_END = re.compile(r"[\s'\"`<>]")


def is_placeholder(match: str, line: str = "", start: Optional[int] = None) -> bool:
    """Only the matched value (extended to the end of its token, e.g. a URI's host part) decides: hint words
    elsewhere on the line must not hide a real secret."""
    value = match
    if start is not None and line:
        end = _TOKEN_END.search(line, start)
        value = line[start:end.start() if end else len(line)]
    low = value.lower()
    return any(h in low for h in PLACEHOLDER_HINTS)


def scan_text(text: str, path: str = "<text>") -> list[dict]:
    """Findings (rule, path, line) for secrets in ``text``. Never returns the secret value itself."""
    findings = []
    for n, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in SECRET_PATTERNS.items():
            for m in pattern.finditer(line):
                if is_placeholder(m.group(0), line, m.start()):
                    continue
                findings.append({"rule": rule, "path": path, "line": n})
    return findings


def _excluded(rel: str) -> bool:
    p = Path(rel)
    return bool(EXCLUDED_PARTS & set(p.parts)) or p.name in EXCLUDED_NAMES or p.suffix.lower() in BINARY_SUFFIXES


def tracked_files(root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]


def scan_files(files: Iterable[str], root: Path = ROOT) -> list[dict]:
    findings: list[dict] = []
    for rel in files:
        if _excluded(rel):
            continue
        path = root / rel
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(scan_text(text, rel))
    return findings


# ------------------------------------------------------------------------------------- test gate
def resolve_test_paths(paths: Iterable[str], root: Path = ROOT) -> tuple[list[str], list[str]]:
    present, missing = [], []
    for p in paths:
        (present if (root / p).exists() else missing).append(p)
    return present, missing


def _parse_pytest_summary(output: str) -> dict:
    counts = {}
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    for num, word in re.findall(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected)", tail):
        counts["errors" if word.startswith("error") else word] = int(num)
    return counts


def gate_security_tests(allow_missing: bool) -> dict:
    present, missing = resolve_test_paths(REQUIRED_TEST_PATHS)
    for m in missing:
        print(f"!!! WARNING: required security test path is MISSING: {m}", file=sys.stderr)
    gate = {"name": "security_tests", "required": list(REQUIRED_TEST_PATHS), "missing": missing,
            "allow_missing": allow_missing}
    if not present:
        gate.update(passed=False, reason="no required test paths exist")
        return gate
    proc = subprocess.run([PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", *present],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    counts = _parse_pytest_summary(proc.stdout)
    gate.update(exit_code=proc.returncode, counts=counts)
    if proc.returncode != 0:
        print(proc.stdout[-4000:], file=sys.stderr)
    ok = proc.returncode == 0 and (allow_missing or not missing)
    if missing and not allow_missing:
        gate["reason"] = "required security tests are missing (pass --allow-missing only with explicit approval)"
    elif proc.returncode != 0:
        gate["reason"] = "security/invariant tests failed"
    gate["passed"] = ok
    return gate


# ---------------------------------------------------------------------------------- benchmark gate
def gate_benchmark(tolerance: float, baseline: Path = BASELINE) -> dict:
    gate = {"name": "rag_benchmark", "baseline": str(baseline.relative_to(ROOT)) if baseline.is_relative_to(ROOT)
            else str(baseline), "tolerance": tolerance}
    if not baseline.exists():
        gate.update(passed=False, reason="baseline report missing")
        return gate
    proc = subprocess.run([PYTHON, str(ROOT / "scripts" / "run_rag_benchmark.py"), "--out", str(BENCH_OUT),
                           "--baseline", str(baseline), "--tolerance", str(tolerance)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    gate["exit_code"] = proc.returncode
    gate["output"] = proc.stdout.strip().splitlines()[-12:]
    try:
        report = json.loads(BENCH_OUT.read_text(encoding="utf-8"))
        gate["overall"] = report.get("overall")
        gate["regressions"] = (report.get("baseline_comparison") or {}).get("regressions")
    except (OSError, ValueError):
        gate["reason"] = "benchmark report not produced"
    if proc.returncode != 0:
        print(proc.stdout[-3000:] + proc.stderr[-2000:], file=sys.stderr)
    gate["passed"] = proc.returncode == 0
    return gate


def gate_secret_scan(files: Optional[list[str]] = None) -> dict:
    try:
        files = tracked_files() if files is None else files
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"name": "secret_scan", "passed": False, "reason": f"git ls-files failed ({type(exc).__name__})"}
    findings = scan_files(files)
    for f in findings:
        print(f"!!! possible secret ({f['rule']}) at {f['path']}:{f['line']}", file=sys.stderr)
    return {"name": "secret_scan", "files_scanned": len(files), "findings": findings, "passed": not findings}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AI release gate")
    ap.add_argument("--allow-missing", action="store_true",
                    help="do not fail when a required security test path is missing (requires explicit approval)")
    ap.add_argument("--tolerance", type=float, default=0.0)
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--skip-tests", action="store_true", help="local debugging only; the gate then FAILS")
    args = ap.parse_args(argv)

    gates = []
    if args.skip_tests:
        gates.append({"name": "security_tests", "passed": False, "reason": "skipped by --skip-tests"})
    else:
        gates.append(gate_security_tests(args.allow_missing))
    gates.append(gate_benchmark(args.tolerance))
    gates.append(gate_secret_scan())
    passed = all(g["passed"] for g in gates)
    report = {"report_type": "ai_release_gate", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "passed": passed, "gates": gates}
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    for g in gates:
        print(f"[{'PASS' if g['passed'] else 'FAIL'}] {g['name']}" + (f" - {g['reason']}" if g.get("reason") else ""))
    print(f"release gate: {'PASSED' if passed else 'FAILED'} (report: {out})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
