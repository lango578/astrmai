"""Fixture for tests/test_job_guard.py: arm the job guard, spawn a leaf, then wait to be hard-killed.

Run only through the test. Writes {"mid", "leaf", "armed"} to the PID file given as argv[1].
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))

from astrmai.jobguard import JobGuard, arm  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1])
    # ASTRMAI_JOB_ARM=0 gives the negative control: same fixture, no guard, children must survive.
    guard = arm() if os.environ.get("ASTRMAI_JOB_ARM", "1") == "1" else JobGuard()

    leaf = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    payload = {
        "mid": os.getpid(),
        "leaf": leaf.pid,
        "armed": guard.mode,
        "detail": guard.detail,
        "inherited": guard.is_member(leaf.pid),
    }
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(out)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
