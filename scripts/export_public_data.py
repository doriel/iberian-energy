"""Write the dashboard data file from the local lakehouse build.

The work is in iberian.publish.dashboard, which the daily Job's notebook imports
too. This is the command line around it, and the only difference between the two
callers is where the tables are read from.

    python scripts/export_public_data.py
    python scripts/export_public_data.py --out app/public/data.json

Running this by hand stays supported on purpose. The Job publishes the same file
every afternoon, but being able to rebuild it from a laptop is what makes the
published numbers checkable by someone who does not have the workspace.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.publish.dashboard import (  # noqa: E402
    LocalFiles,
    build,
    serialise,
    summarise,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/lakehouse")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--evaluation", default="evaluation")
    parser.add_argument("--out", default="app/public/data.json")
    args = parser.parse_args()

    payload = build(
        LocalFiles(Path(args.root), Path(args.raw_dir)), Path(args.evaluation)
    )
    body = serialise(payload)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)

    print(f"{out}  {len(body) / 1024:.0f} KB")
    for line in summarise(payload):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())