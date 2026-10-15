import annotationlib
import ast
import dataclasses
import importlib
import inspect
import json
import pkgutil
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import pytest
from builders import FIXTURES_ROOT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ocfl_lib.fs import FileRef, FsEntry

REPO = Path(__file__).resolve().parents[3]
LIB_SRC = REPO / "packages/ocfl-lib/src/ocfl_lib"
CLI_SRC = REPO / "packages/ocfl-cli/src/ocfl_cli"
INDEX_SRC = REPO / "packages/ocfl-index/src/ocfl_index"


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(p: Path) -> ast.Module:
    return ast.parse(p.read_text(), filename=str(p))


def _lib_module_name(p: Path) -> str:
    rel = p.relative_to(LIB_SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class TestErrors:
    def _error_classes(self):
        import ocfl_lib

        base = ocfl_lib.OcflError
        seen: list[type[ocfl_lib.OcflError]] = []
        stack: list[type[ocfl_lib.OcflError]] = [base]
        while stack:
            c = stack.pop()
            if c.__module__.startswith("ocfl_lib"):
                seen.append(c)
            stack.extend(c.__subclasses__())
        return seen

    def test_error_class_enum(self):
        from ocfl_lib import ErrorClass

        assert {m.name for m in ErrorClass} == {
            "USAGE",
            "NOT_FOUND",
            "INVALID",
            "SERVICE",
            "STATE",
            "BUSY",
            "HALT",
            "CONFIG",
            "INTERRUPTED",
        }

    def test_every_error_has_class_and_unique_code(self):
        from ocfl_lib import ErrorClass

        codes: dict[str, type] = {}
        for c in self._error_classes():
            assert "code" in vars(c), f"{c.__name__} lacks its own code"
            assert "error_class" in vars(c), f"{c.__name__} lacks error_class"
            assert isinstance(c.error_class, ErrorClass), c
            assert re.fullmatch(r"[a-z][a-z0-9_]*", c.code), (c, c.code)
            assert c.code not in codes, (c, codes.get(c.code))
            codes[c.code] = c

    def test_every_error_exported(self):
        import ocfl_lib

        for c in self._error_classes():
            assert c.__name__ in ocfl_lib.__all__, c.__name__
            assert getattr(ocfl_lib, c.__name__) is c

    def test_lib_raises_only_ocfl_errors(self):
        import ocfl_lib

        allowed_builtin = {"ExceptionGroup"}
        exempt = {"fs/local.py", "fs/_local.py", "fs/s3.py"}
        bad = []
        for p in _py_files(LIB_SRC):
            rel = p.relative_to(LIB_SRC).as_posix()
            if rel in exempt:
                continue
            for node in ast.walk(_tree(p)):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if not isinstance(exc, ast.Name) or not exc.id[:1].isupper():
                    continue
                if exc.id in allowed_builtin:
                    continue
                cls = getattr(ocfl_lib, exc.id, None)
                if not (isinstance(cls, type) and issubclass(cls, ocfl_lib.OcflError)):
                    bad.append(f"{rel}:{node.lineno} raise {exc.id}")
        assert not bad, "\n".join(bad)

    def test_no_unused_error_classes(self):
        raised: set[str] = set()
        for p in _py_files(LIB_SRC):
            for node in ast.walk(_tree(p)):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                    if isinstance(exc, ast.Name):
                        raised.add(exc.id)
        classes = self._error_classes()
        used = set()
        for c in classes:
            if c.__name__ in raised:
                used.update(b.__name__ for b in c.__mro__)
        unused = [c.__name__ for c in classes if c.__name__ not in used]
        assert not unused, unused

    def test_compat_bases(self):
        import ocfl_lib

        assert issubclass(ocfl_lib.ObjectNotFound, LookupError)
        assert issubclass(ocfl_lib.VersionNotFound, LookupError)


PUBLIC_MODULES = {
    "ocfl_lib",
    "ocfl_lib.bitcoin",
    "ocfl_lib.fs",
    "ocfl_lib.fs.s3",
    "ocfl_lib.ots",
    "ocfl_lib.validation",
}


class TestPublicApi:
    def test_only_public_modules_lack_underscore(self):
        public = set()
        for p in _py_files(LIB_SRC):
            name = _lib_module_name(p)
            if not any(seg.startswith("_") for seg in name.split(".")):
                public.add(name)
        assert public == PUBLIC_MODULES

    def test_consumers_import_no_private_modules(self):
        bad = []
        roots = [
            CLI_SRC,
            INDEX_SRC,
            REPO / "packages/ocfl-cli/tests",
            REPO / "packages/ocfl-index/tests",
        ]
        for root in roots:
            for p in _py_files(root):
                for node in ast.walk(_tree(p)):
                    mods = []
                    if isinstance(node, ast.ImportFrom) and node.module:
                        mods = [node.module]
                    elif isinstance(node, ast.Import):
                        mods = [a.name for a in node.names]
                    for m in mods:
                        segs = m.split(".")
                        if segs[0] == "ocfl_lib" and any(
                            s.startswith("_") for s in segs[1:]
                        ):
                            bad.append(f"{p}:{getattr(node, 'lineno', '?')} {m}")
                    if (
                        isinstance(node, ast.ImportFrom)
                        and node.module
                        and (node.module.split(".")[0] == "ocfl_lib")
                    ):
                        for a in node.names:
                            if a.name.startswith("_"):
                                bad.append(f"{p}:{node.lineno} {a.name}")
        assert not bad, "\n".join(bad)

    def test_all_names_importable(self):
        import ocfl_lib

        for name in ocfl_lib.__all__:
            assert hasattr(ocfl_lib, name), name

    def test_object_path_public(self):
        from ocfl_lib import LocalFS, Object

        obj = Object(LocalFS(REPO), "some/path")
        assert obj.path == "some/path"

    def test_changelog(self):
        assert (REPO / "packages/ocfl-lib/CHANGELOG.md").is_file()


class _BoomFS:
    def open_file(self, name: str) -> BinaryIO:
        raise AssertionError(f"I/O in constructor: open_file {name}")

    def read_dir(self, name: str) -> Iterator[FsEntry]:
        raise AssertionError(f"I/O in constructor: read_dir {name}")

    def exists(self, name: str) -> bool:
        raise AssertionError(f"I/O in constructor: exists {name}")

    def walk_files(self, name: str) -> Iterator[FileRef]:
        raise AssertionError(f"I/O in constructor: walk_files {name}")


class TestReadOnlyObjectAndOpen:
    def test_object_has_no_write_methods(self):
        from ocfl_lib import Object

        for name in ("update", "new_update_plan", "apply_update_plan"):
            assert not hasattr(Object, name), name
        for name, fn in inspect.getmembers(Object, inspect.isfunction):
            sig = inspect.signature(fn, annotation_format=annotationlib.Format.STRING)
            assert "WriteFS" not in str(sig), name

    def test_constructors_do_no_io(self):
        from ocfl_lib import Object, StorageRoot

        StorageRoot(_BoomFS())
        Object(_BoomFS(), "x")

    def test_open(self, tmp_path):
        from ocfl_lib import LocalFS, NotAnOcflRoot, Spec, StorageRoot

        with pytest.raises(NotAnOcflRoot):
            StorageRoot.open(LocalFS(tmp_path))
        fs = LocalFS(tmp_path)
        StorageRoot.init(fs, fs, spec=Spec.V1_0)
        root = StorageRoot.open(fs)
        assert root.spec is Spec.V1_0
        assert root.layout is None


class TestVersionState:
    def test_names(self):
        import ocfl_lib

        assert hasattr(ocfl_lib, "VersionState")
        assert not hasattr(ocfl_lib, "Stage")

    def test_no_stage_identifier_anywhere(self):
        bad = []
        for root in (LIB_SRC, CLI_SRC, INDEX_SRC):
            for p in _py_files(root):
                for node in ast.walk(_tree(p)):
                    name = None
                    if isinstance(node, ast.Name):
                        name = node.id
                    elif isinstance(node, ast.ClassDef):
                        name = node.name
                    elif isinstance(node, ast.alias):
                        name = node.name.split(".")[-1]
                    elif isinstance(node, ast.Attribute):
                        name = node.attr
                    if name == "Stage":
                        bad.append(f"{p}:{getattr(node, 'lineno', '?')}")
        assert not bad, bad

    def test_sources_module(self):
        import ocfl_lib

        cs, fx = ocfl_lib.ContentSource, ocfl_lib.FixitySource
        assert cs.__module__ == fx.__module__
        assert cs.__module__ != ocfl_lib.VersionState.__module__
        assert cs.__module__ != ocfl_lib.CommitPlan.__module__


class TestJsonValue:
    def test_json_value_exported(self):
        import ocfl_lib

        assert hasattr(ocfl_lib, "JsonValue")

    def test_validators_use_no_any_or_cast(self):
        bad = []
        for p in _py_files(LIB_SRC / "validation"):
            for node in ast.walk(_tree(p)):
                if isinstance(node, ast.Name) and node.id in ("Any", "cast"):
                    bad.append(f"{p.name}:{node.lineno} {node.id}")
                if isinstance(node, ast.Attribute) and node.attr in ("Any", "cast"):
                    bad.append(f"{p.name}:{node.lineno} {node.attr}")
                if isinstance(node, ast.alias) and node.name in ("Any", "cast"):
                    bad.append(f"{p.name}: import {node.name}")
        assert not bad, bad


class TestValidationReport:
    def test_types(self):
        from ocfl_lib.validation import Finding, ValidationReport

        for cls in (Finding, ValidationReport):
            assert dataclasses.is_dataclass(cls)
            assert cls.__dataclass_params__.frozen, cls
        assert [f.name for f in dataclasses.fields(Finding)] == [
            "code",
            "severity",
            "object_id",
            "path",
            "detail",
        ]
        assert "message" not in {f.name for f in dataclasses.fields(ValidationReport)}

    def test_old_result_gone(self):
        import ocfl_lib

        assert not hasattr(ocfl_lib, "ValidationResult")

    def test_validate_object_returns_report(self):
        from ocfl_lib import LocalFS
        from ocfl_lib.validation import Finding, ValidationReport, validate_object

        good = FIXTURES_ROOT / "1.1/good-objects/minimal_one_version_one_file"
        report = validate_object(LocalFS(good), "")
        assert isinstance(report, ValidationReport)
        assert report.is_valid
        bad = FIXTURES_ROOT / "1.1/bad-objects/E001_extra_file_in_root"
        report = validate_object(LocalFS(bad), "")
        assert not report.is_valid
        f = next(f for f in report.findings if f.code.startswith("E001"))
        assert isinstance(f, Finding)
        assert f.object_id == "ark:123/abc"
        assert isinstance(f.render(), str) and f.render()

    def test_root_keeps_warnings_of_valid_objects(self, tmp_path):
        from ocfl_lib import LocalFS, Spec, StorageRoot
        from ocfl_lib.validation import validate_root

        fs = LocalFS(tmp_path)
        StorageRoot.init(fs, fs, spec=Spec.V1_1)
        src = FIXTURES_ROOT / "1.1/warn-objects/W005_id_not_uri"
        shutil.copytree(src, tmp_path / "a/obj")
        report = validate_root(fs, "")
        assert report.is_valid
        w = [f for f in report.findings if f.code.startswith("W005")]
        assert w, report.findings
        assert w[0].path == "a/obj"


class TestLayoutRegistry:
    def test_default_frozen(self):
        from ocfl_lib import DEFAULT_LAYOUTS, LayoutRegistry

        assert not hasattr(LayoutRegistry, "register")
        with pytest.raises(
            (AttributeError, TypeError, dataclasses.FrozenInstanceError)
        ):
            DEFAULT_LAYOUTS.foo = 1  # ty: ignore[invalid-assignment]

    def test_custom_registry_passed_explicitly(self, tmp_path):
        from ocfl_lib import (
            DEFAULT_LAYOUTS,
            FlatDirectLayout,
            LocalFS,
            OcflError,
            StorageRoot,
        )

        name = "9999-custom-layout"

        class Custom(FlatDirectLayout):
            @property
            def name(self):
                return name

            @property
            def config(self):
                return None

        custom = DEFAULT_LAYOUTS.with_layout(name, lambda cfg: Custom())
        fs = LocalFS(tmp_path)
        StorageRoot.init(fs, fs, layout=Custom())
        with pytest.raises(OcflError):
            StorageRoot.open(fs)
        root = StorageRoot.open(fs, layouts=custom)
        assert root.layout is not None and root.layout.name == name
        assert name not in DEFAULT_LAYOUTS


class TestNoDuplicates:
    def _lib_trees(self):
        return [(p, _tree(p)) for p in _py_files(LIB_SRC)]

    def test_one_join_helper(self):
        defs = [
            f"{p.name}:{n.name}"
            for p, t in self._lib_trees()
            for n in ast.walk(t)
            if isinstance(n, ast.FunctionDef)
            and n.name.lstrip("_") in ("join", "join_path")
        ]
        assert len(defs) == 1, defs

    def test_one_version_regex(self):
        hits = [
            f"{p.name}:{n.lineno}"
            for p, t in self._lib_trees()
            for n in ast.walk(t)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value.startswith("^v")
            and "\\d" in n.value
        ]
        assert len(hits) == 1, hits

    def test_no_supported_version_sets(self):
        bad = []
        for p, t in self._lib_trees():
            for n in ast.walk(t):
                if isinstance(n, ast.Name) and "SUPPORTED" in n.id:
                    bad.append(f"{p.name}:{n.lineno} {n.id}")
                if isinstance(n, (ast.Set, ast.List, ast.Tuple)) and any(
                    isinstance(e, ast.Constant) and e.value in ("1.0", "1.1")
                    for e in n.elts
                ):
                    bad.append(f"{p.name}:{n.lineno} literal version set")
        assert not bad, bad

    def test_root_uses_layout_config_helpers(self):
        root_py = next(
            p for p in _py_files(LIB_SRC) if p.name in ("root.py", "_root.py")
        )
        text = root_py.read_text()
        assert "ocfl_layout.json" not in text
        assert "config.json" not in text
        assert "msgspec" not in text

    def test_one_multi_digest_loop(self):
        hits = 0
        for p in _py_files(LIB_SRC):
            hits += p.read_text().count(".new_hasher() for ")
        assert hits == 1, hits

    def test_no_fs_utils_exists(self):
        fs = importlib.import_module("ocfl_lib.fs")
        for m in pkgutil.iter_modules(fs.__path__, "ocfl_lib.fs."):
            if "util" in m.name:
                mod = importlib.import_module(m.name)
                assert not hasattr(mod, "exists"), m.name
        assert not hasattr(fs, "exists")


def _plan(tmp_path):
    from ocfl_lib import (
        DigestAlgorithm,
        LocalFS,
        Spec,
        StorageRoot,
        get_layout,
        scan_dir,
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    fs = LocalFS(root_dir)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    vs = scan_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)
    return StorageRoot.open(fs).plan("x:1", vs, created="2026-01-01T00:00:00Z")


class TestPlanIntent:
    def test_plan_fields_are_intent_only(self):
        from ocfl_lib import CommitPlan

        assert CommitPlan.__dataclass_params__.frozen
        assert [f.name for f in dataclasses.fields(CommitPlan)] == [
            "object_id",
            "version",
            "base_digest",
            "base_digest_algorithm",
            "inventory",
            "inventory_proof",
        ]

    def test_plan_json_keys(self, tmp_path):
        data = json.loads(_plan(tmp_path).to_json())
        assert set(data) == {
            "planFormat",
            "objectId",
            "version",
            "base",
            "inventory",
            "inventoryProof",
        }

    def test_no_step_serialization(self):
        import ocfl_lib

        for name in ("serialize_plan", "deserialize_plan"):
            assert not hasattr(ocfl_lib, name), name
        bad = [
            f"{p.name}:{node.name}"
            for p in _py_files(LIB_SRC)
            for node in ast.walk(_tree(p))
            if isinstance(node, ast.ClassDef)
            and node.name in ("_StepState", "_PlanState")
        ]
        assert not bad, bad


_OLD_WRITE_API = (
    "update_object",
    "plan_update",
    "apply_update_plan",
    "prepare_steps",
    "execute_plan",
    "revert_plan",
    "serialize_plan",
    "deserialize_plan",
    "UpdatePlan",
)


def _sig(fn) -> str:
    return str(inspect.signature(fn, annotation_format=annotationlib.Format.STRING))


class TestSingleWriteEntryPoint:
    def test_old_write_api_gone_from_public_modules(self):
        for mod_name in sorted(PUBLIC_MODULES):
            mod = importlib.import_module(mod_name)
            for name in _OLD_WRITE_API:
                assert not hasattr(mod, name), (mod_name, name)

    def test_no_public_function_takes_writefs(self):
        import ocfl_lib

        bad = [
            name
            for name in ocfl_lib.__all__
            if inspect.isfunction(getattr(ocfl_lib, name))
            and "WriteFS" in _sig(getattr(ocfl_lib, name))
        ]
        assert not bad, bad

    def test_only_storage_root_methods_write(self):
        from ocfl_lib import StorageRoot

        writers = {
            name
            for name, fn in inspect.getmembers(StorageRoot)
            if not name.startswith("_") and callable(fn) and "WriteFS" in _sig(fn)
        }
        assert writers <= {"init", "commit", "purge", "recover"}, writers
        assert "commit" in writers

    def test_consumers_do_not_name_old_write_api(self):
        bad = []
        for root in (CLI_SRC, INDEX_SRC):
            for p in _py_files(root):
                for node in ast.walk(_tree(p)):
                    name = None
                    if isinstance(node, ast.Name):
                        name = node.id
                    elif isinstance(node, ast.Attribute):
                        name = node.attr
                    elif isinstance(node, ast.alias):
                        name = node.name.split(".")[-1]
                    if name in _OLD_WRITE_API:
                        bad.append(f"{p}:{getattr(node, 'lineno', '?')} {name}")
        assert not bad, bad


class TestClaimThenPublish:
    def test_backends_declare_their_claim(self):
        from ocfl_lib.fs import ClaimFS, ConditionalFS, LocalFS
        from ocfl_lib.fs.s3 import S3FS

        assert issubclass(LocalFS, ClaimFS)
        assert not issubclass(LocalFS, ConditionalFS)
        assert issubclass(S3FS, ConditionalFS)
        assert not issubclass(S3FS, ClaimFS)

    def test_local_claim_is_renameat2_noreplace(self):
        text = (LIB_SRC / "fs/_local.py").read_text()
        assert "renameat2" in text and "_RENAME_NOREPLACE = 1" in text

    def test_commit_never_renames_or_replaces_directly(self):
        bad = []
        for node in ast.walk(_tree(LIB_SRC / "_commit.py")):
            if isinstance(node, ast.Attribute) and node.attr in (
                "rename",
                "replace",
                "move",
            ):
                bad.append(f"_commit.py:{node.lineno} {node.attr}")
        assert not bad, bad

    def test_no_step_executor(self):
        bad = [
            f"{p.name}:{n.name}"
            for p in _py_files(LIB_SRC)
            for n in ast.walk(_tree(p))
            if isinstance(n, (ast.ClassDef, ast.FunctionDef))
            and n.name in ("PlanStep", "execute_plan", "revert_plan", "prepare_steps")
        ]
        assert not bad, bad

    def test_work_dir_is_a_documented_root_extension(self):
        from ocfl_lib._commit import WORK_DIR_EXTENSION, work_dir

        assert work_dir("r", "x").startswith(f"r/extensions/{WORK_DIR_EXTENSION}/")
        doc = REPO / f"docs/ext/{WORK_DIR_EXTENSION}.md"
        assert doc.is_file()
        assert f"`{WORK_DIR_EXTENSION}`" in doc.read_text()

    def test_leftover_work_dir_is_w016(self, tmp_path):
        from ocfl_lib import LocalFS, Spec, StorageRoot
        from ocfl_lib._commit import WORK_DIR_EXTENSION
        from ocfl_lib.validation import validate_root

        fs = LocalFS(tmp_path)
        StorageRoot.init(fs, fs, spec=Spec.V1_1)
        (tmp_path / "extensions" / WORK_DIR_EXTENSION).mkdir(parents=True)
        report = validate_root(fs, "", validate_objects=False)
        assert report.has_code("W016")


class TestRecoveryTables:
    def test_one_full_table_per_backend(self):
        from ocfl_lib import CommitState, RecoveryAction
        from ocfl_lib._recovery import TABLES

        assert set(TABLES) == {"local", "s3"}
        for name, table in TABLES.items():
            assert set(table) == set(CommitState), name
            for state, row in table.items():
                assert set(row) == set(RecoveryAction), (name, state)

    def test_tables_differ_where_backends_differ(self):
        from ocfl_lib import CommitState, RecoveryAction
        from ocfl_lib._recovery import TABLES

        claimed = CommitState.CLAIMED
        for action in (RecoveryAction.RESUME, RecoveryAction.REVERT):
            assert TABLES["local"][claimed][action] is not TABLES["s3"][claimed][action]
        stray = TABLES["local"][CommitState.STRAY][RecoveryAction.REMOVE_STRAY]
        assert stray is not TABLES["s3"][CommitState.STRAY][RecoveryAction.REMOVE_STRAY]

    def test_status_reads_only(self, tmp_path):
        from ocfl_lib import LocalFS
        from ocfl_lib._recovery import detect

        inner = LocalFS(tmp_path)

        class ReadOnly:
            def open_file(self, name: str) -> BinaryIO:
                return inner.open_file(name)

            def read_dir(self, name: str) -> Iterator[FsEntry]:
                return inner.read_dir(name)

            def exists(self, name: str) -> bool:
                return inner.exists(name)

        status, plan = detect(ReadOnly(), "", "obj", "x:1")
        assert plan is None and status.state.value == "clean"

    def test_recovery_reads_progress_from_storage_only(self):
        text = (LIB_SRC / "_recovery.py").read_text() + (
            LIB_SRC / "_commit.py"
        ).read_text()
        for word in ("completed", "journal", "progress.json"):
            assert word not in text, word


class TestPurgeGuards:
    def _names(self) -> set[str]:
        names = set()
        for node in ast.walk(_tree(LIB_SRC / "_purge.py")):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    def test_purge_checks_for_live_and_stray_claims(self):
        names = self._names()
        assert {"object_lock", "detect", "RecoveryRequiredError"} <= names

    def test_purge_removes_root_inventory_first(self):
        src = (LIB_SRC / "_purge.py").read_text()
        body = src[src.index("def _delete_in_order") :]
        order = [
            body.index("remove_if_exists(fsys, path(INVENTORY_FILENAME))"),
            body.index('n.startswith(f"{INVENTORY_FILENAME}.")'),
            body.index("sorted(versions, reverse=True)"),
            body.index('("extensions", "logs")'),
            body.index("for name in namastes"),
        ]
        assert order == sorted(order)


class TestRecoverCommand:
    def test_registered_with_its_actions(self):
        import typer.main

        from ocfl_cli.main import app

        group = typer.main.get_group(app)
        cmd = group.commands["recover"]
        opts = {o for p in cmd.params for o in getattr(p, "opts", [])}
        assert {"--resume", "--revert", "--remove-stray"} <= opts

    def test_uses_the_lib_recovery(self):
        tree = _tree(CLI_SRC / "archive/recover.py")
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "recover" in attrs
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "RecoveryAction" in names


class TestCrashTests:
    def test_fault_fs_crashes_like_a_dead_process(self, tmp_path):
        from io import BytesIO

        from ocfl_lib import LocalFS
        from tests.helpers.faults import Crash, fail_at

        assert issubclass(Crash, BaseException)
        assert not issubclass(Crash, Exception)
        fs, counter = fail_at(LocalFS(tmp_path), 2)
        assert isinstance(fs, LocalFS)
        fs.write("a", BytesIO(b"1"))
        with pytest.raises(Crash):
            fs.write("b", BytesIO(b"2"))
        with pytest.raises(Crash):
            fs.mkdir("c")
        assert counter.crashed and not (tmp_path / "b").exists()

    def test_state_machines_cover_both_backends(self):
        tree = _tree(REPO / "packages/ocfl-lib/tests/test_commit_stateful.py")
        machines = {
            n.name: {b.id for b in n.bases if isinstance(b, ast.Name)}
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
        }
        crash_machines = [
            name for name, bases in machines.items() if "RuleBasedStateMachine" in bases
        ]
        assert len(crash_machines) >= 2, machines
        text = (REPO / "packages/ocfl-lib/tests/test_commit_stateful.py").read_text()
        assert "mock_aws" in text and "LocalFS(" in text and "fail_at(" in text


OTS_SPEC = REPO / "docs/ext/NNNN-ots-timestamps.md"


class TestOtsExtensionSpec:
    def test_codes_generated_from_the_spec(self):
        import subprocess
        import sys

        script = REPO / "packages/ocfl-lib/scripts/gen_ots_codes.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--check"], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr

    def test_every_spec_code_registered_and_no_other(self):
        from ocfl_lib.validation import get_code

        table = set(re.findall(r"^\| ([EW]9\d\d) \|", OTS_SPEC.read_text(), re.M))
        data = json.loads((LIB_SRC / "_data/validation-errors.json").read_text())
        assert {c for c in data if re.fullmatch(r"[EW]9\d\d", c)} == table
        for code in table:
            get_code(code)
        with pytest.raises(KeyError):
            get_code("E903")

    def test_extension_dir_without_config_is_e908(self, tmp_path):
        from ocfl_lib import LocalFS, ots
        from ocfl_lib.validation import validate_object
        from tests.helpers.ots import build_ots_object

        build_ots_object(tmp_path)
        (tmp_path / ots.CONFIG_PATH).unlink()
        report = validate_object(LocalFS(tmp_path), "")
        assert report.has_code("E908")

    def test_extra_detail_still_renders(self):
        from ocfl_lib.validation import Finding, Severity

        f = Finding("E905", Severity.ERROR, None, "", {"proof": "x", "reason": "r"})
        assert f.render().endswith("(reason: r)")


OTS_NAME = "NNNN-ots-timestamps"


def _non_doc_strings(p: Path) -> list[tuple[int, str]]:
    tree = _tree(p)
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            docs.add(id(body[0].value))
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docs
    ]


class TestOtsRulesInLib:
    def test_extension_name_defined_once(self):
        homes = [
            f"{p.relative_to(REPO)}:{line}"
            for root in (LIB_SRC, CLI_SRC, INDEX_SRC)
            for p in _py_files(root)
            for line, s in _non_doc_strings(p)
            if OTS_NAME in s
        ]
        assert [h.rsplit(":", 1)[0] for h in homes] == [
            "packages/ocfl-lib/src/ocfl_lib/ots.py"
        ], homes

    def test_commit_has_no_ots_constants(self):
        from ocfl_lib import _commit

        for name in ("OTS_DIR", "OTS_CONFIG_PATH", "OTS_CONFIG_BYTES", "proof_path"):
            assert not hasattr(_commit, name), name

    def test_consumers_build_no_proof_paths(self):
        bad = []
        for root in (CLI_SRC, INDEX_SRC, LIB_SRC):
            for p in _py_files(root):
                if p == LIB_SRC / "ots.py":
                    continue
                for node in ast.walk(_tree(p)):
                    if isinstance(node, ast.JoinedStr):
                        lits = "".join(
                            str(v.value)
                            for v in node.values
                            if isinstance(v, ast.Constant)
                        )
                        if lits.startswith("inventory.json.") and lits.endswith(".ots"):
                            bad.append(f"{p.relative_to(REPO)}:{node.lineno}")
        assert not bad, bad

    def test_consumers_use_the_lib_fs_factory(self):
        bad = [
            str(p.relative_to(REPO))
            for root in (CLI_SRC, INDEX_SRC)
            for p in _py_files(root)
            if "S3FS(" in p.read_text() or "def make_fs" in p.read_text()
        ]
        assert not bad, bad

    def test_cli_validate_walks_lib_proofs(self):
        text = (CLI_SRC / "archive/validate.py").read_text()
        assert "ots.check_object_chain(" in text or "ots.object_proofs(" in text
        assert "_enumerate_versions" not in text


_NETWORK = {"httpx", "httpx2", "requests", "urllib3", "zmq", "socket", "http"}


def _top_imports(p: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(_tree(p)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
    return mods


class TestBitcoinBoundary:
    def test_only_the_bitcoin_extra_has_network_code(self):
        bad = {
            p.relative_to(LIB_SRC).as_posix(): _top_imports(p) & _NETWORK
            for p in _py_files(LIB_SRC)
            if p != LIB_SRC / "bitcoin.py"
        }
        assert not {k: v for k, v in bad.items() if v}, bad
        assert "zmq" not in _top_imports(LIB_SRC / "bitcoin.py")

    def test_base_import_loads_no_network_package(self):
        import subprocess
        import sys

        code = (
            "import sys, ocfl_lib, ocfl_lib.ots, ocfl_lib.validation, ocfl_lib.fs;"
            "print(sorted({m.split('.')[0] for m in sys.modules}"
            " & {'httpx2', 'boto3', 'zmq', 'requests'}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "[]"

    def test_bitcoin_is_an_extra(self):
        import tomllib

        proj = tomllib.loads((REPO / "packages/ocfl-lib/pyproject.toml").read_text())[
            "project"
        ]
        assert not any("httpx" in d for d in proj["dependencies"])
        assert proj["optional-dependencies"]["bitcoin"] == ["httpx2>=2.0"]

    def test_attestation_check_is_pure(self):
        tree = _tree(LIB_SRC / "ots.py")
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "check_attestations"
        )
        calls = {
            n.func.attr
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert calls <= {"block_at", "append"}, calls

    def test_rpc_client_is_a_block_header_source(self):
        from ocfl_lib.bitcoin import BitcoinRpcClient
        from ocfl_lib.ots import BlockHeaderSource

        assert issubclass(BitcoinRpcClient, BlockHeaderSource)

    def test_cli_keeps_zmq_and_reuses_the_lib(self):
        text = (CLI_SRC / "ots/bitcoin.py").read_text()
        assert "class BitcoinZmqSubscriber" in text
        assert "check_attestations(" in text
        assert "from ocfl_lib.bitcoin import BitcoinRpcClient" in text
        cli = "".join(p.read_text() for p in _py_files(CLI_SRC))
        assert '"merkleroot"' not in cli and "[::-1]" not in cli
        assert "verify_attestation" not in (CLI_SRC / "archive/validate.py").read_text()


def _func(path: Path, name: str) -> ast.FunctionDef:
    return next(
        n
        for n in ast.walk(_tree(path))
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


class TestCommitRefusesUnproved:
    def test_extension_checked_before_plan_is_written(self):
        names = _names(_func(LIB_SRC / "_commit.py", "_require_ots_admissible"))
        assert {"read_config", "check_extension"} <= names

    def test_refusals_name_the_spec_codes(self):
        fn = _func(LIB_SRC / "_commit.py", "check_extension")
        raised = {
            n.exc.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Raise)
            and isinstance(n.exc, ast.Call)
            and isinstance(n.exc.func, ast.Name)
        }
        assert {"MissingInventoryProofError", "MissingContentProofError"} <= raised
        text = ast.unparse(fn)
        for code in ("E901", "E902", "E904"):
            assert code in text, code

    def test_state_machines_cover_timestamped_objects(self):
        path = REPO / "packages/ocfl-lib/tests/test_commit_stateful.py"
        tree = _tree(path)
        machines = [
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
            and any(
                isinstance(b, ast.Name) and b.id == "RuleBasedStateMachine"
                for b in n.bases
            )
        ]
        assert {"LocalOtsCommitCrash", "S3OtsCommitCrash"} <= set(machines)
        text = path.read_text()
        assert "inventory_proof" in text and "MissingInventoryProofError" in text


class TestStartTimestamping:
    def test_config_is_written_before_the_proof(self):
        body = ast.unparse(_func(LIB_SRC / "_commit.py", "write_proof"))
        assert body.index("_start_extension_config(t)") < body.index("inventory_proof)")
        start = ast.unparse(_func(LIB_SRC / "_commit.py", "_start_extension_config"))
        assert "ots.CONFIG_PATH" in start and "start_config" in start

    def test_start_sets_timestamp_from_to_the_new_version(self):
        body = ast.unparse(_func(LIB_SRC / "_commit.py", "start_config"))
        assert "timestamp_from" in body and "plan.version" in body

    def test_revert_and_remove_stray_undo_a_started_extension(self):
        assert "unstart_extension" in _names(
            _func(LIB_SRC / "_commit.py", "revert_claimed")
        )
        for fn in ("_remove_stray_local", "_remove_stray_s3"):
            assert "unstart_extension" in _names(_func(LIB_SRC / "_recovery.py", fn))

    def test_state_machines_start_timestamping(self):
        path = REPO / "packages/ocfl-lib/tests/test_commit_stateful.py"
        text = path.read_text()
        assert "def start_timestamping" in text
        assert "LocalStartTimestamping" in text and "S3StartTimestamping" in text


class TestCommitChecksContentProofs:
    def test_content_proofs_checked_before_plan_on_both_backends(self):
        assert "_check_content_proofs" in _names(
            _func(LIB_SRC / "_commit.py", "_require_ots_admissible")
        )
        for fn in ("_commit_local", "_commit_s3"):
            body = ast.unparse(_func(LIB_SRC / "_commit.py", fn))
            check = body.index("_require_ots_admissible(fsys, t, base, content)")
            assert check < body.index("t.plan_file, t.plan.to_json()"), fn

    def test_refusals_name_the_spec_codes(self):
        body = ast.unparse(_func(LIB_SRC / "_commit.py", "_check_content_proofs"))
        assert "PendingProofError" in body and "E907" in body
        assert "InvalidProofError" in body and "E905" in body

    def test_validation_and_commit_share_one_proof_check(self):
        for path, fn in (
            (LIB_SRC / "_commit.py", "_check_content_proofs"),
            (LIB_SRC / "validation/_extension_ots.py", "_check_proof"),
        ):
            assert "verify_proof" in _names(_func(path, fn)), path
