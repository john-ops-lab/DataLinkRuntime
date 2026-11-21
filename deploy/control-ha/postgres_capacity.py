"""Capacity reader for an externally managed synchronous PostgreSQL deployment."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID

_SIZE = 16 * 1024
_NODE = re.compile(r"[a-z][a-z0-9_]{0,30}")
_SYNC = re.compile(r"FIRST\s+(\d+)\s*\(([^()]+)\)", re.IGNORECASE)
_MAX_BYTES = 2**63 - 1

# Observe via this host's Unix socket, in the maintenance database. Both the
# instance identifier and the data/WAL paths come from the local server.
_SQL = """
WITH target AS (
  SELECT d.oid AS database_oid, r.oid AS role_oid
  FROM pg_catalog.pg_database d, pg_catalog.pg_roles r
  WHERE d.datname = :'runtime_database' AND r.rolname = :'runtime_role'
)
SELECT json_build_object(
  'node', pg_catalog.current_setting('dlr_capacity.node_id', true),
  'system_identifier', (pg_catalog.pg_control_system()).system_identifier::text,
  'data_directory', pg_catalog.current_setting('data_directory'),
  'standby', pg_catalog.pg_is_in_recovery(),
  'synchronous_commit', pg_catalog.current_setting('synchronous_commit'),
  'runtime_settings_safe', NOT EXISTS (
     SELECT FROM pg_catalog.pg_db_role_setting s, LATERAL unnest(s.setconfig) AS c(setting)
     WHERE s.setdatabase IN (0, target.database_oid) AND s.setrole IN (0, target.role_oid)
       AND split_part(c.setting, '=', 1) = 'synchronous_commit'
       AND split_part(c.setting, '=', 2) <> 'remote_apply'
  ),
  'synchronous_standby_names', pg_catalog.current_setting('synchronous_standby_names'),
  'persistent_wal', pg_catalog.current_setting('fsync') = 'on'
    AND pg_catalog.current_setting('full_page_writes') = 'on',
  'timeline', CASE WHEN pg_catalog.pg_is_in_recovery() THEN (
     SELECT received_tli FROM pg_catalog.pg_stat_wal_receiver
     WHERE status = 'streaming' AND NOT pg_catalog.pg_is_wal_replay_paused()
  ) ELSE (
     ('x' || left(pg_catalog.pg_walfile_name(pg_catalog.pg_current_wal_insert_lsn()), 8))
     ::bit(32)::bigint
  ) END,
  'synchronous_peers', ARRAY(
     SELECT application_name FROM pg_catalog.pg_stat_replication
     WHERE state = 'streaming' AND sync_state = 'sync' AND replay_lsn IS NOT NULL
     ORDER BY application_name
  )
) FROM target
WHERE pg_catalog.current_database() = 'postgres' AND pg_catalog.inet_server_addr() IS NULL
"""


def read_object(path: Path) -> dict:
    with path.open("rb") as stream:
        body = stream.read(_SIZE + 1)
    if len(body) > _SIZE:
        raise ValueError("Capacity record exceeds size limit")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise TypeError("Invalid capacity record")
    return value


def free_bytes(path: Path) -> int:
    stats = os.statvfs(path)
    if stats.f_flag & os.ST_RDONLY:
        raise ValueError("PostgreSQL storage is read-only")
    return stats.f_bavail * stats.f_frsize


def observe(args: argparse.Namespace) -> dict:
    observed_at = datetime.now(UTC)
    result = subprocess.run(
        [
            "psql",
            "-XqAtw",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            "postgres",
            "-v",
            f"runtime_database={args.database}",
            "-v",
            f"runtime_role={args.role}",
            "-f",
            "-",
        ],
        env={
            **os.environ,
            "PGCONNECT_TIMEOUT": "2",
            "PGOPTIONS": "-c statement_timeout=1200 -c default_transaction_read_only=on",
        },
        input=_SQL.encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=2.5,
    )
    row = json.loads(result.stdout)
    data = Path(row.pop("data_directory"))
    if not data.is_absolute() or not _NODE.fullmatch(row["node"]):
        raise ValueError("Invalid local PostgreSQL capacity identity")
    return {
        "activation_id": str(args.activation_id),
        "runtime_database": args.database,
        "runtime_role": args.role,
        "observed_at": observed_at.isoformat(),
        "data_free_bytes": free_bytes(data),
        "wal_free_bytes": free_bytes((data / "pg_wal").resolve(strict=True)),
        **row,
    }


def capacity(topology: Path, batch: Path) -> dict:
    declared = read_object(topology)
    now = datetime.now(UTC)
    cluster = str(UUID(declared["cluster_id"]))
    domain = declared["storage_domain_id"]
    epoch, timeline = declared["topology_epoch"], declared["timeline"]
    identifier = declared["system_identifier"]
    activation_id = str(UUID(declared["activation_id"]))
    primary, standbys = declared["primary"], declared["promotable"]
    if (
        not isinstance(domain, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", domain)
        or type(epoch) is not int
        or not 0 < epoch <= _MAX_BYTES
        or type(timeline) is not int
        or not 0 < timeline <= 2**32 - 1
        or not isinstance(identifier, str)
        or not re.fullmatch(r"[1-9]\d{0,19}", identifier, re.ASCII)
        or not isinstance(standbys, list)
        or not 1 <= len(standbys) <= 8
    ):
        raise ValueError("Invalid capacity topology")
    members = [primary, *standbys]
    if not all(isinstance(node, str) and _NODE.fullmatch(node) for node in members):
        raise ValueError("Invalid capacity member name")

    samples = read_object(batch)["samples"]
    if not isinstance(samples, list) or len(samples) != len(members):
        raise ValueError("Incomplete PostgreSQL capacity batch")
    by_node = {row["node"]: row for row in samples}
    if len(by_node) != len(members) or set(by_node) != set(members):
        raise ValueError("Duplicate or missing capacity member")

    oldest = datetime.max.replace(tzinfo=UTC)
    writable = _MAX_BYTES
    for node in members:
        row = by_node[node]
        measured = datetime.fromisoformat(row["observed_at"])
        if (
            row["activation_id"] != activation_id
            or row["runtime_database"] != declared["runtime_database"]
            or row["runtime_role"] != declared["runtime_role"]
            or measured.tzinfo is None
            or not -timedelta(seconds=5) <= now - measured <= timedelta(seconds=10)
            or row["system_identifier"] != identifier
            or type(row["timeline"]) is not int
            or row["timeline"] != timeline
            or row["persistent_wal"] is not True
            or row["standby"] is not (node != primary)
        ):
            raise ValueError("PostgreSQL member observation unavailable")
        oldest = min(oldest, measured)
        for key in ("data_free_bytes", "wal_free_bytes"):
            available = row[key]
            if type(available) is not int or not 0 <= available <= _MAX_BYTES:
                raise ValueError("Invalid PostgreSQL filesystem capacity")
            writable = min(writable, available)
        if node == primary:
            sync = _SYNC.fullmatch(row["synchronous_standby_names"].strip())
            if (
                row["synchronous_commit"] != "remote_apply"
                or row["runtime_settings_safe"] is not True
                or not sync
                or int(sync[1]) != len(standbys)
                or sorted(name.strip() for name in sync[2].split(","))
                != sorted(standbys)
                or sorted(row["synchronous_peers"]) != sorted(standbys)
            ):
                raise ValueError("PostgreSQL synchronous candidates unavailable")

    return {
        "schema_version": 1,
        "cluster_id": cluster,
        "storage_domain_id": domain,
        "topology_epoch": epoch,
        "observed_at": oldest.isoformat(),
        "writable_free_bytes": writable,
    }


def _request_deadline(_signum, _frame) -> None:
    raise TimeoutError("Capacity request deadline exceeded")


class CapacityServer(HTTPServer):
    def process_request(self, request, client_address):
        request.settimeout(1.0)
        try:
            signal.setitimer(signal.ITIMER_REAL, 1.0)
            with self.context.wrap_socket(request, server_side=True) as connection:
                self.finish_request(connection, client_address)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            self.shutdown_request(request)

    def handle_error(self, request, client_address):
        # Private paths, credentials and internal observation errors stay local.
        pass


def serve(args: argparse.Namespace) -> None:
    with args.token_file.open("rb") as stream:
        token = stream.read(4097).strip(b"\n")
    if not 0 < len(token) <= 4096 or not all(33 <= b <= 126 for b in token):
        raise ValueError("Invalid provider token")
    authorization = b"Bearer " + token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path != "/capacity":
                self.send_error(404)
                return
            headers = self.headers.get_all("Authorization") or []
            if len(headers) != 1 or not hmac.compare_digest(
                headers[0].encode("latin-1"), authorization
            ):
                self.send_error(401)
                return
            try:
                payload = json.dumps(capacity(args.topology, args.batch)).encode()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                OverflowError,
            ):
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with CapacityServer((args.address, args.port), Handler) as server:
        server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.context.minimum_version = ssl.TLSVersion.TLSv1_2
        server.context.load_cert_chain(args.certificate, args.private_key)
        signal.signal(signal.SIGALRM, _request_deadline)
        server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("observe")
    probe.add_argument("--activation-id", type=UUID, required=True)
    probe.add_argument("--database", required=True)
    probe.add_argument("--role", required=True)
    check = commands.add_parser("check")
    http = commands.add_parser("serve")
    for command in (check, http):
        command.add_argument("topology", type=Path)
        command.add_argument("batch", type=Path)
    http.add_argument("--address", required=True)
    http.add_argument("--port", required=True, type=int)
    http.add_argument("--token-file", required=True, type=Path)
    http.add_argument("--certificate", required=True, type=Path)
    http.add_argument("--private-key", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            serve(args)
        else:
            result = (
                observe(args)
                if args.command == "observe"
                else capacity(args.topology, args.batch)
            )
            print(json.dumps(result))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        subprocess.SubprocessError,
    ):
        parser.exit(1, "PostgreSQL capacity unavailable\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
