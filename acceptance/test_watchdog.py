#!/usr/bin/env python3
"""Watchdog test — proves the heartbeat sidecar that catches a HANG (the gap the Stop-hook can't see).

The Stop-hook blocks a premature stop but cannot tell a hung run from a working one. The watchdog
runs the unattended command as a child, polls its heartbeat, restarts it resumable when the heartbeat
goes stale, and on exhausted restarts runs an alert and exits 75 (claude-run's "gave up" convention).
This proves all three hermetically with bounded fake children — NO live claude-run is touched (the
watchdog is a sidecar by design; integrating with claude-run is opt-in composition, never a rewrite).

Exit 0 = GREEN.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness import watchdog  # noqa: E402

PY = sys.executable


def _hb(path, ts):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"ts": ts, "n": 1, "ticket": "", "phase": ""}, fh)


def test_child_exits_clean(failures, tmp):
    """A child that finishes on its own → watchdog returns the child's exit code, no restart."""
    hb = os.path.join(tmp, "hb1.json")
    rc = watchdog.main(["--heartbeat", hb, "--stale", "30", "--poll", "1", "--grace", "0",
                        "--max-restarts", "1", "--", PY, "-c", "import sys; sys.exit(0)"])
    if rc != 0:
        failures.append(f"[clean-exit] expected child rc 0 passthrough, got {rc}")


def test_fresh_heartbeat_no_restart(failures, tmp):
    """A child whose heartbeat stays fresh must NOT be restarted — it runs to its own clean exit."""
    hb = os.path.join(tmp, "hb2.json")
    _hb(hb, time.time())  # fresh; stale=30 >> child lifetime so age never exceeds it
    rc = watchdog.main(["--heartbeat", hb, "--stale", "30", "--poll", "1", "--grace", "0",
                        "--max-restarts", "1", "--", PY, "-c", "import time; time.sleep(2)"])
    if rc != 0:
        failures.append(f"[fresh-hb] healthy child should exit 0 (not be restarted), got {rc}")


def test_stale_restart_then_alert(failures, tmp):
    """A child that hangs and never beats → stale → restart up to the cap → alert + exit 75."""
    hb = os.path.join(tmp, "hb3.json")  # never created → age is None → stale
    marker = os.path.join(tmp, "alerted.flag")
    start = time.time()
    rc = watchdog.main(["--heartbeat", hb, "--stale", "1", "--poll", "1", "--grace", "0",
                        "--max-restarts", "1", "--alert", f"touch {marker}",
                        "--", PY, "-c", "import time; time.sleep(60)"])
    if rc != 75:
        failures.append(f"[stale] exhausted restarts should exit 75, got {rc}")
    if not os.path.exists(marker):
        failures.append("[stale] alert command did not run on exhaustion")
    if time.time() - start > 40:
        failures.append("[stale] took too long — children may not be terminating on restart")


def main() -> int:
    failures = []
    tmp = tempfile.mkdtemp(prefix="ue-wd-")
    test_child_exits_clean(failures, tmp)
    test_fresh_heartbeat_no_restart(failures, tmp)
    test_stale_restart_then_alert(failures, tmp)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — watchdog not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — clean-exit passthrough, fresh-heartbeat no-restart, and "
          "stale→restart→alert→exit-75 all hold (sidecar; claude-run untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
