"""The behaviour golden leaves out what is not behaviour: timing, and the version that answered."""
from behaviour_252 import _without_volatile


def test_timing_is_dropped_and_the_version_blanked_at_every_depth():
    observed = {"server_version": "2.6.0a8", "timing_ms": {"total": 3.2},
                "trace": {"server_version": "2.6.0a8", "rows": [{"timing_ms": 1, "ref": "mem:1"}]}}
    assert _without_volatile(observed) == {
        "server_version": "<server version>",
        "trace": {"server_version": "<server version>", "rows": [{"ref": "mem:1"}]},
    }
