"""Provider seams of the recall and reconstruct paths (2.6).

Every stage of recall and reconstruct that a later version may exchange is
called through the provider installed in that stage's slot. The built-in
providers (cpersona/builtin_providers.py) wrap the functions that did the work
before the seams existed, and change nothing they return.

What no provider can change stays with the caller, the Core: the quality gate
and its rescue, autocut, the cut to the count, the reservation places, the
response assembly, and the checks at the end of this module, which every
provider's output passes before the Core uses it. A provider is handed the
stage's inputs and returns the stage's result; it is not handed the gate, the
count or the response.

Selection is an explicit allowlist. A slot's provider is named by an id the
allowlist holds, never by an import path, and nothing is discovered. A
selection the allowlist cannot satisfy -- an unknown slot or id, another major
contract version, an operation the Core calls that the provider does not
declare or does not have, a provider that generates text, is not
deterministic, or does not run in this process -- is refused when the
selection is resolved, which for the running server is at import, before the
first request.

A recall reads the installed set once, at its start, and uses it until it
returns; so does a reconstruct, whose candidate pool comes from a recall of its
own. Installing another set affects the calls that start after it.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

# The contract the Core calls providers under. A provider built for another
# major version is refused; a minor version may add optional operations only.
CONTRACT_MAJOR = 1
CONTRACT_MINOR = 0

BUILTIN = "builtin"


class ProviderConfigError(RuntimeError):
    """A provider selection the registry refuses. The message names the slot and the rule."""


class ProviderContractError(RuntimeError):
    """A provider returned something its slot's contract does not allow.

    Raised by the Core's checks below rather than used: a result that broke the
    contract is not a result the Core can repair into one that did not.
    """


@dataclass(frozen=True)
class Manifest:
    """What a provider says about itself; the registry checks it before the provider is used.

    `capabilities` names the operations the provider offers. `policy` is the
    version of the rules the provider applies, where it has one (the time cue's
    stages carry cue.POLICY).
    """

    provider_id: str
    slot: str
    role: str
    contract: tuple[int, int]
    capabilities: frozenset[str]
    deterministic: bool = True
    generative: bool = False
    locality: str = "in_process"
    policy: str | None = None

    def describe(self) -> dict:
        out: dict = {
            "provider_id": self.provider_id,
            "role": self.role,
            "contract": f"{self.contract[0]}.{self.contract[1]}",
        }
        if self.policy is not None:
            out["policy"] = self.policy
        return out


@dataclass(frozen=True)
class Slot:
    """One place in the pipeline a provider can fill.

    `role` names the kind of stage the slot holds; several slots can share one
    (three hold a CandidateGenerator). `operations` are the methods the Core
    calls on the slot's provider: a provider has to declare each in its
    manifest and have each as a callable.
    """

    name: str
    role: str
    operations: tuple[str, ...]


# The slots, in the order a recall reaches them. Three slots share the
# CandidateGenerator role: the ordinary arms' retrieval is one provider with
# its fusion (not separated at this stage), the block arm and the cue arm are
# others, and reconstruct reads its candidates through a fourth.
SLOTS: Mapping[str, Slot] = MappingProxyType(
    {
        s.name: s
        for s in (
            Slot("fusion", "CandidateGenerator+FusionStrategy", ("retrieve",)),
            Slot("scoring", "PriorFunction+FusionStrategy", ("score",)),
            Slot("block_candidates", "CandidateGenerator", ("reserved_rows",)),
            Slot("cue_interpreter", "CueInterpreter", ("parse", "recent_only")),
            Slot("envelope_planner", "EnvelopePlanner", ("period", "wider")),
            Slot("cue_candidates", "CandidateGenerator", ("search",)),
            Slot("prior", "PriorFunction", ("apply",)),
            Slot("evidence_selector", "EvidenceSelector", ("lift", "seats")),
            Slot("propagation_selector", "EvidenceSelector", ("seat",)),
            Slot("reconstruct_candidates", "CandidateGenerator", ("candidates",)),
            Slot("reconstructor", "Reconstructor", ("bundle", "walk", "structure", "allocate")),
        )
    }
)

Factory = Callable[[], object]
Allowlist = Mapping[str, Mapping[str, Factory]]


class Providers:
    """The resolved set: one provider per slot, read as attributes (`providers.fusion`)."""

    __slots__ = ("_by_slot", "manifests", "digest")

    def __init__(self, by_slot: Mapping[str, object]) -> None:
        self._by_slot = MappingProxyType(dict(by_slot))
        self.manifests = MappingProxyType({name: p.manifest for name, p in by_slot.items()})
        described = {name: m.describe() for name, m in sorted(self.manifests.items())}
        self.digest = hashlib.sha256(
            json.dumps(described, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]

    def __getattr__(self, name: str) -> object:
        try:
            return self._by_slot[name]
        except KeyError:
            raise AttributeError(name) from None

    def describe(self) -> dict:
        """Slot -> the provider's id, role, contract and policy; what a trace would record."""
        return {name: m.describe() for name, m in sorted(self.manifests.items())}


def _check_manifest(slot: Slot, provider_id: str, provider: object) -> None:
    manifest = getattr(provider, "manifest", None)
    if not isinstance(manifest, Manifest):
        raise ProviderConfigError(f"slot {slot.name}: provider {provider_id!r} has no manifest")
    if manifest.slot != slot.name:
        raise ProviderConfigError(
            f"slot {slot.name}: provider {provider_id!r} declares slot {manifest.slot!r}"
        )
    if manifest.provider_id != provider_id:
        raise ProviderConfigError(
            f"slot {slot.name}: allowlist id {provider_id!r} names a provider "
            f"that calls itself {manifest.provider_id!r}"
        )
    if manifest.contract[0] != CONTRACT_MAJOR:
        raise ProviderConfigError(
            f"slot {slot.name}: provider {provider_id!r} implements contract "
            f"{manifest.contract[0]}.x; the Core calls {CONTRACT_MAJOR}.x"
        )
    missing = [op for op in slot.operations if op not in manifest.capabilities]
    if missing:
        raise ProviderConfigError(
            f"slot {slot.name}: provider {provider_id!r} does not declare {missing}"
        )
    absent = [op for op in slot.operations if not callable(getattr(provider, op, None))]
    if absent:
        raise ProviderConfigError(
            f"slot {slot.name}: provider {provider_id!r} declares but does not have {absent}"
        )
    # The line the design draws: a model that generates text is not called. A
    # provider that is not deterministic would break invariant 3 (same state,
    # same query, same bounds, same output), and one outside this process would
    # hide its cost and its failures from the request's budget.
    if manifest.generative:
        raise ProviderConfigError(f"slot {slot.name}: provider {provider_id!r} generates text")
    if not manifest.deterministic:
        raise ProviderConfigError(f"slot {slot.name}: provider {provider_id!r} is not deterministic")
    if manifest.locality != "in_process":
        raise ProviderConfigError(
            f"slot {slot.name}: provider {provider_id!r} runs {manifest.locality!r}; "
            "only in-process providers are accepted"
        )


def resolve(allowlist: Allowlist, selection: Mapping[str, str] | None = None) -> Providers:
    """Build and check the provider for every slot.

    `selection` maps a slot to a provider id; a slot it leaves out gets the
    built-in. Raises ProviderConfigError for anything the allowlist cannot
    satisfy -- nothing is skipped and nothing falls back.
    """
    selection = dict(selection or {})
    unknown = sorted(set(selection) - set(SLOTS))
    if unknown:
        raise ProviderConfigError(f"unknown slot(s) {unknown}; the slots are {sorted(SLOTS)}")
    chosen: dict[str, object] = {}
    for name, slot in SLOTS.items():
        provider_id = selection.get(name, BUILTIN)
        factory = allowlist.get(name, {}).get(provider_id)
        if factory is None:
            raise ProviderConfigError(f"slot {name}: no provider {provider_id!r} in the allowlist")
        provider = factory()
        _check_manifest(slot, provider_id, provider)
        chosen[name] = provider
    return Providers(chosen)


_active: Providers | None = None


def active() -> Providers:
    """The installed set; the built-ins unless something else was installed."""
    global _active
    if _active is None:
        from . import builtin_providers

        _active = resolve(builtin_providers.ALLOWLIST)
    return _active


def install(providers: Providers) -> Providers:
    """Install `providers` for requests that start from now on; returns the set it replaced."""
    global _active
    previous = active()
    _active = providers
    return previous


# ---------------------------------------------------------------------------
# The Core's checks. Each holds one contract a slot's provider could otherwise
# break without anything downstream noticing; each raises ProviderContractError.
# ---------------------------------------------------------------------------


def check_reorder(stage: str, before: Iterable[dict], after: list[dict]) -> None:
    """`after` holds exactly the rows of `before` (the same objects), in any order.

    The prior and the cue's bounded move order what the gate admitted; neither
    admits, removes, duplicates or replaces a row.
    """
    if sorted(map(id, before)) != sorted(map(id, after)):
        raise ProviderContractError(f"{stage}: the rows returned are not the rows it was given")


def check_lift(before: list[dict], after: list[dict], bound: int) -> None:
    """The cue's move reorders the returned rows and moves none up more than `bound` places."""
    check_reorder("evidence_selector.lift", before, after)
    was = {id(row): p for p, row in enumerate(before)}
    for q, row in enumerate(after):
        if was[id(row)] - q > bound:
            raise ProviderContractError(
                f"evidence_selector.lift: a row moved up {was[id(row)] - q} places; the bound is {bound}"
            )


def check_seats(
    seated: list[dict], eligible: list[dict], places: int, stage: str = "evidence_selector.seats"
) -> None:
    """At most `places` seats, each a row the Core found eligible, none twice."""
    allowed = {id(row) for row in eligible}
    if len(seated) > places:
        raise ProviderContractError(f"{stage}: {len(seated)} seats filled; {places} are held")
    if any(id(row) not in allowed for row in seated) or len({id(r) for r in seated}) != len(seated):
        raise ProviderContractError(f"{stage}: a seat went to a row that is not eligible")


def check_recall_count(returned: int, limit: int, seats: int, reservation: int) -> None:
    """recall: returned rows <= limit + the held seats + the block reservation.

    The held seats are the cue's, plus the propagation seat's when it is on.
    The reserved places sit beside the count rather than inside it, so a bound
    of `limit` alone would refuse a correct answer that filled them.
    """
    if returned > limit + seats + reservation:
        raise ProviderContractError(
            f"recall returned {returned} rows; the most it may is {limit} + {seats} + {reservation}"
        )


def check_walk(reached: list, chosen: list, reachable: Mapping) -> None:
    """One walk result per chosen cluster, and every walked record one the graph read holds."""
    if len(reached) != len(chosen):
        raise ProviderContractError(
            f"reconstructor.walk: {len(reached)} results for {len(chosen)} clusters"
        )
    for walked in reached:
        for ref, *_ in walked:
            if ref not in reachable:
                raise ProviderContractError(f"reconstructor.walk: {ref!r} is not a record the walk read")


def check_structure(item: dict, refs: set[str]) -> None:
    """An item's head and claims are records it was built from (invariant 5, design A04)."""
    named = [item.get("head_ref"), *(claim.get("ref") for claim in item.get("claims", []))]
    stray = [ref for ref in named if ref not in refs]
    if stray:
        raise ProviderContractError(f"reconstructor.structure: {stray} are not the item's records")


def check_allocation(entries: list, items: list[dict], used: int, budget: int, cut: bool) -> None:
    """The budget chooses a prefix and nothing else (invariant 9).

    Items are the first entries, in order, each quoting its own head and a
    prefix of its own excerpts; `used` is what they carry; it is within the
    budget unless only the first head is carried (it is always admitted); and
    `cut` says whether an item was left out.
    """
    if len(items) > len(entries):
        raise ProviderContractError("reconstructor.allocate: more items than entries")
    carried = 0
    for (item, head, others), out in zip(entries, items):
        if out.get("head_ref") != item.get("head_ref") or out.get("content") != head["content"]:
            raise ProviderContractError(
                f"reconstructor.allocate: item for {item.get('head_ref')!r} is not its entry's head"
            )
        excerpts = out.get("excerpts", [])
        if excerpts != others[: len(excerpts)]:
            raise ProviderContractError(
                f"reconstructor.allocate: excerpts of {item.get('head_ref')!r} are not a prefix of its own"
            )
        carried += len(head["content"]) + sum(len(e["content"]) for e in excerpts)
    if used != carried:
        raise ProviderContractError(f"reconstructor.allocate: reports {used} characters, carries {carried}")
    first_head_only = len(items) == 1 and not items[0].get("excerpts")
    if used > budget and not first_head_only:
        raise ProviderContractError(f"reconstructor.allocate: carries {used} characters over a budget of {budget}")
    if cut != (len(items) < len(entries)):
        raise ProviderContractError("reconstructor.allocate: the cut flag disagrees with the items returned")


def check_reconstruct_count(returned: int, reserved: int, effective: int, reservation: int) -> None:
    """reconstruct: 0 <= returned - reserved <= effective, 0 <= reserved <= reservation.

    Held items follow the window rather than taking places in it (the
    Reconstruction Window, docs/RELIABLE_RECALL_2_6.md section 7).
    """
    if not (0 <= returned - reserved <= effective and 0 <= reserved <= reservation):
        raise ProviderContractError(
            f"reconstruct returned {returned} items with {reserved} reserved; "
            f"the window is {effective} and the reservation {reservation}"
        )
