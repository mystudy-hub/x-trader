"""Collect offline S0 environment evidence; never log in to a broker."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import platform
import re
import sqlite3
import struct
import sys
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = {
    "akshare": "akshare",
    "polars": "polars",
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "numpy": "numpy",
    "pydantic": "pydantic",
    "pyyaml": "yaml",
    "structlog": "structlog",
    "duckdb": "duckdb",
}


def get_file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_reference(root, path):
    return {"path": path.relative_to(root).as_posix(), "sha256": get_file_hash(path)}


def check_python_version(root=ROOT):
    pin = (root / ".python-version").read_text(encoding="utf-8").strip()
    requested = tuple(int(part) for part in pin.split("."))
    actual = tuple(sys.version_info[:3])
    return {
        "version": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "bits": struct.calcsize("P") * 8,
        "requested": pin,
        "supported": actual >= (3, 12) and actual[: len(requested)] == requested,
    }


def check_core_dependencies(root=ROOT):
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    versions = {}
    for package in lock["package"]:
        versions.setdefault(package["name"], set()).add(package.get("version"))
    results = {}
    for package, module_name in DEPENDENCIES.items():
        try:
            importlib.import_module(module_name)
            version = importlib.metadata.version(package)
            matched = version in versions.get(package, set())
            results[package] = {"status": "OK" if matched else "LOCK_MISMATCH", "version": version}
        except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as exc:
            results[package] = {"status": "FAILED", "error_type": type(exc).__name__}
    return results


def check_sqlite(root=ROOT):
    probe_root = root / "runs/s0"
    probe_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="sqlite-probe-", dir=probe_root) as temporary:
        connection = sqlite3.connect(Path(temporary) / "probe.db")
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            settings = {
                key: connection.execute(f"PRAGMA {key}").fetchone()[0]
                for key in ("synchronous", "foreign_keys", "busy_timeout")
            }
            connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
            enforced = False
            try:
                connection.execute("INSERT INTO child VALUES (999)")
            except sqlite3.IntegrityError:
                enforced = True
            connection.rollback()
        finally:
            connection.close()
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "probe_kind": "local_file_connection_settings",
        "journal_mode": mode,
        **settings,
        "foreign_key_enforced": enforced,
        "passed": mode == "wal"
        and settings == {"synchronous": 2, "foreign_keys": 1, "busy_timeout": 5000}
        and enforced,
        "durability_fault_tests": "not_executed",
    }


def check_ctp_candidates():
    results = {}
    for name in ("openctp_ctp", "vnpy_ctp"):
        try:
            module = importlib.import_module(name)
            source = Path(module.__file__) if getattr(module, "__file__", None) else None
            results[name] = {
                "status": "IMPORTABLE",
                "version": str(getattr(module, "__version__", "unknown")),
                "module_sha256": get_file_hash(source) if source and source.is_file() else None,
                "login_query_callback_tests": "not_executed",
            }
        except (ImportError, OSError) as exc:
            results[name] = {
                "status": "NOT_INSTALLED",
                "error_type": type(exc).__name__,
                "login_query_callback_tests": "not_executed",
            }
    return results


def inspect_sdk_archives(root=ROOT):
    archives = []
    for path in sorted((root / "docs/simnow").glob("*.zip")):
        record = file_reference(root, path)
        hint = re.search(r"(\d+\.\d+\.\d+)", path.name)
        record["version_hint_from_filename"] = hint[1] if hint else None
        record["members"] = []
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if member.is_dir() or not member.filename.lower().endswith((".dll", ".lib", ".h", ".so")):
                        continue
                    digest = hashlib.sha256()
                    with archive.open(member) as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    record["members"].append(
                        {"name": member.filename, "size": member.file_size, "sha256": digest.hexdigest()}
                    )
            record["status"] = "INVENTORIED"
        except (OSError, zipfile.BadZipFile) as exc:
            record["status"] = "INVALID"
            record["error_type"] = type(exc).__name__
        archives.append(record)
    return archives


def build_report(root=ROOT, ctp_evidence=None):
    root = Path(root).resolve()
    dependencies = check_core_dependencies(root)
    report = {
        "schema_version": 1,
        "kind": "s0_environment",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            name: file_reference(root, root / name) for name in ("pyproject.toml", "uv.lock", ".python-version")
        },
        "python": check_python_version(root),
        "dependencies": dependencies,
        "required_dependencies_ok": all(item["status"] == "OK" for item in dependencies.values()),
        "sqlite": check_sqlite(root),
        "ctp_candidates": check_ctp_candidates(),
        "sdk_archives": inspect_sdk_archives(root),
        "scope": "离线依赖、文件级 SQLite 配置及 SDK 归档；不代表柜台登录、回报或断电恢复通过",
    }
    if ctp_evidence:
        evidence = (root / ctp_evidence).resolve()
        if not evidence.is_relative_to(root) or not evidence.is_file():
            raise ValueError("CTP 联调证据必须是项目内的已存在文件")
        report["ctp_runtime_evidence"] = file_reference(root, evidence)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="runs/s0/environment.json")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ctp-evidence", help="Attach an existing redacted CTP verification record; does not run it")
    args = parser.parse_args(argv)
    target = (ROOT / args.output).resolve()
    if not target.is_relative_to(ROOT / "runs"):
        parser.error("环境证据须写入项目 runs/，避免将机器相关信息提交到 Git")
    try:
        report = build_report(ctp_evidence=args.ctp_evidence)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"FAIL: environment evidence could not be collected ({type(exc).__name__})", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"Python: {report['python']['version']} / {report['python']['platform']} / {report['python']['bits']} bit"
        )
        for name, item in report["dependencies"].items():
            print(f"{name}: {item['status']} {item.get('version', '')}")
        sqlite = report["sqlite"]
        print(
            f"SQLite: {sqlite['journal_mode']}, synchronous={sqlite['synchronous']}, "
            f"foreign_keys={sqlite['foreign_keys']}, busy_timeout={sqlite['busy_timeout']}"
        )
        for name, item in report["ctp_candidates"].items():
            print(f"{name}: {item['status']}；登录/查询/回报未执行")
        print(f"SDK 归档清单：{len(report['sdk_archives'])} 个；文件名版本线索不等于兼容性验证")
        print(f"报告：{target.relative_to(ROOT).as_posix()}")
    passed = report["python"]["supported"] and report["required_dependencies_ok"] and report["sqlite"]["passed"]
    return 0 if passed else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
