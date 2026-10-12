"""Architecture guards for the lib cleanup (docs/roadmap.md §2)."""

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

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB_SRC = REPO / "packages/ocfl-lib/src/ocfl_lib"
CLI_SRC = REPO / "packages/ocfl-cli/src/ocfl_cli"
INDEX_SRC = REPO / "packages/ocfl-index/src/ocfl_index"
FIXTURES = REPO / "tests/fixtures"


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


# --- 1. errors


class TestErrors:
    def _error_classes(self):
        import ocfl_lib

        base = ocfl_lib.OcflError
        seen = []
        stack = [base]
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
        """Every `raise X(...)` / `raise X` in lib src (FS backends excepted)
        names an OcflError subclass."""
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
        """Every error class is raised in lib src or is a base of one that is."""
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


# --- 2. public API

PUBLIC_MODULES = {
    "ocfl_lib",
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


# --- 3. Object reads; StorageRoot.open


class _BoomFS:
    def __getattr__(self, name):
        raise AssertionError(f"I/O in constructor: {name}")


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

        StorageRoot(_BoomFS())  # ty: ignore[invalid-argument-type]
        Object(_BoomFS(), "x")  # ty: ignore[invalid-argument-type]

    def test_open(self, tmp_path):
        from ocfl_lib import LocalFS, NotAnOcflRoot, Spec, StorageRoot

        with pytest.raises(NotAnOcflRoot):
            StorageRoot.open(LocalFS(tmp_path))
        fs = LocalFS(tmp_path)
        StorageRoot.init(fs, fs, spec=Spec.V1_0)
        root = StorageRoot.open(fs)
        assert root.spec is Spec.V1_0
        assert root.layout is None


# --- 4. Stage -> VersionState


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


# --- 5. JsonValue


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


# --- 6. ValidationReport


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

        good = FIXTURES / "1.1/good-objects/minimal_one_version_one_file"
        report = validate_object(LocalFS(good), "")
        assert isinstance(report, ValidationReport)
        assert report.is_valid
        bad = FIXTURES / "1.1/bad-objects/E001_extra_file_in_root"
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
        src = FIXTURES / "1.1/warn-objects/W005_id_not_uri"
        shutil.copytree(src, tmp_path / "a/obj")
        report = validate_root(fs, "")
        assert report.is_valid
        w = [f for f in report.findings if f.code.startswith("W005")]
        assert w, report.findings
        assert w[0].path == "a/obj"


# --- 7. layout registry


class TestLayoutRegistry:
    def test_default_frozen(self):
        from ocfl_lib import DEFAULT_LAYOUTS, LayoutRegistry

        assert not hasattr(LayoutRegistry, "register")
        with pytest.raises(
            (AttributeError, TypeError, dataclasses.FrozenInstanceError)
        ):
            DEFAULT_LAYOUTS.foo = 1  # type: ignore[attr-defined]

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


# --- 8. duplicates


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


# --- phase 3 (docs/roadmap.md §3, ADR-0001)


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


# --- 3.1 the plan records only the intent


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


# --- 3.4 StorageRoot.commit is the only write entry point

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


# --- 3.2 claim, then publish


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

    def test_work_dir_not_hidden_from_validation(self):
        """Tool conveniences never weaken the validator: W016 still fires."""
        from ocfl_lib._commit import WORK_DIR_EXTENSION

        text = (LIB_SRC / "validation/_root.py").read_text()
        assert WORK_DIR_EXTENSION not in text


# --- 3.3 recovery tables, one per backend


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
            def open_file(self, n):
                return inner.open_file(n)

            def read_dir(self, n):
                return inner.read_dir(n)

            def exists(self, n):
                return inner.exists(n)

        status, plan = detect(ReadOnly(), "", "obj", "x:1")  # ty: ignore[invalid-argument-type]
        assert plan is None and status.state.value == "clean"

    def test_recovery_reads_progress_from_storage_only(self):
        """No module records commit progress next to the plan."""
        text = (LIB_SRC / "_recovery.py").read_text() + (
            LIB_SRC / "_commit.py"
        ).read_text()
        for word in ("completed", "journal", "progress.json"):
            assert word not in text, word


# --- 3.5 purge follows the ADR-0001 order, refuses live or stray claims


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


# --- 3.6 ocfl recover OBJ --resume|--revert|--remove-stray VN


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


# --- 3.7 fail at write N; commit -> crash -> recover state machines


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


# --- phase 4 (docs/roadmap.md §4, ADR-0002)

OTS_SPEC = REPO / "docs/ext/NNNN-ots-timestamps.md"


# --- 4.1 the revised extension spec


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

    def test_activation_is_the_extension_directory(self):
        """Deleting config.json must not switch the checks off (E908)."""
        text = (LIB_SRC / "validation/_extension_ots.py").read_text()
        assert '"E908"' in text
        assert "if not fsys.exists(ext_dir):" in text
        assert 'fsys.exists(join_path(ext_dir, "config.json"))' not in text

    def test_extra_detail_still_renders(self):
        from ocfl_lib.validation import Finding, Severity

        f = Finding("E905", Severity.ERROR, None, "", {"proof": "x", "reason": "r"})
        assert f.render().endswith("(reason: r)")
