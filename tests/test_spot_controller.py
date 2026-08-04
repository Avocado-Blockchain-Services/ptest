import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_controller import scale_target


def test_idle_worker_is_kept_until_the_exact_idle_timeout():
    assert scale_target(0, 1, 3599, 5) == 1
    assert scale_target(0, 1, 3600, 5) == 0


def test_backlog_is_capped_at_max_workers():
    assert scale_target(99, 0, 0, 5) == 5


def test_active_lease_never_scales_in():
    assert scale_target(0, 3, 3600, 5, active_leases=1) == 3
