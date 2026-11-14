"""Explicit one-shot fresh-identity enrollment; never accepts secrets in argv."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from dlr.worker import enrollment_v4


class _InvalidArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        # argparse normally echoes unrecognized values, which may contain a
        # mistakenly supplied secret. Only emit a fixed, non-sensitive message.
        raise _InvalidArguments


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Exchange a fresh Worker enrollment once")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--worker-identity-id", type=UUID, required=True)
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    try:
        args = parser.parse_args(argv)
    except _InvalidArguments:
        print("enrollment configuration invalid", file=sys.stderr)
        return 2
    try:
        enrollment_v4.enroll(
            base_url=args.base_url,
            worker_id=args.worker_id,
            worker_identity_id=args.worker_identity_id,
            credential_directory=args.credential_directory,
            timeout_seconds=args.request_timeout_seconds,
        )
    except enrollment_v4.EnrollmentError as error:
        code = str(error)
        if code == "enrollment_credential_exists":
            print("runtime credential already exists; no exchange was made", file=sys.stderr)
            return 3
        if code == "enrollment_configuration_invalid":
            print("enrollment configuration invalid", file=sys.stderr)
            return 2
        # Includes unknown errors: never echo arbitrary exception contents.
        print("enrollment outcome uncertain; administrator recovery required", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - exceptions can contain secrets or response bodies
        print("enrollment outcome uncertain; administrator recovery required", file=sys.stderr)
        return 1
    print("enrollment completed; runtime credential stored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
