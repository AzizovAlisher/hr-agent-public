"""Action and sensitive-field authorization for durable workflow definitions.

``workflows.manage`` controls the automation surface itself; it must never act
as a privilege-escalation shortcut into the hiring domains that an automation
mutates or inspects.  This module is the single contract used by the HTTP
surface and by workers when they execute an immutable rule snapshot.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.models.user import User
from app.services.enterprise import require_field_access, require_permission


@dataclass(frozen=True, slots=True)
class WorkflowAccessRequirements:
    permissions: tuple[str, ...]
    fields: tuple[FieldClass, ...]


_ACTION_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "move_stage": ("applications.disposition",),
    "assign_owner": ("applications.write",),
    "set_due_date": ("applications.write",),
    "add_tag": ("candidates.write",),
    "create_task": ("interviews.manage",),
    "send_template": ("communications.send",),
    # Owner notifications are local, but still cause a user-visible external
    # effect.  Requiring communications.send prevents a workflows.manage-only
    # role from amplifying notifications through high-volume rules.
    "notify_owner": ("communications.send",),
}

_ACTION_FIELDS: dict[str, tuple[FieldClass, ...]] = {
    # Technical tasks are part of the structured-assessment surface in the
    # canonical HTTP policy, even before an answer/result exists.
    "create_task": (FieldClass.ASSESSMENT_RESULT,),
    "send_template": (FieldClass.CONTACT,),
}


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def workflow_access_requirements(
    *,
    conditions: Mapping[str, Any] | Any,
    actions: Sequence[Mapping[str, Any] | Any],
) -> WorkflowAccessRequirements:
    """Return deterministic underlying permissions for a rule definition."""

    permissions: set[str] = set()
    fields: set[FieldClass] = set()
    condition_values = _mapping(conditions)
    if any(condition_values.get(key) is not None for key in ("min_score", "max_score")):
        permissions.add("assessments.results")
        fields.add(FieldClass.ASSESSMENT_RESULT)
    for raw_action in actions:
        action = _mapping(raw_action)
        action_type = action.get("type")
        if not isinstance(action_type, str) or action_type not in _ACTION_PERMISSIONS:
            # Schema validation normally makes this unreachable.  Returning an
            # impossible permission is less safe than rejecting the snapshot.
            raise ValueError("Unsupported workflow action")
        permissions.update(_ACTION_PERMISSIONS[action_type])
        fields.update(_ACTION_FIELDS.get(action_type, ()))
    return WorkflowAccessRequirements(
        permissions=tuple(sorted(permissions)),
        fields=tuple(sorted(fields, key=lambda value: value.value)),
    )


async def require_workflow_definition_access(
    db: AsyncSession,
    user: User,
    *,
    conditions: Mapping[str, Any] | Any,
    actions: Sequence[Mapping[str, Any] | Any],
) -> WorkflowAccessRequirements:
    """Require every action permission and sensitive field for ``user``."""

    requirements = workflow_access_requirements(
        conditions=conditions,
        actions=actions,
    )
    for permission in requirements.permissions:
        await require_permission(db, user, permission)
    for field in requirements.fields:
        await require_field_access(db, user, field)
    return requirements
