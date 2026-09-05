"""Direct boundary tests for the Issue #132 tabular reader recipes."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import pytest

from dlr.runtime.java_runtime import SOURCE as JAVA_RUNTIME_SOURCE

CATALOG_ROOT = Path(__file__).parents[1] / "src/dlr/control/template_catalog"


@pytest.mark.parametrize(
    ("slug", "module_name", "secret_name", "dsn"),
    [
        ("postgresql-readonly-snapshot", "psycopg", "POSTGRES_DSN", "postgresql://fixture"),
        ("mysql-readonly-snapshot", "pymysql", "MYSQL_DSN", "mysql://user@host/db"),
    ],
)
def test_python_database_cells_are_json_safe_and_batches_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    slug: str,
    module_name: str,
    secret_name: str,
    dsn: str,
) -> None:
    columns = ["amount", "at", "day", "id", "payload", "nested"]
    records = [
        (
            Decimal("1234567890.0000000001"),
            datetime(2026, 9, 5, 1, 2, 3),
            date(2026, 9, 5),
            UUID("12345678-1234-5678-1234-567812345678"),
            b"\x00\xff",
            {"amount": Decimal("0.10")},
        )
    ]

    class Cursor:
        def __init__(self, labels: list[str]) -> None:
            self.description = [(label,) for label in labels]
            self.at = 0
            self.fetch_sizes: list[int] = []

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            self.at = 0

        def fetchmany(self, size: int) -> list[tuple[object, ...]]:
            self.fetch_sizes.append(size)
            batch = records[self.at : self.at + size]
            self.at += len(batch)
            return batch

    class Connection:
        def __init__(self, labels: list[str]) -> None:
            self.labels = labels
            self.cursors: list[Cursor] = []

        def execute(self, *_args: object) -> None:
            return None

        def cursor(self, *_args: object, **_kwargs: object) -> Cursor:
            cursor = Cursor(self.labels)
            self.cursors.append(cursor)
            return cursor

        def begin(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    module = ModuleType(module_name)
    if module_name == "pymysql":
        cursors = ModuleType("pymysql.cursors")
        cursors.SSCursor = object()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors)
    monkeypatch.setitem(sys.modules, module_name, module)
    namespace = runpy.run_path(str(CATALOG_ROOT / f"variants/{slug}/python.py"))
    connections: list[Connection] = []

    def connect(*_args: object, **_kwargs: object) -> Connection:
        connection = Connection(columns)
        connections.append(connection)
        return connection

    module.connect = connect  # type: ignore[attr-defined]
    result = namespace["handle"](
        SimpleNamespace(secrets={secret_name: dsn}),
        {"sql": "SELECT cells FROM fixture", "batch_size": 1, "max_rows": 2},
    )
    assert result == {
        "rows": [
            {
                "amount": "1234567890.0000000001",
                "at": "2026-09-05T01:02:03",
                "day": "2026-09-05",
                "id": "12345678-1234-5678-1234-567812345678",
                "payload": {"$binary_base64": "AP8="},
                "nested": {"amount": "0.10"},
            }
        ],
        "count": 1,
        "partial": False,
        "checkpoint": None,
    }
    assert all(size <= 1 for cursor in connections[-1].cursors for size in cursor.fetch_sizes)

    module.connect = lambda *_args, **_kwargs: Connection(["duplicate", "duplicate"])
    duplicate = namespace["handle"](
        SimpleNamespace(secrets={secret_name: dsn}),
        {"sql": "SELECT duplicate, duplicate FROM fixture"},
    )
    assert duplicate == {
        "rows": [],
        "count": 0,
        "partial": True,
        "error": "database_query_failed",
    }


def _run_node_fixture(tmp_path: Path, source: Path, packages: dict[str, str], script: str) -> dict:
    fixture = tmp_path / source.parent.name
    fixture.mkdir(parents=True)
    (fixture / "recipe.mjs").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    for relative, content in packages.items():
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=fixture,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_postgresql_javascript_uses_parameterized_bounded_cursor(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/postgresql-readonly-snapshot/javascript.mjs"
    package = json.dumps({"type": "module", "exports": "./index.js"})
    driver = r"""
class Client {
  constructor() {
    this.at = 0;
    this.audit = { fetches: [], declare: null };
    globalThis.audit.push(this.audit);
  }
  async connect() {}
  async query(request) {
    const text = typeof request === "string" ? request : request.text;
    if (/^DECLARE /.test(text)) {
      this.at = 0;
      this.audit.declare = { text, values: request.values };
      return { rows: [], fields: [] };
    }
    if (/^FETCH /.test(text)) {
      const count = Number(text.match(/^FETCH FORWARD (\d+)/)[1]);
      this.audit.fetches.push(count);
      const rows = globalThis.fixtureRows.slice(this.at, this.at + count);
      this.at += rows.length;
      return { rows, fields: globalThis.fixtureFields };
    }
    return { rows: [], fields: [] };
  }
  async end() {}
}
export default { Client };
"""
    script = r"""
import { handle } from "./recipe.mjs";
globalThis.audit = [];
globalThis.fixtureFields = [{ name: "value" }];
const context = { secrets: new Map([["POSTGRES_DSN", "postgresql://fixture"]]) };
globalThis.fixtureRows = [["a"], ["b"]];
const exact = await handle(context, {
  sql: "SELECT value FROM fixture WHERE id = $1", params: [7], max_rows: 2, batch_size: 1,
});
globalThis.fixtureRows = [["a"], ["b"], ["c"]];
const overflow = await handle(
  context, { sql: "SELECT value FROM fixture", max_rows: 2, batch_size: 1 },
);
globalThis.fixtureFields = [
  { name: "amount" }, { name: "at" }, { name: "payload" }, { name: "nested" },
];
globalThis.fixtureRows = [[
  12345678901234567890n, new Date("2026-09-05T01:02:03Z"),
  Buffer.from([0, 255]), { n: 9n },
]];
const normalized = await handle(
  context, { sql: "SELECT cells FROM fixture", max_rows: 2, batch_size: 1 },
);
process.stdout.write(JSON.stringify({ exact, overflow, normalized, audit: globalThis.audit }));
"""
    output = _run_node_fixture(
        tmp_path,
        source,
        {
            "node_modules/pg/package.json": package,
            "node_modules/pg/index.js": driver,
        },
        script,
    )
    assert output["exact"]["rows"] == [{"value": "a"}, {"value": "b"}]
    assert output["exact"]["partial"] is False
    assert output["overflow"]["partial"] is True
    assert output["normalized"]["rows"] == [
        {
            "amount": "12345678901234567890",
            "at": "2026-09-05T01:02:03.000+00:00",
            "payload": {"$binary_base64": "AP8="},
            "nested": {"n": "9"},
        }
    ]
    assert output["audit"][0]["declare"]["values"] == [7]
    assert "DECLARE dlr_snapshot_cursor NO SCROLL CURSOR" in output["audit"][0]["declare"]["text"]
    assert all(size <= 1 for audit in output["audit"] for size in audit["fetches"])


def test_mysql_javascript_streams_callback_rows_in_real_batches(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/mysql-readonly-snapshot/javascript.mjs"
    package = json.dumps({"type": "module", "exports": "./index.js"})
    driver = r"""
import { EventEmitter } from "node:events";
import { Readable } from "node:stream";
export default {
  createConnection(options) {
    const audit = { highWaterMarks: [], options, destroyed: false };
    globalThis.audit.push(audit);
    return {
      connect(callback) { queueMicrotask(() => callback(null)); },
      query(request, values, callback) {
        if (typeof request === "string") {
          const done = typeof values === "function" ? values : callback;
          queueMicrotask(() => done(null, {}));
          return undefined;
        }
        const query = new EventEmitter();
        query.stream = ({ highWaterMark }) => {
          audit.highWaterMarks.push(highWaterMark);
          query.emit("fields", globalThis.fixtureFields);
          return Readable.from(globalThis.fixtureRows, { objectMode: true, highWaterMark });
        };
        return query;
      },
      end(callback) { queueMicrotask(() => callback(null)); },
      destroy() { audit.destroyed = true; },
    };
  },
};
"""
    script = r"""
import { handle } from "./recipe.mjs";
globalThis.audit = [];
globalThis.fixtureFields = [{ name: "value" }];
const context = { secrets: new Map([["MYSQL_DSN", "mysql://user:password@host/db"]]) };
globalThis.fixtureRows = [["a"], ["b"]];
const exact = await handle(
  context, { sql: "SELECT value FROM fixture", max_rows: 2, batch_size: 1 },
);
globalThis.fixtureRows = [["a"], ["b"], ["c"]];
const overflow = await handle(
  context, { sql: "SELECT value FROM fixture", max_rows: 2, batch_size: 1 },
);
globalThis.fixtureFields = [{ name: "amount" }, { name: "at" }, { name: "payload" }];
globalThis.fixtureRows = [[
  12345678901234567890n, new Date("2026-09-05T01:02:03Z"), Buffer.from([0, 255]),
]];
const normalized = await handle(context, { sql: "SELECT cells FROM fixture", batch_size: 1 });
let invalidDsn;
try {
  await handle(
    { secrets: new Map([["MYSQL_DSN", "mysql://user:%E0%A4%A@host/db"]]) },
    { sql: "SELECT 1" },
  );
} catch (error) { invalidDsn = error.message; }
process.stdout.write(JSON.stringify({
  exact, overflow, normalized, invalidDsn, audit: globalThis.audit,
}));
"""
    output = _run_node_fixture(
        tmp_path,
        source,
        {
            "node_modules/mysql2/package.json": package,
            "node_modules/mysql2/index.js": driver,
        },
        script,
    )
    assert output["exact"]["rows"] == [{"value": "a"}, {"value": "b"}]
    assert output["exact"]["partial"] is False
    assert output["overflow"]["partial"] is True
    assert output["normalized"]["rows"] == [
        {
            "amount": "12345678901234567890",
            "at": "2026-09-05T01:02:03.000+00:00",
            "payload": {"$binary_base64": "AP8="},
        }
    ]
    assert output["invalidDsn"] == "invalid_mysql_dsn"
    assert all(value == 1 for audit in output["audit"] for value in audit["highWaterMarks"])


def test_python_excel_exact_probe_checkpoint_headers_dates_and_range_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = CATALOG_ROOT / "variants/excel-to-json/python.py"
    namespace = runpy.run_path(str(source))
    workbook_path = tmp_path / "fixture.xlsx"
    workbook_path.write_bytes(b"")
    matrix = [
        ["name", "at"],
        ["a", datetime(2026, 9, 5, 1, 2, 3)],
        ["b", datetime(2026, 9, 5, 4, 5, 6)],
    ]

    class Cell:
        def __init__(self, value: object) -> None:
            self.value = value
            self.data_type = "n"

    class Sheet:
        @property
        def max_row(self) -> int:
            return len(matrix)

        @property
        def max_column(self) -> int:
            return max(len(row) for row in matrix)

        def iter_rows(self, *, min_row: int, min_col: int, max_row: int, max_col: int) -> object:
            for row_at in range(min_row - 1, max_row):
                yield tuple(
                    Cell(matrix[row_at][column] if column < len(matrix[row_at]) else None)
                    for column in range(min_col - 1, max_col)
                )

    class Workbook:
        sheetnames = ["Sheet1"]

        def __getitem__(self, _name: str) -> Sheet:
            return Sheet()

        def close(self) -> None:
            return None

    openpyxl = ModuleType("openpyxl")
    openpyxl.load_workbook = lambda *_args, **_kwargs: Workbook()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openpyxl", openpyxl)
    namespace["handle"].__globals__["_inspect_xlsx"] = lambda _path: None
    context = SimpleNamespace(
        input_files=[
            SimpleNamespace(
                path=workbook_path,
                original_name="fixture.xlsx",
                size_bytes=workbook_path.stat().st_size,
            )
        ]
    )
    exact = namespace["handle"](context, {"max_rows": 2})
    assert exact["partial"] is False
    assert exact["rows"][0]["at"] == "2026-09-05T01:02:03.000"

    matrix.append(["c", datetime(2026, 9, 5, 7, 8, 9)])
    overflow = namespace["handle"](context, {"max_rows": 2})
    assert overflow["rows"] == exact["rows"]
    assert overflow["checkpoint"] == {"reason": "row_limit", "next_row": 4}

    bounded = namespace["handle"](
        context,
        {"range": "A2:B4", "header": False, "max_rows": 1, "max_columns": 1},
    )
    assert bounded["rows"] == [["a"]]
    assert bounded["checkpoint"] == {
        "reason": "multiple_limits",
        "limits": ["row_limit", "column_limit"],
        "next_row": 3,
        "next_column": 2,
    }

    matrix[0][1] = "name"
    with pytest.raises(ValueError, match="^invalid_or_duplicate_header$"):
        namespace["handle"](context, {})
    for invalid_range in ("A1:XFE2", "A1:A1048577"):
        with pytest.raises(ValueError, match="^invalid_range$"):
            namespace["handle"](context, {"range": invalid_range})

    class LegacyWorkbook:
        released = False

        def sheet_names(self) -> list[str]:
            return ["Sheet1"]

        def release_resources(self) -> None:
            self.released = True

    legacy_workbook = LegacyWorkbook()
    xlrd = ModuleType("xlrd")
    xlrd.open_workbook = lambda *_args, **_kwargs: legacy_workbook  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "xlrd", xlrd)
    with pytest.raises(ValueError, match="^sheet_not_found$"):
        namespace["_xls"](workbook_path, {"sheet": "Missing"}, 10, 10)
    assert legacy_workbook.released is True


def test_javascript_excel_exact_probe_checkpoint_dates_and_range_bounds(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/excel-to-json/javascript.mjs"
    package = json.dumps({"type": "module", "exports": "./index.js"})
    xlsx = r"""
export function read() { return globalThis.workbook; }
const column = (text) => {
  let value = 0;
  for (const character of text.toUpperCase()) value = value * 26 + character.charCodeAt(0) - 64;
  return value - 1;
};
export const utils = {
  decode_range(value) {
    const match = /^([A-Z]+)(\d+):([A-Z]+)(\d+)$/i.exec(value);
    if (!match) throw new Error("bad range");
    return {
      s: { c: column(match[1]), r: Number(match[2]) - 1 },
      e: { c: column(match[3]), r: Number(match[4]) - 1 },
    };
  },
  encode_cell({ r, c }) {
    let name = "";
    for (let value = c + 1; value > 0; value = Math.floor((value - 1) / 26)) {
      name = String.fromCharCode(65 + ((value - 1) % 26)) + name;
    }
    return `${name}${r + 1}`;
  },
};
"""
    script = r"""
import { handle } from "./recipe.mjs";
const sheet = {
  "!ref": "A1:B3",
  A1: { v: "name" }, B1: { v: "at" },
  A2: { v: "a" }, B2: { v: new Date("2026-09-05T01:02:03Z") },
  A3: { v: "b" }, B3: { v: new Date("2026-09-05T04:05:06Z") },
};
globalThis.workbook = { SheetNames: ["Sheet1"], Sheets: { Sheet1: sheet }, files: {} };
const context = {
  inputFiles: [{ path: "./fixture.xls", originalName: "fixture.xls", sizeBytes: 0 }],
};
const exact = handle(context, { max_rows: 2 });
sheet["!ref"] = "A1:B4";
sheet.A4 = { v: "c" }; sheet.B4 = { v: new Date("2026-09-05T07:08:09Z") };
const overflow = handle(context, { max_rows: 2 });
const bounded = handle(context, { range: "A2:B4", header: false, max_rows: 1, max_columns: 1 });
sheet.B1 = { v: "name" };
let duplicate;
try { handle(context, {}); } catch (error) { duplicate = error.message; }
const invalid = [];
for (const range of ["A1:XFE2", "A1:A1048577"]) {
  try { handle(context, { range }); } catch (error) { invalid.push(error.message); }
}
process.stdout.write(JSON.stringify({ exact, overflow, bounded, duplicate, invalid }));
"""
    output = _run_node_fixture(
        tmp_path,
        source,
        {
            "node_modules/@e965/xlsx/package.json": package,
            "node_modules/@e965/xlsx/index.js": xlsx,
            "fixture.xls": "",
        },
        script,
    )
    assert output["exact"]["partial"] is False
    assert output["exact"]["rows"][0]["at"] == "2026-09-05T01:02:03.000"
    assert output["overflow"]["checkpoint"] == {"reason": "row_limit", "next_row": 4}
    assert output["bounded"]["checkpoint"] == {
        "reason": "multiple_limits",
        "limits": ["row_limit", "column_limit"],
        "next_row": 3,
        "next_column": 2,
    }
    assert output["duplicate"] == "invalid_or_duplicate_header"
    assert output["invalid"] == ["invalid_range", "invalid_range"]


def test_java_database_params_validation_and_csv_decoder_are_strict(tmp_path: Path) -> None:
    boundary_driver = r"""
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.Driver;
import java.sql.DriverManager;
import java.sql.DriverPropertyInfo;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.sql.Types;
import java.util.List;
import java.util.Properties;
import java.util.logging.Logger;

public final class BoundaryDriver implements Driver {
    static {
        try { DriverManager.registerDriver(new BoundaryDriver()); }
        catch (SQLException error) { throw new ExceptionInInitializerError(error); }
    }
    public Connection connect(String url, Properties info) {
        if (!acceptsURL(url)) return null;
        return (Connection) Proxy.newProxyInstance(
            BoundaryDriver.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "createStatement" -> statement();
                case "prepareStatement" -> prepared();
                case "getAutoCommit" -> false;
                case "isClosed", "isWrapperFor" -> false;
                case "unwrap" -> throw new SQLException("not_a_wrapper");
                default -> defaultValue(method.getReturnType());
            }
        );
    }
    private static Statement statement() {
        return (Statement) Proxy.newProxyInstance(
            BoundaryDriver.class.getClassLoader(), new Class<?>[] {Statement.class},
            (proxy, method, args) -> defaultValue(method.getReturnType())
        );
    }
    private static PreparedStatement prepared() {
        return (PreparedStatement) Proxy.newProxyInstance(
            BoundaryDriver.class.getClassLoader(), new Class<?>[] {PreparedStatement.class},
            (proxy, method, args) -> method.getName().equals("executeQuery")
                ? result() : defaultValue(method.getReturnType())
        );
    }
    private static ResultSet result() {
        List<Object> values = List.of("kept", new Object());
        int[] at = {-1};
        ResultSetMetaData metadata = (ResultSetMetaData) Proxy.newProxyInstance(
            BoundaryDriver.class.getClassLoader(), new Class<?>[] {ResultSetMetaData.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "getColumnCount" -> 1;
                case "getColumnLabel" -> "value";
                case "getColumnType" -> Types.VARCHAR;
                default -> defaultValue(method.getReturnType());
            }
        );
        return (ResultSet) Proxy.newProxyInstance(
            BoundaryDriver.class.getClassLoader(), new Class<?>[] {ResultSet.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "next" -> ++at[0] < values.size();
                case "getMetaData" -> metadata;
                case "getObject" -> values.get(at[0]);
                default -> defaultValue(method.getReturnType());
            }
        );
    }
    private static Object defaultValue(Class<?> type) {
        if (!type.isPrimitive()) return null;
        if (type == boolean.class) return false;
        if (type == byte.class) return (byte) 0;
        if (type == short.class) return (short) 0;
        if (type == int.class) return 0;
        if (type == long.class) return 0L;
        if (type == float.class) return 0F;
        if (type == double.class) return 0D;
        if (type == char.class) return '\0';
        return null;
    }
    public boolean acceptsURL(String url) {
        return url != null && url.startsWith("jdbc:boundary:");
    }
    public DriverPropertyInfo[] getPropertyInfo(String url, Properties info) {
        return new DriverPropertyInfo[0];
    }
    public int getMajorVersion() { return 1; }
    public int getMinorVersion() { return 0; }
    public boolean jdbcCompliant() { return false; }
    public Logger getParentLogger() { return Logger.getGlobal(); }
}
"""
    for slug, secret_name in (
        ("postgresql-readonly-snapshot", "POSTGRES_DSN"),
        ("mysql-readonly-snapshot", "MYSQL_DSN"),
    ):
        compile_root = tmp_path / slug
        compile_root.mkdir()
        (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
        (compile_root / "Adapter.java").write_text(
            (CATALOG_ROOT / f"variants/{slug}/java.java").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (compile_root / "BoundaryDriver.java").write_text(boundary_driver, encoding="utf-8")
        subprocess.run(
            [
                "javac",
                "-encoding",
                "UTF-8",
                "DlrRuntime.java",
                "Adapter.java",
                "BoundaryDriver.java",
            ],
            cwd=compile_root,
            check=True,
            capture_output=True,
            text=True,
        )
        workspace = compile_root / "dlr-exec-1"
        workspace.mkdir()
        (workspace / "input").mkdir()
        (workspace / "input.json").write_text(
            json.dumps({"sql": "SELECT 1", "params": {"not": "an array"}}),
            encoding="utf-8",
        )
        (workspace / "runtime_config.json").write_text("{}", encoding="utf-8")
        (workspace / "input_manifest.json").write_text(
            json.dumps({"execution_id": 1, "files": []}), encoding="utf-8"
        )
        environment = os.environ.copy()
        environment[f"DLR_SECRET_{secret_name}"] = "jdbc:unused"
        completed = subprocess.run(
            ["java", "-cp", str(compile_root), "DlrRuntime", str(workspace)],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert "params_must_be_array" in completed.stderr

        (workspace / "input.json").write_text(
            json.dumps({"sql": "SELECT value FROM fixture", "params": []}),
            encoding="utf-8",
        )
        environment[f"DLR_SECRET_{secret_name}"] = "jdbc:boundary:test"
        completed = subprocess.run(
            [
                "java",
                "-Djdbc.drivers=BoundaryDriver",
                "-cp",
                str(compile_root),
                "DlrRuntime",
                str(workspace),
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        preserved = json.loads((workspace / "output.json").read_text(encoding="utf-8"))
        assert preserved == {
            "rows": [{"value": "kept"}],
            "count": 1,
            "partial": True,
            "checkpoint": {"row_offset": 1},
        }

    csv_source = (CATALOG_ROOT / "variants/csv-to-json/java.java").read_text(encoding="utf-8")
    assert "CodingErrorAction.REPORT" in csv_source
    assert '"invalid_encoding_or_content"' in csv_source
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    (csv_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (csv_root / "Adapter.java").write_text(csv_source, encoding="utf-8")
    (csv_root / "CsvHarness.java").write_text(
        """
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

public final class CsvHarness {
    public static void main(String[] args) throws Exception {
        Path path = Path.of(args[0]);
        InputFile file = new InputFile(
            0, path, "fixture.csv", "text/csv", Files.size(path), "0".repeat(64)
        );
        try {
            new Adapter().handle(
                new Context(Map.of(), List.of(file)), Map.of("encoding", "UTF-8")
            );
        } catch (Exception error) {
            System.out.print(error.getMessage());
        }
    }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "DlrRuntime.java", "Adapter.java", "CsvHarness.java"],
        cwd=csv_root,
        check=True,
        capture_output=True,
        text=True,
    )
    invalid_csv = csv_root / "fixture.csv"
    invalid_csv.write_bytes(b"\xff")
    completed = subprocess.run(
        ["java", "-cp", str(csv_root), "CsvHarness", str(invalid_csv)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "invalid_encoding_or_content"
