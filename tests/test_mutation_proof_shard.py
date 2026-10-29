"""CI runs the mutation proof as N parallel shards, so the shards must cover it.

Running every shard has to be running every mutation exactly once: a mutation
that falls between shards is never applied, so the seam it guards loses its
proof without any job going red, and a mutation in two shards only wastes time.
These tests pin that on the real mutation list, for the shard counts CI could
use, as well as the refusal of a spec that names no share.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _harness():
    path = ROOT / "scripts" / "mutation-proof.py"
    spec = importlib.util.spec_from_file_location("mutation_proof_shard", path)
    module = importlib.util.module_from_spec(spec)
    # Dataclass annotations are resolved through the module's registered name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


H = _harness()


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 8, 12])
def test_the_shards_together_run_every_mutation_exactly_once(n):
    ids = [m.id for m in H.MUTATIONS]
    shards = [[m.id for m in H.shard(H.MUTATIONS, f"{i}/{n}")] for i in range(1, n + 1)]
    merged = [mid for share in shards for mid in share]
    assert sorted(merged) == sorted(ids)
    assert len(merged) == len(set(merged)) == len(ids)
    sizes = [len(share) for share in shards]
    assert max(sizes) - min(sizes) <= 1


def test_a_shard_is_every_nth_mutation_in_list_order():
    ids = [m.id for m in H.MUTATIONS]
    assert [m.id for m in H.shard(H.MUTATIONS, "2/4")] == ids[1::4]
    assert [m.id for m in H.shard(H.MUTATIONS, "1/1")] == ids


@pytest.mark.parametrize("spec", ["0/4", "5/4", "1/0", "4", "a/b", "1/4/2", "-1/4", " 1/4", "01/4"])
def test_a_spec_that_names_no_share_is_refused(spec):
    with pytest.raises(SystemExit):
        H.shard(H.MUTATIONS, spec)
