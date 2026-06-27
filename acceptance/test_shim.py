#!/usr/bin/env python3
"""Back-compat shim tests — the `harness` import name must keep working through 1.x.

The package was renamed `harness` → `agents_never_sleep` for 1.0; `harness/` is a thin
deprecation shim (removed in 2.0). These tests guard the SHIPPED behaviour so a future edit
can't silently drop it (the failure mode that would 404 every `python -m harness.run` recipe):

  - `import harness` emits exactly ONE DeprecationWarning.
  - `harness.__version__` == `agents_never_sleep.__version__`.
  - `harness.run` / `harness.launcher` resolve to the real modules (via `__path__` aliasing).

Run in subprocesses so each assertion sees a clean import state. Exit 0 = GREEN.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)


def _run(code: str):
    """Run a snippet in a child interpreter with the checkout on PYTHONPATH."""
    env = dict(os.environ, PYTHONPATH=SKILL_ROOT)
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )


def test_deprecation_warning_fires(failures):
    # Under -W error a DeprecationWarning becomes an exception → non-zero exit proves it fired.
    r = _run("import warnings; warnings.simplefilter('error'); import harness")
    if r.returncode == 0:
        failures.append("[warn] import harness did NOT raise under -W error (no DeprecationWarning)")
    elif "DeprecationWarning" not in r.stderr:
        failures.append(f"[warn] expected DeprecationWarning, got: {r.stderr.strip()[:200]}")


def test_warning_fires_exactly_once(failures):
    r = _run(
        "import warnings\n"
        "with warnings.catch_warnings(record=True) as w:\n"
        "    warnings.simplefilter('always')\n"
        "    import harness\n"
        "dep = [x for x in w if issubclass(x.category, DeprecationWarning)]\n"
        "print(len(dep))\n"
    )
    out = r.stdout.strip()
    if r.returncode != 0:
        failures.append(f"[once] import failed: {r.stderr.strip()[:200]}")
    elif out != "1":
        failures.append(f"[once] expected exactly 1 DeprecationWarning, got {out!r}")


def test_version_alias(failures):
    r = _run(
        "import warnings; warnings.simplefilter('ignore')\n"
        "import harness, agents_never_sleep\n"
        "assert harness.__version__ == agents_never_sleep.__version__, 'version mismatch'\n"
        "print('ok')\n"
    )
    if r.returncode != 0 or r.stdout.strip() != "ok":
        failures.append(f"[version] alias broken: {(r.stderr or r.stdout).strip()[:200]}")


def test_submodules_resolve(failures):
    r = _run(
        "import warnings; warnings.simplefilter('ignore')\n"
        "from harness.launcher import main as lm\n"
        "from harness.run import main as rm\n"
        "import harness.enforce  # the platform-hook entry the adapters call\n"
        "assert callable(lm) and callable(rm)\n"
        "print('ok')\n"
    )
    if r.returncode != 0 or r.stdout.strip() != "ok":
        failures.append(f"[submodules] harness.<submodule> did not resolve: {(r.stderr or r.stdout).strip()[:200]}")


def main():
    failures: list[str] = []
    test_deprecation_warning_fires(failures)
    test_warning_fires_exactly_once(failures)
    test_version_alias(failures)
    test_submodules_resolve(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — back-compat shim broken")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — `harness` shim works (one DeprecationWarning, version alias, "
          "submodules resolve to the real package)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
