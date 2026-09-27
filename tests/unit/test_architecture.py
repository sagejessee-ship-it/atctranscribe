"""Guards for architectural decisions that are easy to erode by accident."""

import ast
import subprocess
import sys
from pathlib import Path

from aerochorus.db import models
from aerochorus.db.base import Base

SRC = Path(__file__).resolve().parents[2] / "src" / "aerochorus"


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_worker_never_loads_the_database_layer():
    """Workers talk to the API, never to PostgreSQL (ADR: worker coupling via API)."""
    probe = (
        "import sys\n"
        "import aerochorus.cli, aerochorus.worker.daemon, aerochorus.worker.scanner\n"
        "import aerochorus.worker.health, aerochorus.worker.client, aerochorus.edge.server\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in ('sqlalchemy', 'alembic', 'psycopg')"
        " or m.startswith(('aerochorus.db', 'aerochorus.api'))]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# The only modules allowed to write files, each to its own configured root:
# model artifacts, raw CrispASR output (ADR-004), CrispASR server logs, and
# benchmark clip preparation (writes a new derived corpus; never touches input).
WRITERS = {"model_store.py", "artifacts.py", "crispasr.py", "atco2.py", "faa_nasr.py"}


def test_source_audio_cannot_be_modified():
    """Only designated modules write files, and never the corpus reader (ADR-002)."""
    mutating_os = {
        "remove",
        "unlink",
        "rename",
        "renames",
        "replace",
        "rmdir",
        "removedirs",
        "utime",
        "chmod",
        "chown",
        "lchown",
        "truncate",
        "link",
        "symlink",
        "mkdir",
        "makedirs",
    }
    mutating_path = {
        "write_bytes",
        "write_text",
        "unlink",
        "touch",
        "rmdir",
        "mkdir",
        "chmod",
        "symlink_to",
        "hardlink_to",
        "rename",
    }
    violations = []
    for path, tree in _modules():
        if path.name in WRITERS:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "shutil":
                violations.append(f"{path.name}:{node.lineno} imports from shutil")
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "shutil"
                and node.attr != "which"
            ):
                violations.append(f"{path.name}:{node.lineno} uses shutil.{node.attr}")
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                owner = func.value.id if isinstance(func.value, ast.Name) else None
                if (owner == "os" and func.attr in mutating_os) or func.attr in mutating_path:
                    violations.append(f"{path.name}:{node.lineno} calls {func.attr}")
            builtin_open = isinstance(func, ast.Name) and func.id == "open"
            method_open = isinstance(func, ast.Attribute) and func.attr == "open"
            if builtin_open or method_open:
                # open(path, mode) versus Path.open(mode)
                position = 1 if builtin_open else 0
                mode = node.args[position] if len(node.args) > position else None
                for kw in node.keywords:
                    if kw.arg == "mode":
                        mode = kw.value
                if not (isinstance(mode, ast.Constant) and set(mode.value) <= {"r", "b"}):
                    violations.append(f"{path.name}:{node.lineno} opens without a read mode")
    assert not violations, violations
    assert "fs.py" not in WRITERS and "scanner.py" not in WRITERS


def test_exactly_one_architecture_family_registry():
    """ADR-009: model independence has one source of truth, the model registry."""
    family_columns = [
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if "family" in column.name
        # Derived counts, recomputed from the registry (api/agreement.py), are not registries.
        and not (table.name == "segment_agreement" and column.name.endswith("_family_count"))
    ]
    assert family_columns == [("model", "architecture_family")]
    fk = next(iter(models.Model.__table__.c.architecture_family.foreign_keys))
    assert fk.target_fullname == "architecture_family.key"

    literal_types = (ast.Dict, ast.DictComp, ast.Set, ast.SetComp, ast.List, ast.Tuple)
    shadow_registries = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for target in targets:
                name = getattr(target, "id", None) or getattr(target, "attr", "")
                if "family" in name.lower() and isinstance(value, literal_types):
                    shadow_registries.append(f"{path.name}:{node.lineno} {name}")
    assert not shadow_registries, shadow_registries


def test_no_jsonl_operational_datastore():
    """ADR-003: PostgreSQL is authoritative; JSONL is export-only (none exists yet)."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "jsonl" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


def test_source_rows_are_read_only_by_construction():
    checks = {c.name for c in models.CorpusSource.__table__.constraints if c.name}
    assert "ck_corpus_source_read_only" in checks


def test_transcription_path_cannot_see_gold():
    """ADR-015: gold references live below the leakage line."""
    probe = (
        "import sys\n"
        "import aerochorus.worker.transcriber, aerochorus.worker.scanner\n"
        "import aerochorus.worker.daemon\n"
        "bad = [m for m in sys.modules if m.startswith(('aerochorus.datasets',"
        " 'aerochorus.eval_contracts', 'aerochorus.eval_client', 'aerochorus.api'))]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    # Only the evaluation service uses the gold model (db/models.py merely defines it).
    readers = sorted(
        path.relative_to(SRC).as_posix()
        for path, tree in _modules()
        if any(
            isinstance(node, ast.Name | ast.alias)
            and (getattr(node, "id", None) or getattr(node, "name", None)) == "GoldSegment"
            for node in ast.walk(tree)
        )
    )
    assert readers == ["api/evaluation.py"]
