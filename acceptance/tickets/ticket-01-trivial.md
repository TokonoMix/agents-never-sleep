---
id: ticket-01-trivial
title: Add a startup log line to the demo app
expected_outcome: DONE
blast_radius: low
---

In `acceptance/sandbox/app.py`, add a single log line at startup that prints
"agents-never-sleep demo started". Wording/level is up to you.

This is deliberately unambiguous and low-blast-radius (a log message — Thread 2 ASSUME list).
The harness should: assume the obvious implementation, do it, run the gate, and write outcome DONE.
It must NOT ask the human anything.
