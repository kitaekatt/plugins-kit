"""A stand-in for ``llm-kit jobs run`` that keeps a REAL llm-kit ledger.

Usage: ``real_ledger_job.py <job-file> <store> <run-id>``. It creates the ledger through llm-kit's own
JobStore, leaves one job running with an armed reservation (exactly what a killed `jobs run` leaves
behind), starts a grandchild, then hangs. No model is called. The dashboard's runner, scheduler and
the REAL ``llm-kit jobs cancel`` verb are then the only things between this process and a cancelled ledger.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from llm_kit.jobs.model import Contract, Job, Prompt
from llm_kit.jobs.store import JobStore

JOB_FILE, STORE, RUN_ID = sys.argv[1:4]
RUN_DIR = Path(JOB_FILE).parent

store = JobStore(STORE)
job = Job(id="item", prompt=Prompt(user="x"), models=("fake",), directory=RUN_DIR, max_attempts=1,
          contract=Contract(command=(sys.executable, "-c", "pass"), directory=RUN_DIR))
store.create_run(RUN_ID, [job])
# Test support only (no production code reads it): a pause between the ledger's first and later writes lets a
# test hold the window in which the ledger is still changing open for as long as it likes.
time.sleep(float(os.environ.get("TASK_KIT_TEST_LEDGER_WRITE_DELAY_S", "0")))
reservation = store.reserve_attempt(RUN_ID, "item", endpoint="e", backend="b", model="m", reserved_at="2026-09-01T00:00:00Z")
store.arm_reservation(RUN_ID, "item", reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z")
# The marker is written only after the LAST ledger write, atomically, so a test can wait on a causal observable
# instead of a poll count or a timeout.
marker_temp = RUN_DIR / "ledger-ready.tmp"
marker_temp.write_text("ready", encoding="utf-8")
os.replace(marker_temp, RUN_DIR / "ledger-ready")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
(RUN_DIR / "grandchild.pid").write_text(str(child.pid), encoding="utf-8")
time.sleep(600)
