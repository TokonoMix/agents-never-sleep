#!/usr/bin/env python3
"""Drift guard — the wheel-packaged `agents_never_sleep/hooks/` copy must stay BYTE-IDENTICAL to
the repo-root `hooks/` tree it's copied from (same file set, same contents), so `pyproject.toml`'s
package-data can never silently ship a stale/divergent copy to a `pip install`. The repo-root
`hooks/` tree remains the sole canonical/editable source; `agents_never_sleep/hooks/` is a
committed, read-only mirror used only as the wheel's package-data payload and the fallback source
for a pip/wheel install with no repo-root `hooks/` next to it (see `init_cmd._hooks_root`).

Exit 0 = GREEN.
"""
import filecmp
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)

SOURCE_HOOKS = os.path.join(REPO_ROOT, "hooks")
PACKAGED_HOOKS = os.path.join(REPO_ROOT, "agents_never_sleep", "hooks")


def _walk_files(root):
    """Relative POSIX-style paths of every regular file under `root`."""
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.add(rel)
    return out


def test_packaged_hooks_dir_exists():
    assert os.path.isdir(PACKAGED_HOOKS), (
        f"{PACKAGED_HOOKS} is missing — the wheel-packaged copy of hooks/ must be committed so "
        f"package-data can ship it (see pyproject.toml [tool.setuptools.package-data])."
    )


def test_packaged_hooks_file_set_matches_source():
    source_files = _walk_files(SOURCE_HOOKS)
    packaged_files = _walk_files(PACKAGED_HOOKS) if os.path.isdir(PACKAGED_HOOKS) else set()
    missing = source_files - packaged_files
    extra = packaged_files - source_files
    assert not missing, f"packaged copy is missing files present in repo-root hooks/: {sorted(missing)}"
    assert not extra, f"packaged copy has extra files not in repo-root hooks/: {sorted(extra)}"


def test_packaged_hooks_bytes_match_source():
    source_files = _walk_files(SOURCE_HOOKS)
    mismatches = []
    for rel in sorted(source_files):
        src = os.path.join(SOURCE_HOOKS, rel)
        pkg = os.path.join(PACKAGED_HOOKS, rel)
        if not os.path.isfile(pkg):
            continue  # already reported loudly by test_packaged_hooks_file_set_matches_source
        if not filecmp.cmp(src, pkg, shallow=False):
            mismatches.append(rel)
    assert not mismatches, f"packaged copy differs in content from repo-root hooks/ for: {mismatches}"


def _run():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print("=" * 60)
    if fails:
        print(f"RESULT: ❌ RED — {fails} drift-guard check(s) failed")
    else:
        print("RESULT: ✅ GREEN — packaged agents_never_sleep/hooks/ is byte-identical to repo-root hooks/")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    _run()
