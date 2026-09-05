"""Mid-stream failure gates for the PostgreSQL and MySQL template Recipes."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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
def test_python_database_midstream_failure_keeps_complete_rows_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    slug: str,
    module_name: str,
    secret_name: str,
    dsn: str,
) -> None:
    class Cursor:
        def __init__(self, data: bool) -> None:
            self.data = data
            self.description = [("id",)] if data else None
            self.fetches = 0

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            return None

        def fetchmany(self, _size: int) -> list[tuple[int]]:
            self.fetches += 1
            if self.fetches == 1:
                return [(1,)]
            raise RuntimeError("AUDIT_DRIVER_SECRET")

    class Connection:
        def __init__(self) -> None:
            self.cursor_calls = 0
            self.rollbacks = 0
            self.closes = 0

        def execute(self, *_args: object) -> None:
            return None

        def cursor(self, *_args: object, **kwargs: object) -> Cursor:
            self.cursor_calls += 1
            is_data = module_name == "psycopg" or self.cursor_calls > 1 or bool(kwargs.get("name"))
            return Cursor(is_data)

        def begin(self) -> None:
            return None

        def rollback(self) -> None:
            self.rollbacks += 1

        def close(self) -> None:
            self.closes += 1

    module = ModuleType(module_name)
    if module_name == "pymysql":
        cursors = ModuleType("pymysql.cursors")
        cursors.SSCursor = object()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors)
    monkeypatch.setitem(sys.modules, module_name, module)
    connection = Connection()
    module.connect = lambda *_args, **_kwargs: connection  # type: ignore[attr-defined]
    namespace = runpy.run_path(str(CATALOG_ROOT / f"variants/{slug}/python.py"))

    result = namespace["handle"](
        SimpleNamespace(secrets={secret_name: dsn}),
        {"sql": "SELECT id FROM inventory", "batch_size": 1},
    )

    assert result == {
        "rows": [{"id": 1}],
        "count": 1,
        "partial": True,
        "error": "database_query_failed",
    }
    assert connection.rollbacks >= 1
    assert connection.closes == 1
    assert "SECRET" not in json.dumps(result)


def _run_javascript_fixture(
    tmp_path: Path,
    slug: str,
    package_name: str,
    driver_source: str,
    secret_name: str,
    dsn: str,
) -> dict[str, object]:
    fixture = tmp_path / slug
    module_root = fixture / f"node_modules/{package_name}"
    module_root.mkdir(parents=True)
    (fixture / "recipe.mjs").write_text(
        (CATALOG_ROOT / f"variants/{slug}/javascript.mjs").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (module_root / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
    )
    (module_root / "index.js").write_text(driver_source, encoding="utf-8")
    script = f"""
import {{ handle }} from "./recipe.mjs";
const result = await handle(
  {{ secrets: new Map([["{secret_name}", "{dsn}"]]) }},
  {{ sql: "SELECT id FROM inventory", batch_size: 1 }},
);
process.stdout.write(JSON.stringify({{ result, audit: globalThis.databaseAudit }}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=fixture,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_javascript_postgresql_midstream_failure_keeps_complete_rows_and_closes(
    tmp_path: Path,
) -> None:
    output = _run_javascript_fixture(
        tmp_path,
        "postgresql-readonly-snapshot",
        "pg",
        """
globalThis.databaseAudit = { fetches: 0, rollback: 0, end: 0 };
class Client {
  async connect() {}
  async query(request) {
    const text = typeof request === "string" ? request : request.text;
    if (text === "ROLLBACK") { globalThis.databaseAudit.rollback += 1; return {}; }
    if (text.startsWith("FETCH FORWARD")) {
      globalThis.databaseAudit.fetches += 1;
      if (globalThis.databaseAudit.fetches === 1) {
        return { fields: [{ name: "id" }], rows: [[1]] };
      }
      throw new Error("AUDIT_DRIVER_SECRET");
    }
    return {};
  }
  async end() { globalThis.databaseAudit.end += 1; }
}
const types = { getTypeParser() { return (value) => value; } };
export default { Client, types };
""",
        "POSTGRES_DSN",
        "postgresql://fixture",
    )
    assert output["result"] == {
        "rows": [{"id": 1}],
        "count": 1,
        "partial": True,
        "error": "database_query_failed",
    }
    assert output["audit"] == {"fetches": 2, "rollback": 1, "end": 1}
    assert "SECRET" not in json.dumps(output)


def test_javascript_mysql_midstream_failure_keeps_complete_rows_and_closes(
    tmp_path: Path,
) -> None:
    output = _run_javascript_fixture(
        tmp_path,
        "mysql-readonly-snapshot",
        "mysql2",
        """
globalThis.databaseAudit = { rollback: 0, end: 0, destroy: 0 };
function createConnection() {
  return {
    connect(callback) { callback(null); },
    query(sql, values, callback) {
      if (typeof sql === "object") {
        return {
          once(event, listener) {
            if (event === "fields") listener([{ name: "id", type: 3 }]);
            return this;
          },
          stream() {
            return {
              async *[Symbol.asyncIterator]() {
                yield [1];
                throw new Error("AUDIT_DRIVER_SECRET");
              },
            };
          },
        };
      }
      if (sql === "ROLLBACK") globalThis.databaseAudit.rollback += 1;
      const done = typeof values === "function" ? values : callback;
      done(null, {});
    },
    end(callback) { globalThis.databaseAudit.end += 1; callback(); },
    destroy() { globalThis.databaseAudit.destroy += 1; },
  };
}
export default { createConnection };
""",
        "MYSQL_DSN",
        "mysql://user@host/db",
    )
    assert output["result"] == {
        "rows": [{"id": 1}],
        "count": 1,
        "partial": True,
        "error": "database_query_failed",
    }
    assert output["audit"] == {"rollback": 1, "end": 1, "destroy": 0}
    assert "SECRET" not in json.dumps(output)


JAVA_DRIVER_SOURCE = r"""
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
import java.util.Properties;
import java.util.logging.Logger;

public final class FakeDriver implements Driver {
    public static int rollbacks = 0;
    public static int closes = 0;
    static {
        try { DriverManager.registerDriver(new FakeDriver()); }
        catch (SQLException error) { throw new ExceptionInInitializerError(error); }
    }
    public Connection connect(String url, Properties info) {
        if (!acceptsURL(url)) return null;
        return (Connection) Proxy.newProxyInstance(
            FakeDriver.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "createStatement" -> statement();
                case "prepareStatement" -> preparedStatement();
                case "getAutoCommit" -> false;
                case "rollback" -> { rollbacks += 1; yield null; }
                case "close" -> { closes += 1; yield null; }
                case "isClosed", "isWrapperFor" -> false;
                case "unwrap" -> throw new SQLException("not_a_wrapper");
                default -> defaultValue(method.getReturnType());
            }
        );
    }
    private static Statement statement() {
        return (Statement) Proxy.newProxyInstance(
            FakeDriver.class.getClassLoader(), new Class<?>[] {Statement.class},
            (proxy, method, args) -> method.getName().equals("execute")
                ? true : defaultValue(method.getReturnType())
        );
    }
    private static PreparedStatement preparedStatement() {
        return (PreparedStatement) Proxy.newProxyInstance(
            FakeDriver.class.getClassLoader(), new Class<?>[] {PreparedStatement.class},
            (proxy, method, args) -> method.getName().equals("executeQuery")
                ? resultSet() : defaultValue(method.getReturnType())
        );
    }
    private static ResultSet resultSet() {
        int[] calls = {0};
        ResultSetMetaData metadata = metadata();
        return (ResultSet) Proxy.newProxyInstance(
            FakeDriver.class.getClassLoader(), new Class<?>[] {ResultSet.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "getMetaData" -> metadata;
                case "next" -> {
                    calls[0] += 1;
                    if (calls[0] == 1) yield true;
                    throw new SQLException("AUDIT_DRIVER_SECRET");
                }
                case "getObject" -> 1;
                case "wasNull", "isClosed", "isWrapperFor" -> false;
                case "unwrap" -> throw new SQLException("not_a_wrapper");
                default -> defaultValue(method.getReturnType());
            }
        );
    }
    private static ResultSetMetaData metadata() {
        return (ResultSetMetaData) Proxy.newProxyInstance(
            FakeDriver.class.getClassLoader(), new Class<?>[] {ResultSetMetaData.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "getColumnCount" -> 1;
                case "getColumnLabel" -> "id";
                case "getColumnTypeName" -> "INTEGER";
                case "getColumnType" -> Types.INTEGER;
                case "isWrapperFor" -> false;
                case "unwrap" -> throw new SQLException("not_a_wrapper");
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
    public boolean acceptsURL(String url) { return url != null && url.startsWith("jdbc:fixture:"); }
    public DriverPropertyInfo[] getPropertyInfo(String url, Properties info) {
        return new DriverPropertyInfo[0];
    }
    public int getMajorVersion() { return 1; }
    public int getMinorVersion() { return 0; }
    public boolean jdbcCompliant() { return false; }
    public Logger getParentLogger() { return Logger.getGlobal(); }
}
"""


@pytest.mark.parametrize(
    ("slug", "secret_name"),
    [
        ("postgresql-readonly-snapshot", "POSTGRES_DSN"),
        ("mysql-readonly-snapshot", "MYSQL_DSN"),
    ],
)
def test_java_database_midstream_failure_keeps_complete_rows_and_closes(
    tmp_path: Path,
    slug: str,
    secret_name: str,
) -> None:
    compile_root = tmp_path / slug
    compile_root.mkdir()
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(
        (CATALOG_ROOT / f"variants/{slug}/java.java").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (compile_root / "FakeDriver.java").write_text(JAVA_DRIVER_SOURCE, encoding="utf-8")
    (compile_root / "Probe.java").write_text(
        """
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    Class.forName("FakeDriver");
    Object result = new Adapter().handle(
      new Context(Map.of()),
      Map.of("sql", "SELECT id FROM inventory", "batch_size", 1)
    );
    Map<String,Object> output = new LinkedHashMap<>();
    output.put("result", result); output.put("rollbacks", FakeDriver.rollbacks);
    output.put("closes", FakeDriver.closes);
    System.out.print(Json.stringify(output));
  }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "javac",
            "-encoding",
            "UTF-8",
            "DlrRuntime.java",
            "Adapter.java",
            "FakeDriver.java",
            "Probe.java",
        ],
        cwd=compile_root,
        check=True,
        capture_output=True,
        text=True,
    )
    environment = os.environ.copy()
    environment[f"DLR_SECRET_{secret_name}"] = "jdbc:fixture:midstream"
    completed = subprocess.run(
        ["java", "-cp", str(compile_root), "Probe"],
        cwd=compile_root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    output = json.loads(completed.stdout)
    assert output["result"] == {
        "rows": [{"id": 1}],
        "count": 1,
        "partial": True,
        "error": "database_query_failed",
    }
    assert output["rollbacks"] >= 1
    assert output["closes"] == 1
    assert "SECRET" not in completed.stdout
