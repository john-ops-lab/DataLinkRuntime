"""Install an administrator-staged recovery without exposing its secret in argv."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from dlr.worker import runtime_credential_v4


class _InvalidArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise _InvalidArguments


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Install a staged Worker recovery credential")
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--worker-identity-id", type=UUID, required=True)
    parser.add_argument("--credential-directory", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        runtime_credential_v4.install_recovered_credential(
            args.credential_directory,
            worker_id=args.worker_id,
            worker_identity_id=args.worker_identity_id,
        )
    except Exception:  # noqa: BLE001 - errors may contain secret input or paths
        print("credential recovery unavailable or uncertain", file=sys.stderr)
        return 1
    print("recovery credential installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
