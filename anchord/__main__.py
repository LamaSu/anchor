"""`python -m anchord` entry point (equivalent to the `anchord` console script)."""
from __future__ import annotations

import argparse
import sys

from .server import DEFAULT_HOST, DEFAULT_PORT, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anchord", description="anchor daemon")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        serve(host=args.host, port=args.port)
    except OSError as e:
        print(f"anchord: bind failed on {args.host}:{args.port} — {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
