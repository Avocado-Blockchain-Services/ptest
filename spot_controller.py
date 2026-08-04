"""Pure capacity policy and authenticated reconciliation for Spot workers."""

from __future__ import annotations


def scale_target(backlog: int, active_workers: int, idle_seconds: int,
                 max_workers: int, active_leases: int = 0) -> int:
    """Return the desired worker count without executing or inspecting tests."""
    if min(backlog, active_workers, idle_seconds, max_workers, active_leases) < 0:
        raise ValueError("capacity inputs must be non-negative")
    if max_workers == 0:
        return 0
    if active_leases:
        return min(max_workers, max(active_workers, 1))
    if backlog:
        return min(max_workers, max(backlog, 1))
    return 1 if active_workers and idle_seconds < 3600 else 0


def reconcile(adapter, max_workers: int, idle_seconds: int = 3600) -> int:
    """Read authenticated queue metrics and set only the Spot MIG target."""
    backlog, workers, leases = adapter.authenticated_metrics()
    target = scale_target(backlog, workers, idle_seconds, max_workers, leases)
    adapter.set_target(target)
    return target
