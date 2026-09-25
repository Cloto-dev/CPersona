"""The built-in providers: each recall and reconstruct stage as it was before the seams.

Each wraps one existing function and adds nothing to it. The function is looked
up on its module when the stage runs, not bound when this module is imported,
so a module attribute patched at run time -- `memory_handlers.RECALL_MODE`, a
stubbed `_recall_rsf`, `blocks.BLOCK_RESERVATION` -- reaches the stage exactly
as it did when the Core called the function directly. memory_handlers and
reconstruct are imported inside the methods because both import the provider
seams at module scope.
"""
from __future__ import annotations

from . import blocks, cue
from .providers import BUILTIN, CONTRACT_MAJOR, CONTRACT_MINOR, SLOTS, Manifest


def _manifest(slot: str, policy: str | None = None) -> Manifest:
    spec = SLOTS[slot]
    return Manifest(
        provider_id=BUILTIN,
        slot=slot,
        role=spec.role,
        contract=(CONTRACT_MAJOR, CONTRACT_MINOR),
        capabilities=frozenset(spec.operations),
        policy=policy,
    )


class Fusion:
    """The ordinary arms and their fusion: rrf, rsf, or the cascade (one provider at this stage)."""

    manifest = _manifest("fusion")

    async def retrieve(
        self, db, *, agent_id, query, depth, limit, deep, channel, exclude_set,
        project_id, source_id, query_vec_out, lexical_terms,
    ) -> list[dict]:
        from . import memory_handlers as mh

        # Passed only when there are terms, so a recall without them calls each
        # fusion path exactly as it always did.
        lexical = {"lexical_terms": lexical_terms} if lexical_terms else {}
        if mh.RECALL_MODE == "rrf" and query.strip():
            return await mh._recall_rrf(
                db, agent_id, query, depth, deep, channel, exclude_set,
                project_id=project_id, source_id=source_id,
                query_vec_out=query_vec_out, **lexical,
            )
        if mh.RECALL_MODE == "rsf" and query.strip():
            return await mh._recall_rsf(
                db, agent_id, query, depth, deep, channel, exclude_set,
                project_id=project_id, source_id=source_id,
                query_vec_out=query_vec_out, **lexical,
            )
        return await mh._recall_cascade(
            db, agent_id, query, limit, deep, channel, exclude_set,
            project_id=project_id, source_id=source_id,
            query_vec_out=query_vec_out, **lexical,
        )


class Scoring:
    """The episode-boundary penalty and the confidence score (`_apply_recall_scoring`)."""

    manifest = _manifest("scoring")

    async def score(self, db, agent_id, results, deep, *, project_id, channel, query):
        from . import memory_handlers as mh

        return await mh._apply_recall_scoring(
            db, agent_id, results, deep, project_id=project_id, channel=channel, query=query
        )


class BlockCandidates:
    """The block arm: the Hamming search and the hydration of the records it reached."""

    manifest = _manifest("block_candidates")

    async def reserved_rows(
        self, db, query_vec, *, agent_id, project_id, channel, source_id, exclude_set, limit
    ) -> list[dict]:
        from . import memory_handlers as mh

        hits = await blocks.search(
            db,
            query_vec,
            mh.isolation_where(agent_id=agent_id, project_id=project_id, channel=channel),
        )
        # Enough to survive every one of them already being in the result: the
        # reservation is a fixed number of places and is not derived from the
        # count, but how many candidates must be looked at to fill those places
        # does depend on how many rows the cut can hold.
        return await mh._block_reserved_rows(
            db,
            hits[: limit + blocks.BLOCK_RESERVATION],
            agent_id,
            project_id=project_id,
            channel=channel,
            source_id=source_id,
            exclude_set=exclude_set,
            wanted=limit + blocks.BLOCK_RESERVATION,
        )


class CueInterpreter:
    """Reads a `time_cue` argument, and says whether a cue points only at today."""

    manifest = _manifest("cue_interpreter", cue.POLICY)

    def parse(self, raw):
        return cue.parse(raw)

    def recent_only(self, time_cue, now, span) -> bool:
        return cue.recent_only(time_cue, now, span)


class EnvelopePlanner:
    """The period a cue points at under a confidence, and the one step wider."""

    manifest = _manifest("envelope_planner", cue.POLICY)

    def period(self, time_cue, confidence, now, span):
        return cue.period(time_cue, confidence, now, span)

    def wider(self, confidence: str) -> str | None:
        return cue.WIDER[confidence]


class CueCandidates:
    """The cue arm: the memories and episodes whose time falls in the period."""

    manifest = _manifest("cue_candidates", cue.POLICY)

    async def search(
        self, db, *, agent_id, query, depth, window, channel, project_id, source_id,
        exclude_set, query_vec, lexical_terms,
    ) -> list[dict]:
        from . import memory_handlers as mh

        return await mh._search_cue_arm(
            db, agent_id, query, depth, window, channel=channel, project_id=project_id,
            source_id=source_id, exclude_set=exclude_set, query_vec=query_vec,
            lexical_terms=lexical_terms,
        )


class Prior:
    """The age weight that orders what the gate and autocut admitted (`_apply_prior`)."""

    manifest = _manifest("prior")

    def apply(self, results, span, now):
        from . import memory_handlers as mh

        return mh._apply_prior(results, span, now)


class EvidenceSelector:
    """recall's selection among the admitted rows: the cue's bounded move, and its held seat."""

    manifest = _manifest("evidence_selector", cue.POLICY)

    def lift(self, admitted, cue_rank, bound, rid_of):
        return cue.lift(admitted, cue_rank, bound, rid_of)

    def seats(self, eligible: list[dict], places: int) -> list[dict]:
        # The cue arm's order, best first: the best eligible records take the places.
        return eligible[:places]


class ReconstructCandidates:
    """reconstruct's candidate pool: a recall at the candidate depth."""

    manifest = _manifest("reconstruct_candidates")

    async def candidates(self, agent_id, query, top_k, **kwargs) -> dict:
        from . import memory_handlers as mh

        return await mh.do_recall(agent_id, query, top_k, **kwargs)


class Reconstructor:
    """reconstruct's four pure stages: bundle, walk, structure, and the budget's allocation."""

    manifest = _manifest("reconstructor")

    def bundle(self, candidates, spans, links):
        from . import reconstruct

        return reconstruct.bundle(candidates, spans, links)

    def walk(self, chosen, candidates, graph, max_hops):
        from . import reconstruct

        return reconstruct.walk(chosen, candidates, graph, max_hops)

    def structure(self, rows, why_by_ref, spans, max_evidence, extra, links):
        from . import reconstruct

        return reconstruct.structure(rows, why_by_ref, spans, max_evidence, extra, links)

    def allocate(self, entries, budget):
        from . import reconstruct

        return reconstruct.allocate(entries, budget)


# The allowlist: slot -> provider id -> factory. Nothing outside this mapping
# can be selected (providers.resolve refuses it).
ALLOWLIST = {
    "fusion": {BUILTIN: Fusion},
    "scoring": {BUILTIN: Scoring},
    "block_candidates": {BUILTIN: BlockCandidates},
    "cue_interpreter": {BUILTIN: CueInterpreter},
    "envelope_planner": {BUILTIN: EnvelopePlanner},
    "cue_candidates": {BUILTIN: CueCandidates},
    "prior": {BUILTIN: Prior},
    "evidence_selector": {BUILTIN: EvidenceSelector},
    "reconstruct_candidates": {BUILTIN: ReconstructCandidates},
    "reconstructor": {BUILTIN: Reconstructor},
}
