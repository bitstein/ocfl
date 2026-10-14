import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from msgspec.structs import replace

from ocfl_cli.errors import PhaseError
from ocfl_cli.model.ots_state import OtsState, StagedInventory
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import plan_path
from ocfl_cli.workflow.commit import (
    build_inventory_plan,
    load_plan,
    require_staged_metadata,
    version_user_from_staged,
)
from ocfl_cli.workflow.common import (
    CarriedProofs,
    StateMap,
    carried_proofs,
    place_companions,
    proof_cache_ready,
    proof_ready,
    require_verified_content_before_inventory,
    required_content,
    update_head_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli.model.metadata import StagedMetadata
    from ocfl_cli.store.staging import StagingStore
    from ocfl_cli.workflow.batch.reasons import FixedBatchReason
    from ocfl_lib import CommitPlan, StorageRoot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Area:
    store: StagingStore
    sr: StorageRoot
    root_path: str
    object_id: str
    staging_dir: Path


@dataclass(frozen=True, slots=True)
class Progress:
    unverified: tuple[str, ...] = ()
    unrequested: tuple[str, ...] = ()

    @property
    def done(self) -> bool:
        return not self.unverified and not self.unrequested


class TargetRules:
    target: TimestampTarget
    pending_phase: Phase
    upgraded_phase: Phase
    commits: bool
    not_ready_reason: FixedBatchReason

    def prepare(
        self, area: Area, state: OtsState, *, allow_missing_metadata: bool
    ) -> tuple[OtsState, tuple[str, ...]]:
        raise NotImplementedError

    def check_upgradable(self, state: OtsState) -> None: ...

    def in_scope(self, state: OtsState, digest: str) -> bool:
        raise NotImplementedError

    def reverify_digests(self, state: OtsState) -> tuple[str, ...]:
        return tuple(
            d for d, p in state.proofs.items() if p.target is self.target and p.verified
        )

    def progress(self, area: Area, state: OtsState) -> Progress:
        raise NotImplementedError

    def row_upgraded(self, state: OtsState, digests: tuple[str, ...]) -> bool:
        return all(self.in_scope(state, d) and state.verified(d) for d in digests)

    def row_current(
        self, area: Area, state: OtsState, digests: tuple[str, ...]
    ) -> bool:
        return True


class _PerFileRules(TargetRules):
    target = TimestampTarget.CONTENT
    pending_phase = Phase.CONTENT_PENDING
    upgraded_phase = Phase.CONTENT_UPGRADED
    commits = False
    not_ready_reason = "no_new_digests"

    def prepare(
        self, area: Area, state: OtsState, *, allow_missing_metadata: bool
    ) -> tuple[OtsState, tuple[str, ...]]:
        carried = carried_proofs(area.sr, area.object_id)
        need = place_content_companions(area, state, carried)
        own = tuple(sorted(d for d in need if d not in carried))
        return replace(state, pending_digests=own), own

    def in_scope(self, state: OtsState, digest: str) -> bool:
        proof = state.proofs.get(digest)
        return proof is None or proof.target is self.target

    def progress(self, area: Area, state: OtsState) -> Progress:
        carried = carried_proofs(area.sr, area.object_id)
        need = place_content_companions(area, state, carried)
        return Progress(
            unverified=tuple(
                d
                for d in need
                if d in state.proofs
                and not proof_ready(state, area.staging_dir, carried, d)
            ),
            unrequested=tuple(
                d for d in need if d not in state.proofs and d not in carried
            ),
        )


class _InventoryRules(TargetRules):
    target = TimestampTarget.INVENTORY
    pending_phase = Phase.INVENTORY_PENDING
    upgraded_phase = Phase.INVENTORY_UPGRADED
    commits = True
    not_ready_reason = "content_not_upgraded"

    def prepare(
        self, area: Area, state: OtsState, *, allow_missing_metadata: bool
    ) -> tuple[OtsState, tuple[str, ...]]:
        if state.phase is Phase.CONTENT_UPGRADED:
            require_verified_content_before_inventory(state)
        metadata = area.store.load_metadata(area.root_path, area.object_id)
        require_staged_metadata(metadata, allow_missing_metadata=allow_missing_metadata)
        plan = build_reusable_inventory_plan(area, state, metadata)
        digest = plan.inventory.digest
        existing = state.inventory
        staged = StagedInventory(
            version=plan.version,
            digest=digest,
            job_id=existing.job_id
            if existing is not None and existing.digest == digest
            else None,
        )
        # The plan is the intent: written before the digest is sent, so a
        # crash leaves the plan its submission names.
        plan_path(area.staging_dir).write_bytes(plan.to_json())
        return replace(state, inventory=staged), (digest,)

    def check_upgradable(self, state: OtsState) -> None:
        if state.inventory is None:
            raise PhaseError(f"staged inventory missing in phase {state.phase.value!r}")

    def in_scope(self, state: OtsState, digest: str) -> bool:
        return state.inventory is not None and state.inventory.digest == digest

    def reverify_digests(self, state: OtsState) -> tuple[str, ...]:
        if state.inventory is None or not state.verified(state.inventory.digest):
            return ()
        return (state.inventory.digest,)

    def progress(self, area: Area, state: OtsState) -> Progress:
        if state.inventory is None:
            return Progress()
        d = state.inventory.digest
        if d not in state.proofs:
            return Progress(unrequested=(d,))
        if not proof_cache_ready(state, area.staging_dir, d):
            return Progress(unverified=(d,))
        return Progress()

    def row_current(
        self, area: Area, state: OtsState, digests: tuple[str, ...]
    ) -> bool:
        if state.inventory is None or state.inventory.digest not in digests:
            return False
        prior = load_plan(area.staging_dir)
        if prior is None:
            return False
        prior_inv = prior.inventory.inventory
        metadata = area.store.load_metadata(area.root_path, area.object_id)
        plan, _content_source = build_inventory_plan(
            area.store,
            area.sr,
            area.root_path,
            area.object_id,
            message=metadata.message,
            user=version_user_from_staged(metadata),
            created=prior_inv.versions[prior_inv.head].created,
        )
        if plan.inventory.digest != state.inventory.digest:
            return False
        plan_path(area.staging_dir).write_bytes(plan.to_json())
        return True


CONTENT: TargetRules = _PerFileRules()
INVENTORY: TargetRules = _InventoryRules()
ALL: tuple[TargetRules, ...] = (CONTENT, INVENTORY)
_RULES: dict[TimestampTarget, TargetRules] = {
    TimestampTarget.CONTENT: CONTENT,
    TimestampTarget.INVENTORY: INVENTORY,
}


def rules_for(target: TimestampTarget) -> TargetRules:
    return _RULES[target]


def place_content_companions(
    area: Area, state: OtsState, carried: CarriedProofs
) -> dict[str, list[str]]:
    from ocfl_lib import DigestAlgorithm

    inv = area.store.load_inventory(area.root_path, area.object_id)
    head_state: StateMap = {
        d: list(ps) for d, ps in inv.versions[inv.head].state.items()
    }
    need = required_content(head_state)
    content_map = area.store.load_content_map(area.root_path, area.object_id)
    if place_companions(
        head_state,
        need,
        state,
        carried,
        staging_dir=area.staging_dir,
        content_map=content_map,
        alg=DigestAlgorithm.from_ocfl_name(inv.digest_algorithm),
    ):
        area.store.save_content_map(area.root_path, area.object_id, content_map)
        area.store.save_inventory(
            area.root_path, area.object_id, update_head_state(inv, head_state)
        )
    return need


def build_reusable_inventory_plan(
    area: Area, state: OtsState, metadata: StagedMetadata
) -> CommitPlan:
    prior = load_plan(area.staging_dir)
    prior_created = None
    if prior is not None:
        prior_inv = prior.inventory.inventory
        prior_created = prior_inv.versions[prior_inv.head].created

    def build(created: str | None) -> CommitPlan:
        plan, _cs = build_inventory_plan(
            area.store,
            area.sr,
            area.root_path,
            area.object_id,
            message=metadata.message,
            user=version_user_from_staged(metadata),
            created=created,
        )
        return plan

    plan = build(prior_created)
    if prior_created is None or plan.inventory.digest in state.proofs:
        return plan
    return build(None)
