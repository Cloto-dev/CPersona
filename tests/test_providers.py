"""The provider seams of recall and reconstruct (cpersona/providers.py).

Three things are pinned here. The registry refuses every selection its allowlist
cannot satisfy. Recall and reconstruct call each stage through the provider
installed in its slot -- not the function the built-in wraps -- and read the
installed set once per request. And a provider whose output breaks its slot's
contract is stopped by the Core's check before the output is used.

That the built-ins return exactly what the functions returned before the seams
is not tested here: tests/test_equivalence_252.py replays the behaviour matrix
recorded before them, cue, reservation, reconstruct and trace scenarios included.
"""
import dataclasses
import inspect
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cpersona import builtin_providers, config, cue, memory_handlers, providers, reconstruct
from cpersona.database import get_db

AGENT = "agent.providers"
NOW = datetime.now(timezone.utc)
QUERY = "harbor lighthouse keeper logbook"
CORPUS = [(f"harbor lighthouse keeper logbook entry {i}", 10 * i + 1) for i in range(12)]
SEAT_CORPUS = [(f"{QUERY} {d}", d) for d in range(1, 7)] + [
    (f"harbor lighthouse note {i}", 100 + 5 * i) for i in range(3)
]


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _period(after_days: float, before_days: float, confidence: str = "sure") -> dict:
    return {"after": _ts(after_days)[:10], "before": _ts(before_days)[:10], "confidence": confidence}


async def _seed(rows, agent=AGENT):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()
    for content, days_ago in rows:
        await memory_handlers.do_store(
            agent, {"content": content, "source": {"System": "test"}, "timestamp": _ts(days_ago)}
        )


@pytest.fixture
def installed():
    """Install a provider set for one test and put the previous one back."""
    previous = providers.active()
    yield providers.install
    providers.install(previous)


def _allowlist_with(slot: str, provider_id: str, cls) -> dict:
    allow = {name: dict(ids) for name, ids in builtin_providers.ALLOWLIST.items()}
    allow[slot][provider_id] = cls
    return allow


def _variant(slot_name: str, provider_id: str = "variant", **overrides):
    """A subclass of the slot's built-in whose manifest names `provider_id`, with methods replaced."""
    base = builtin_providers.ALLOWLIST[slot_name][providers.BUILTIN]
    manifest_fields = {k: overrides.pop(k) for k in list(overrides) if k in {f.name for f in dataclasses.fields(providers.Manifest)}}
    ns = {"manifest": dataclasses.replace(base.manifest, provider_id=provider_id, **manifest_fields)}
    ns.update(overrides)
    return type(f"Variant_{slot_name}", (base,), ns)


def _with(slot: str, **overrides) -> providers.Providers:
    cls = _variant(slot, **overrides)
    return providers.resolve(_allowlist_with(slot, "variant", cls), {slot: "variant"})


# --- the registry -----------------------------------------------------------------------


def test_the_default_set_is_the_builtin_in_every_slot():
    active = providers.active()
    assert set(active.manifests) == set(providers.SLOTS)
    assert {m.provider_id for m in active.manifests.values()} == {providers.BUILTIN}
    for name, slot in providers.SLOTS.items():
        provider = getattr(active, name)
        assert provider.manifest.slot == name
        assert set(slot.operations) <= provider.manifest.capabilities
    # The time cue's stages carry the cue policy they apply.
    cue_slots = {"cue_interpreter", "envelope_planner", "cue_candidates", "evidence_selector"}
    assert {n for n, m in active.manifests.items() if m.policy == cue.POLICY} == cue_slots


def test_the_digest_names_the_set_and_changes_with_it():
    builtin = providers.resolve(builtin_providers.ALLOWLIST)
    assert builtin.digest == providers.active().digest
    assert _with("prior").digest != builtin.digest


@pytest.mark.parametrize("selection, message", [
    ({"fusions": "builtin"}, r"unknown slot\(s\) \['fusions'\]"),
    ({"fusion": "rrf-v2"}, "slot fusion: no provider 'rrf-v2' in the allowlist"),
    # An import path is an id like any other, and the allowlist holds none.
    ({"prior": "cpersona.builtin_providers:Prior"}, "slot prior: no provider"),
])
def test_a_selection_the_allowlist_does_not_hold_is_refused(selection, message):
    with pytest.raises(providers.ProviderConfigError, match=message):
        providers.resolve(builtin_providers.ALLOWLIST, selection)


def test_a_slot_missing_from_the_allowlist_is_refused():
    allow = {name: ids for name, ids in builtin_providers.ALLOWLIST.items() if name != "scoring"}
    with pytest.raises(providers.ProviderConfigError, match="slot scoring: no provider 'builtin'"):
        providers.resolve(allow)


@pytest.mark.parametrize("overrides, message", [
    ({"contract": (providers.CONTRACT_MAJOR + 1, 0)}, r"implements contract 2\.x; the Core calls 1\.x"),
    ({"capabilities": frozenset({"lift"})}, r"does not declare \['seats'\]"),
    ({"slot": "prior"}, "declares slot 'prior'"),
    ({"generative": True}, "generates text"),
    ({"deterministic": False}, "is not deterministic"),
    ({"locality": "broker"}, "runs 'broker'; only in-process providers are accepted"),
])
def test_a_manifest_the_core_cannot_call_is_refused(overrides, message):
    cls = _variant("evidence_selector", **overrides)
    with pytest.raises(providers.ProviderConfigError, match=f"slot evidence_selector: provider 'variant' {message}"):
        providers.resolve(_allowlist_with("evidence_selector", "variant", cls), {"evidence_selector": "variant"})


def test_a_minor_contract_version_is_accepted():
    cls = _variant("prior", contract=(providers.CONTRACT_MAJOR, providers.CONTRACT_MINOR + 3))
    assert providers.resolve(_allowlist_with("prior", "variant", cls), {"prior": "variant"}).prior.manifest.contract[1] == 3


def test_a_manifest_describes_its_contract_as_major_dot_minor():
    """What a traced recall records for each slot (trace.providers)."""
    cls = _variant("prior", contract=(providers.CONTRACT_MAJOR, 7), policy="p-1")
    described = providers.resolve(_allowlist_with("prior", "variant", cls), {"prior": "variant"}).manifests["prior"]
    assert described.describe() == {"provider_id": "variant", "role": "PriorFunction", "contract": "1.7", "policy": "p-1"}
    assert providers.active().manifests["prior"].describe() == {
        "provider_id": "builtin", "role": "PriorFunction", "contract": "1.0",
    }


def test_a_declared_operation_the_provider_does_not_have_is_refused():
    cls = _variant("evidence_selector", seats=None)
    with pytest.raises(providers.ProviderConfigError, match=r"declares but does not have \['seats'\]"):
        providers.resolve(_allowlist_with("evidence_selector", "variant", cls), {"evidence_selector": "variant"})


def test_an_allowlist_id_must_be_the_name_the_provider_gives_itself():
    cls = _variant("prior", provider_id="someone-else")
    with pytest.raises(providers.ProviderConfigError, match="names a provider that calls itself 'someone-else'"):
        providers.resolve(_allowlist_with("prior", "variant", cls), {"prior": "variant"})


def test_a_provider_without_a_manifest_is_refused():
    class Bare:
        def apply(self, results, span, now):
            return results

    with pytest.raises(providers.ProviderConfigError, match="slot prior: provider 'bare' has no manifest"):
        providers.resolve(_allowlist_with("prior", "bare", Bare), {"prior": "bare"})


# --- every stage is called through its slot --------------------------------------------


def _spy_set(calls: dict, entry: list) -> providers.Providers:
    """Every slot filled by its built-in, recording each operation the Core calls.

    Calls are recorded under the entry point that is running (`entry[0]`), so an
    operation reconstruct also reaches through its candidate recall cannot stand
    in for the call recall itself makes.
    """
    allow = {name: dict(ids) for name, ids in builtin_providers.ALLOWLIST.items()}
    for name, slot in providers.SLOTS.items():
        base = allow[name][providers.BUILTIN]
        ns = {"manifest": dataclasses.replace(base.manifest, provider_id="spy")}
        for op in slot.operations:
            fn = getattr(base, op)
            if inspect.iscoroutinefunction(fn):
                async def wrapper(self, *a, _fn=fn, _key=(name, op), **kw):
                    calls[entry[0]].append(_key)
                    return await _fn(self, *a, **kw)
            else:
                def wrapper(self, *a, _fn=fn, _key=(name, op), **kw):
                    calls[entry[0]].append(_key)
                    return _fn(self, *a, **kw)
            ns[op] = wrapper
        allow[name]["spy"] = type(f"Spy_{name}", (base,), ns)
    return providers.resolve(allow, {name: "spy" for name in providers.SLOTS})


@pytest_asyncio.fixture
async def spied(fake_embedding_client, monkeypatch, installed):
    """Run recall and reconstruct over paths that reach every operation, recording the calls."""
    calls: dict = {"recall": [], "reconstruct": []}
    entry = ["recall"]
    installed(_spy_set(calls, entry))
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    # The block arm runs whenever retrieval is on and a query vector exists; with
    # no block built its search is empty, which is enough to see it called.
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    await _seed(SEAT_CORPUS)
    seat = await memory_handlers.do_recall(AGENT, QUERY, limit=2, time_cue=_period(115, 95))
    assert seat["time_cue"]["seated"] == 1
    await _seed(CORPUS)
    # Days 3 to 8 hold nothing, so the loop asks for the next width.
    widened = await memory_handlers.do_recall(AGENT, QUERY, limit=5, time_cue=_period(8, 3))
    assert widened["time_cue"]["revised"] is True
    entry[0] = "reconstruct"
    out = await reconstruct.do_reconstruct(AGENT, QUERY, count=3)
    assert out["returned_count"] == 3
    return calls


_RECONSTRUCT_SLOTS = {"reconstruct_candidates", "reconstructor"}


@pytest.mark.asyncio
@pytest.mark.parametrize("slot, op", [(n, op) for n, s in providers.SLOTS.items() for op in s.operations])
async def test_the_core_calls_each_operation_through_its_slot(spied, slot, op):
    assert (slot, op) in spied["reconstruct" if slot in _RECONSTRUCT_SLOTS else "recall"]


@pytest.mark.asyncio
async def test_reconstruct_reads_the_cue_through_the_installed_interpreter(fake_embedding_client, installed):
    """Reconstruct refuses a cue itself before its candidate recall runs (which would
    refuse it too, but as an empty pool rather than an error)."""
    await _seed(CORPUS)

    def refuse(self, raw):
        if raw is None:
            return None
        raise cue.TimeCueError("refused by the installed interpreter")

    installed(_with("cue_interpreter", parse=refuse))
    out = await reconstruct.do_reconstruct(AGENT, QUERY, count=3, time_cue=_period(75, 46))
    assert out.get("error") == "refused by the installed interpreter"
    assert out["items"] == [] and out["returned_count"] == 0


@pytest.mark.asyncio
async def test_a_request_keeps_the_set_it_started_with(fake_embedding_client, monkeypatch, installed):
    await _seed(CORPUS)

    async def failing(self, *a, **kw):
        raise AssertionError("the scoring of a set installed mid-request was used")

    later = _with("scoring", score=failing)

    async def installs_mid_request(self, db, **kw):
        providers.install(later)
        return await builtin_providers.Fusion.retrieve(self, db, **kw)

    installed(_with("fusion", retrieve=installs_mid_request))
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=3)
    assert len(out["messages"]) == 3
    # The set it installed applies from the next request.
    with pytest.raises(AssertionError, match="installed mid-request"):
        await memory_handlers.do_recall(AGENT, QUERY, limit=3)


@pytest.mark.asyncio
async def test_the_set_is_read_before_the_cue_and_kept_after_it(fake_embedding_client, installed):
    await _seed(CORPUS)

    async def failing(self, *a, **kw):
        raise AssertionError("the retrieval of a set installed while reading the cue was used")

    later = _with("fusion", retrieve=failing)

    def installs_while_parsing(self, raw):
        providers.install(later)
        return cue.parse(raw)

    installed(_with("cue_interpreter", parse=installs_while_parsing))
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=3, time_cue=_period(75, 46))
    # Three rows, plus the cue's seat when it fills one.
    assert 3 <= len(out["messages"]) <= 4 and out["time_cue"]["policy"] == cue.POLICY


# --- a provider that breaks its contract is stopped ------------------------------------


def _reverse_lift(self, admitted, cue_rank, bound, rid_of):
    return list(reversed(admitted)), []


def _drop_last(self, admitted, cue_rank, bound, rid_of):
    return admitted[:-1], []


@pytest.mark.asyncio
@pytest.mark.parametrize("lift, message", [
    (_reverse_lift, r"a row moved up 11 places; the bound is 3"),
    (_drop_last, "the rows returned are not the rows it was given"),
])
async def test_a_move_past_its_bound_or_out_of_its_rows_is_stopped(fake_embedding_client, installed, lift, message):
    await _seed(CORPUS)
    installed(_with("evidence_selector", lift=lift))
    with pytest.raises(providers.ProviderContractError, match=message):
        await memory_handlers.do_recall(AGENT, QUERY, limit=12, time_cue=_period(75, 46))


@pytest.mark.asyncio
@pytest.mark.parametrize("seats, message", [
    (lambda self, eligible, places: eligible[:places + 1], "2 seats filled; 1 are held"),
    (lambda self, eligible, places: [dict(eligible[0])], "a seat went to a row that is not eligible"),
])
async def test_a_seat_beyond_the_held_one_or_for_another_row_is_stopped(
    fake_embedding_client, monkeypatch, installed, seats, message
):
    await _seed(SEAT_CORPUS)
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    installed(_with("evidence_selector", seats=seats))
    with pytest.raises(providers.ProviderContractError, match=message):
        await memory_handlers.do_recall(AGENT, QUERY, limit=2, time_cue=_period(115, 95))


@pytest.mark.asyncio
async def test_a_prior_that_removes_a_row_is_stopped(fake_embedding_client, installed):
    await _seed(CORPUS)
    installed(_with("prior", apply=lambda self, results, span, now: results[1:]))
    with pytest.raises(providers.ProviderContractError, match="prior.apply: the rows returned"):
        await memory_handlers.do_recall(AGENT, QUERY, limit=5)


def _stray_head(self, rows, why_by_ref, spans, max_evidence, extra, links):
    item, dropped = reconstruct.structure(rows, why_by_ref, spans, max_evidence, extra, links)
    return {**item, "head_ref": "mem:999999999"}, dropped


def _stray_walk(self, chosen, candidates, graph, max_hops):
    reached, cuts = reconstruct.walk(chosen, candidates, graph, max_hops)
    return [[("mem:999999999", "walked", 1)], *reached[1:]], cuts


def _short_walk(self, chosen, candidates, graph, max_hops):
    reached, cuts = reconstruct.walk(chosen, candidates, graph, max_hops)
    return reached[:-1], cuts


def _skip_second(self, entries, budget):
    items, used, cut = reconstruct.allocate(entries, budget)
    return [items[0], *items[2:]], used, cut


def _over_budget(self, entries, budget):
    return reconstruct.allocate(entries, budget * 100)


def _wrong_usage(self, entries, budget):
    items, used, cut = reconstruct.allocate(entries, budget)
    return items, used - 1, cut


# Four records long enough that two heads fill a budget of 600 characters.
LONG_CORPUS = [(f"{QUERY} {i} " + f"{QUERY} " * 12, 10 * i + 1) for i in range(4)]


@pytest.mark.asyncio
@pytest.mark.parametrize("op, fn, message", [
    ("structure", _stray_head, r"reconstructor.structure: \['mem:999999999'\] are not the item's records"),
    ("walk", _stray_walk, "reconstructor.walk: 'mem:999999999' is not a record the walk read"),
    ("walk", _short_walk, "reconstructor.walk: 2 results for 3 clusters"),
    ("allocate", _skip_second, "is not its entry's head"),
    ("allocate", _wrong_usage, "reports"),
])
async def test_a_reconstructor_that_breaks_its_contract_is_stopped(fake_embedding_client, installed, op, fn, message):
    await _seed(CORPUS)
    installed(_with("reconstructor", **{op: fn}))
    with pytest.raises(providers.ProviderContractError, match=message):
        await reconstruct.do_reconstruct(AGENT, QUERY, count=3)


@pytest.mark.asyncio
async def test_an_allocation_past_the_budget_is_stopped(fake_embedding_client, installed):
    await _seed(LONG_CORPUS)
    within = await reconstruct.do_reconstruct(AGENT, QUERY, count=4, budget=600)
    assert within["returned_count"] < 4, "the budget must be what leaves items out"
    installed(_with("reconstructor", allocate=_over_budget))
    with pytest.raises(providers.ProviderContractError, match="over a budget of 600"):
        await reconstruct.do_reconstruct(AGENT, QUERY, count=4, budget=600)


# --- the checks at their boundaries -------------------------------------------------------


def _rows(n):
    return [{"i": i} for i in range(n)]


def test_a_move_of_exactly_the_bound_passes_and_one_more_does_not():
    rows = _rows(5)
    providers.check_lift(rows, [rows[3], rows[0], rows[1], rows[2], rows[4]], 3)
    with pytest.raises(providers.ProviderContractError, match="moved up 4 places"):
        providers.check_lift(rows, [rows[4], rows[0], rows[1], rows[2], rows[3]], 3)


def test_a_copy_of_a_row_is_not_the_row():
    rows = _rows(3)
    with pytest.raises(providers.ProviderContractError):
        providers.check_reorder("prior.apply", rows, [rows[0], rows[1], dict(rows[2])])


def test_a_duplicated_row_is_not_a_reorder():
    rows = _rows(3)
    with pytest.raises(providers.ProviderContractError):
        providers.check_reorder("prior.apply", rows, [rows[0], rows[0], rows[1]])


def test_the_seat_count_and_eligibility():
    eligible = _rows(3)
    providers.check_seats(eligible[:1], eligible, 1)
    providers.check_seats([], eligible, 1)
    with pytest.raises(providers.ProviderContractError, match="not eligible"):
        providers.check_seats([eligible[0], eligible[0]], eligible, 2)


def test_the_recall_count_allows_the_reserved_places_beside_the_limit():
    providers.check_recall_count(5 + 1 + 2, 5, 1, 2)
    with pytest.raises(providers.ProviderContractError, match="returned 9 rows"):
        providers.check_recall_count(9, 5, 1, 2)


@pytest.mark.parametrize("returned, reserved, ok", [
    (3, 0, True), (6, 3, True), (0, 0, True),
    (4, 0, False),   # past the window
    (7, 4, False),   # more reserved than the reservation
    (2, 3, False),   # more reserved than returned
])
def test_the_reconstruct_count_separates_the_window_from_the_reservation(returned, reserved, ok):
    if ok:
        providers.check_reconstruct_count(returned, reserved, 3, 3)
    else:
        with pytest.raises(providers.ProviderContractError):
            providers.check_reconstruct_count(returned, reserved, 3, 3)


def _entry(ref, head, *others):
    return ({"head_ref": ref}, {"content": head}, [{"ref": f"{ref}-x{i}", "content": o} for i, o in enumerate(others)])


def test_an_allocation_is_a_prefix_and_carries_what_it_reports():
    entries = [_entry("mem:1", "a" * 10, "b" * 5), _entry("mem:2", "c" * 10)]
    items, used, cut = reconstruct.allocate(entries, 100)
    providers.check_allocation(entries, items, used, 100, cut)
    # The first head alone may exceed the budget; it is always admitted.
    items, used, cut = reconstruct.allocate(entries, 3)
    assert len(items) == 1 and used == 10 and cut
    providers.check_allocation(entries, items, used, 3, cut)
    with pytest.raises(providers.ProviderContractError, match="cut flag"):
        providers.check_allocation(entries, items, used, 3, False)
    full, used, _ = reconstruct.allocate(entries, 100)
    bad = [dict(full[0], excerpts=[{"ref": "mem:1-x0", "content": "z"}]), full[1]]
    with pytest.raises(providers.ProviderContractError, match="not a prefix of its own"):
        providers.check_allocation(entries, bad, used, 100, False)
