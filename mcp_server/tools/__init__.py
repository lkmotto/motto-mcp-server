"""MCP tool modules for the Motto fleet.

Workers fill in the NotImplementedError stubs during Day-0 bootstrap.
"""

from .epics import (
    create_epic,
    dispatch_droid_for_epic,
    epic_status,
    pause_epic,
    kill_epic,
)

from .infra_sprawl import (
    list_all_services,
    find_orphans,
    archive_service,
    consolidation_audit,
)

__all__ = [
    "create_epic",
    "dispatch_droid_for_epic",
    "epic_status",
    "pause_epic",
    "kill_epic",
    "list_all_services",
    "find_orphans",
    "archive_service",
    "consolidation_audit",
]
