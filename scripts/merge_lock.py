#!/usr/bin/env python3
"""One writer merges into main at a time.

Wrap a merge with this so two concurrent agents in the same or sibling
checkouts can't interleave `git merge`/`git push origin main`:

    .venv/bin/python scripts/merge_lock.py -- git merge --no-ff agent/x -m "..." && git push origin main

Holds an exclusive fcntl lock on .git/merge.lock for the duration of the
wrapped command; a second call blocks until the first releases it (or exits
non-zero after --timeout seconds if given). Same mechanism as the data-root
lock in app/api.py:_lock_data_root.
"""

import argparse
import fcntl
import subprocess
import sys
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--timeout", type=float, default=None, help="seconds to wait for the lock before giving up")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- command to run while holding the lock")
    args = p.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        p.error("pass a command to run, e.g. -- git merge --no-ff agent/x")

    git_dir = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=True)
    lock_path = Path(git_dir.stdout.strip()) / "merge.lock"
    lock_path.touch(exist_ok=True)

    with open(lock_path, "w") as f:
        deadline = time.monotonic() + args.timeout if args.timeout else None
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline and time.monotonic() > deadline:
                    print(f"merge_lock: timed out waiting for {lock_path}", file=sys.stderr)
                    return 1
                time.sleep(0.5)
        return subprocess.run(command).returncode


if __name__ == "__main__":
    sys.exit(main())
