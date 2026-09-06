"""Narrow runtime correction for stale task-fence snapshot preservation."""
from __future__ import annotations

from functools import wraps
from types import SimpleNamespace

from . import models
from . import stage3_models as wf


def install(repository_cls: type) -> None:
    """Keep already-observed task authority across the final fence lock refresh."""

    original = repository_cls.capture_task_fence
    if getattr(original, "_task_fence_snapshot_patch", False):
        return

    @wraps(original)
    def patched(
        self,
        *,
        execution_id,
        generation_id,
        task_id,
        at,
    ):
        observed_state_fence = None
        observed_membership_revision = None

        execution = self.session.get(wf.CommandExecution, execution_id)
        request = (
            self.session.get(wf.ServiceRequest, execution.request_id)
            if execution is not None
            else None
        )
        if request is not None and request.command_name != "record-cook-log":
            # The canonical capture method calls ``lock_task_currentness``, whose
            # PostgreSQL SELECT ... FOR UPDATE refresh uses ``populate_existing``.
            # Copy only authority this transaction already observed before that
            # refresh can silently rebind it to a concurrently committed winner.
            observed_state = self.session.identity_map.get(
                self.session.identity_key(
                    models.DishState, (generation_id, task_id)
                )
            )
            observed_membership = self.session.identity_map.get(
                self.session.identity_key(
                    models.TaskMembershipHead, (generation_id, task_id)
                )
            )
            if observed_state is not None:
                observed_state_fence = (
                    observed_state.dish_version,
                    observed_state.placement_version,
                    observed_state.catalog_version_id,
                )
            if observed_membership is not None:
                observed_membership_revision = observed_membership.membership_revision

        if observed_state_fence is None and observed_membership_revision is None:
            return original(
                self,
                execution_id=execution_id,
                generation_id=generation_id,
                task_id=task_id,
                at=at,
            )

        canonical_lock = self.lock_task_currentness
        no_instance_override = object()
        previous_override = self.__dict__.get(
            "lock_task_currentness", no_instance_override
        )

        def lock_with_observed_fence(*, generation_id, task_id):
            locked_state, locked_membership, catalog_version_id = canonical_lock(
                generation_id=generation_id,
                task_id=task_id,
            )
            if observed_state_fence is None:
                fence_state = locked_state
                fence_catalog_version_id = catalog_version_id
            else:
                (
                    observed_dish_version,
                    observed_placement_version,
                    observed_catalog_version_id,
                ) = observed_state_fence
                fence_state = SimpleNamespace(
                    archived_at=locked_state.archived_at,
                    dish_version=observed_dish_version,
                    placement_version=observed_placement_version,
                )
                fence_catalog_version_id = observed_catalog_version_id
            fence_membership = (
                locked_membership
                if observed_membership_revision is None
                else SimpleNamespace(
                    membership_revision=observed_membership_revision
                )
            )
            return fence_state, fence_membership, fence_catalog_version_id

        # Keep the canonical capture method responsible for lock ordering,
        # archive/run-revocation checks, immutable-row creation, and flushing.
        # Only its single currentness-read result is narrowed to the task versions
        # already observed by this transaction.
        self.lock_task_currentness = lock_with_observed_fence
        try:
            return original(
                self,
                execution_id=execution_id,
                generation_id=generation_id,
                task_id=task_id,
                at=at,
            )
        finally:
            if previous_override is no_instance_override:
                del self.lock_task_currentness
            else:
                self.lock_task_currentness = previous_override

    patched._task_fence_snapshot_patch = True
    repository_cls.capture_task_fence = patched
