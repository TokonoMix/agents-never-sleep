"""Back-compat shim for the old import name ``harness``.

The package was renamed ``harness`` → ``agents_never_sleep`` for 1.0 (a generic top-level
name is collision-prone once pip-installed). This shim keeps every pre-1.0 recipe working —
``import harness``, ``from harness.launcher import main``, ``python3 -m harness.run`` /
``-m harness.enforce`` — for the whole 1.x series. **It is removed in 2.0.**

How it works: we point this package's ``__path__`` at the real package's directory, so any
``harness.<submodule>`` import resolves to the real module file in ``agents_never_sleep/`` and
runs the real code (its own internal imports all reference ``agents_never_sleep``, so there is
no split-brain state). A single ``DeprecationWarning`` fires the first time ``harness`` is
imported; it is silent under normal use because CPython suppresses DeprecationWarning outside
``__main__``/test runners by default — a gentle nudge, not noise.
"""
from __future__ import annotations

import warnings as _warnings

import agents_never_sleep as _real

# Resolve `harness.<submodule>` against the real package's directory.
__path__ = list(_real.__path__)
__version__ = _real.__version__
__all__ = list(getattr(_real, "__all__", []))

_warnings.warn(
    "The 'harness' import name is deprecated; use 'agents_never_sleep'. "
    "The compatibility shim is removed in 2.0.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export the real package's public surface so `from harness import X` keeps working.
from agents_never_sleep import *  # noqa: E402,F401,F403
