"""Commit -> crash -> recover, as a hypothesis state machine (ADR-0001).

A writer commits versions and may die at any write (`fail_at`), and may
die again while recovering. Between steps the object's root inventory is
always the last published one or the pending plan's, never anything else.
Once recovered, the root validates clean, with no work-dir leftovers.
Runs on local disk and on moto S3.
"""

import shutil
import tempfile
from pathlib import Path

import boto3
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
from moto import mock_aws

from ocfl_lib import (
    CommitPlan,
    CommitState,
    ContentSource,
    DigestAlgorithm,
    LocalFS,
    RecoveryAction,
    RevertCompletedError,
    Spec,
    StorageRoot,
    get_layout,
    scan_dir,
)
from ocfl_lib.validation import validate_root
from tests.helpers.faults import Crash, fail_at

OID = "ark:/1/stateful"
BUCKET = "commit-stateful"
FILES = st.dictionaries(
    st.sampled_from(["a.txt", "b.txt", "d/c.txt"]),
    st.binary(max_size=4),
    min_size=1,
    max_size=3,
)
CRASH_AT = st.none() | st.integers(min_value=1, max_value=60)


class CommitCrashRules:
    """Rules shared by the local and the S3 machine."""

    fs: LocalFS
    root: StorageRoot
    tmp: Path
    published: bytes | None  # root inventory bytes of the last published head
    pending: CommitPlan | None  # a commit that died part-way
    content: ContentSource | None
    n_src: int

    def make_fs(self):
        raise NotImplementedError

    @initialize()
    def init_root(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fs = self.make_fs()
        layout = get_layout("0004-hashed-n-tuple-storage-layout")
        StorageRoot.init(self.fs, self.fs, spec=Spec.V1_1, layout=layout)
        self.root = StorageRoot.open(self.fs)
        self.published = None
        self.pending = None
        self.content = None
        self.n_src = 0

    def _stage(self, files: dict[str, bytes]):
        self.n_src += 1
        name = f"src{self.n_src}"
        for p, b in files.items():
            f = self.tmp / name / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b)
        return scan_dir(LocalFS(self.tmp), name, DigestAlgorithm.SHA512)

    def _root_inventory(self) -> bytes | None:
        obj = self.root.get_object(OID)
        try:
            with self.fs.open_file(f"{obj.path}/inventory.json") as f:
                return f.read()
        except FileNotFoundError:
            return None

    @precondition(lambda self: self.pending is None)
    @rule(files=FILES, crash_at=CRASH_AT)
    def commit(self, files, crash_at):
        vs = self._stage(files)
        plan = self.root.plan(OID, vs)
        writer, _ = fail_at(self.fs, crash_at)
        try:
            self.root.commit(writer, OID, plan, content=vs.content_source)
        except Crash:
            self.pending = plan
            self.content = vs.content_source
            return
        self.published = plan.inventory.raw_bytes

    @precondition(lambda self: self.pending is not None)
    @rule(action=st.sampled_from([RecoveryAction.RESUME, RecoveryAction.REVERT]))
    def recover(self, action):
        assert self.pending is not None
        state = self.root.commit_status(OID).state
        try:
            self.root.recover(self.fs, OID, action, content=self.content)
        except RevertCompletedError:
            assert state in (CommitState.INVENTORY_PUBLISHED, CommitState.PUBLISHED)
            self.root.recover(self.fs, OID, RecoveryAction.RESUME)
        self._settle()

    @precondition(lambda self: self.pending is not None)
    @rule(
        action=st.sampled_from([RecoveryAction.RESUME, RecoveryAction.REVERT]),
        crash_at=st.integers(min_value=1, max_value=20),
    )
    def recover_and_die(self, action, crash_at):
        recoverer, _ = fail_at(self.fs, crash_at)
        try:
            self.root.recover(recoverer, OID, action, content=self.content)
        except Crash:
            return
        except RevertCompletedError:
            return
        self._settle()

    def _settle(self):
        """The pending commit is finished or undone; it must be clean now."""
        assert self.pending is not None
        assert self.root.commit_status(OID).state is CommitState.CLEAN
        current = self._root_inventory()
        assert current in (self.published, self.pending.inventory.raw_bytes)
        self.published = current
        self.pending = None
        self.content = None

    @invariant()
    def root_inventory_is_published_or_pending(self):
        current = self._root_inventory()
        allowed = {self.published}
        if self.pending is not None:
            allowed.add(self.pending.inventory.raw_bytes)
        assert current in allowed

    @invariant()
    def clean_when_nothing_pending(self):
        if self.pending is not None:
            return
        report = validate_root(self.fs, "")
        assert report.is_valid, [(f.code, f.path) for f in report.errors]
        assert not [f for f in report.findings if f.code.startswith("W016")]

    def teardown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class LocalCommitCrash(CommitCrashRules, RuleBasedStateMachine):
    def make_fs(self):
        d = self.tmp / "root"
        d.mkdir()
        return LocalFS(d)


class S3CommitCrash(CommitCrashRules, RuleBasedStateMachine):
    def make_fs(self):
        from ocfl_lib.fs.s3 import S3FS

        self._mock = mock_aws()
        self._mock.start()
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        return S3FS(BUCKET, client=client)

    def teardown(self):
        self._mock.stop()
        super().teardown()


def _settings(max_examples: int) -> settings:
    return settings(
        max_examples=max_examples,
        deadline=None,
        stateful_step_count=8,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )


TestLocalCommitCrash = LocalCommitCrash.TestCase
TestLocalCommitCrash.settings = _settings(40)
TestS3CommitCrash = S3CommitCrash.TestCase
TestS3CommitCrash.settings = _settings(12)
