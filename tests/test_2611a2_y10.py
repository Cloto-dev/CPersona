"""The server boundary: per-agent read scoping, the write gate, startup refusals.

- Per-agent reads: nine read tools demand read on the agent they are called for. Demoted
  to the authenticated-only demand, a row would be skipped by both the grant loop
  and the per-subject boundary loop, so a principal granted one agent would read
  another; the classification tests only compared set membership and the maximum
  level, which a demotion keeps. Pinned here through the served handlers, against
  real rows.
- The write gate: store, declare_associations and archive_episode pass write=True to the
  operating-context gate, the one argument that turns reject mode into a refusal
  instead of a warning. Pinned behaviourally for each write tool, and for every
  boundary at once by comparing the flag each passes with its tool's ACL level.
- Startup refusals: main() refuses a reconstruct count or budget above its maximum and a
  block retrieval gate with no build behind it before anything else starts. Pinned
  by driving main() under each bad configuration, plus the forced-budget branch of
  the validator, which no other test reached.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile

import pytest

from cpersona import acl, aliases, config, database, server, session

# ---------------------------------------------------------------------------
# Shared: a fresh database per test
# ---------------------------------------------------------------------------


class _FreshDB:
    """Swap the process database for an empty file of this test's own.

    The rows these tests read back are the only rows in it, so "nothing landed"
    and "nothing leaked" are statements about this test and no other.
    """

    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "y10.db")
        self.db = await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH = self._saved
        session.reset_pauses_for_tests()


# ---------------------------------------------------------------------------
# The per-agent read tools deny an agent the principal was not granted
# ---------------------------------------------------------------------------

ALPHA = "agent.y10-alpha"
BETA = "agent.y10-beta"
ISSUER = "https://auth.example.com"
OAUTH_CLIENT = f"oauth:{ISSUER}:https://claude.ai/mcp-client"


def _secret(agent: str) -> str:
    return f"secret-{agent.rsplit('-', 1)[-1]}"


#: Every per-agent read tool, with the arguments that make it return the agent's
#: own records when it is allowed to run. ``{agent}`` and ``{ref}`` are filled in
#: per call, so a leak shows as the other agent's secret in the response.
_READ_TOOLS = {
    "recall": {"query": "zebra quokka"},
    "reconstruct": {"query": "zebra quokka"},
    "traverse": {"entity": "{secret}-node"},
    "recall_with_context": {"query": "zebra quokka"},
    "get_contents": {"refs": ["{ref}"]},
    "get_profile": {},
    "get_recall_precision": {},
    "list_memories": {},
    "list_episodes": {},
}

#: get_recall_precision holds no stored text, so for it the denial is the whole
#: check; every other tool also has to quote the granted agent's own secret.
_QUOTES_RECORDS = set(_READ_TOOLS) - {"get_recall_precision"}


def test_the_read_tool_list_is_every_per_agent_read_classification():
    """The parametrisation below covers every per-agent read the server serves."""
    scoped_reads = {
        name
        for name, demands in acl.ACL_CLASSIFICATION.items()
        if demands({"agent_id": ALPHA}) == [(ALPHA, acl.PERM_READ)]
        and next(t for t in server.registry._tools if t.name == name).annotations.readOnlyHint
    }
    assert set(_READ_TOOLS) == scoped_reads


async def _seed_agent(agent: str) -> str:
    """Give *agent* a memory, an entity, an episode and a profile, all carrying its secret."""
    handlers = server.registry._handlers
    secret = _secret(agent)
    stored = await handlers["store"]({"agent_id": agent, "content": f"{secret} zebra quokka"})
    assert stored.get("result") == "stored", stored
    declared = await handlers["declare_associations"](
        {
            "agent_id": agent,
            "associations": {
                "entities": [{"name": f"{secret}-node"}, {"name": f"{secret}-peer"}],
                "relations": [{"subject": f"{secret}-node", "predicate": "knows", "object": f"{secret}-peer"}],
            },
        }
    )
    assert declared.get("result") == "declared", declared
    episode = await handlers["archive_episode"](
        {"agent_id": agent, "summary": f"episode {secret} zebra quokka", "keywords": "zebra"}
    )
    assert episode.get("ok") is True, episode
    profile = await handlers["update_profile"]({"agent_id": agent, "profile": f"profile {secret}"})
    assert profile.get("ok") is True, profile
    return f"mem:{stored['id']}"


def _args(tool: str, agent: str, ref: str) -> dict:
    filled = {"agent_id": agent}
    for key, value in _READ_TOOLS[tool].items():
        if isinstance(value, list):
            filled[key] = [v.format(ref=ref) for v in value]
        else:
            filled[key] = value.format(secret=_secret(agent))
    return filled


async def _call_as(principal: acl.Principal, tool: str, args: dict) -> dict:
    handler = server.registry._handlers[tool]
    assert getattr(handler, "_acl_guarded", False), f"{tool} is served without the ACL guard"
    token = acl.set_principal(principal)
    try:
        return await handler(args)
    finally:
        acl.reset_principal(token)


def _write_acl(tmp_path, payload) -> str:
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.fixture
def _acl_reset():
    yield
    acl.activate(None)
    acl.activate_ledger(None)


@pytest.mark.usefixtures("blocks_off", "_acl_reset")
@pytest.mark.asyncio
@pytest.mark.parametrize("tool", list(_READ_TOOLS))
async def test_a_reader_granted_one_agent_cannot_read_another(tmp_path, tool):
    async with _FreshDB():
        refs = {agent: await _seed_agent(agent) for agent in (ALPHA, BETA)}
        acl.activate(
            acl.load_config(
                _write_acl(
                    tmp_path, {"clients": [{"client_id": "beta-reader", "token": "t", "grants": {BETA: "read"}}]}
                )
            )
        )
        reader = acl.Principal("beta-reader")

        foreign = await _call_as(reader, tool, _args(tool, ALPHA, refs[ALPHA]))
        own = await _call_as(reader, tool, _args(tool, BETA, refs[BETA]))

    assert foreign.get("error") == "permission_denied" and foreign.get("agent_id") == ALPHA, (
        f"{tool}: a principal granted only {BETA} was not refused {ALPHA}: {foreign}"
    )
    assert _secret(ALPHA) not in json.dumps(foreign, default=str), foreign
    assert own.get("error") != "permission_denied", f"{tool}: the granted agent was refused: {own}"
    if tool in _QUOTES_RECORDS:
        # The control: the call that was allowed really reads the agent's records,
        # so the denial above is about the agent and not about a call that reads nothing.
        assert _secret(BETA) in json.dumps(own, default=str), own


@pytest.mark.usefixtures("blocks_off", "_acl_reset")
@pytest.mark.asyncio
@pytest.mark.parametrize("tool", list(_READ_TOOLS))
async def test_the_per_subject_boundary_denies_a_foreign_agent_on_every_read(tmp_path, tool):
    """Held under a "*": read-write grant, so only the boundary can refuse."""
    async with _FreshDB():
        refs = {agent: await _seed_agent(agent) for agent in (ALPHA, BETA)}
        acl.activate(
            acl.load_config(
                _write_acl(
                    tmp_path,
                    {
                        "clients": [
                            {
                                "client_id": OAUTH_CLIENT,
                                "token": None,
                                "grants": {"*": "read-write"},
                                "per_subject": True,
                            }
                        ]
                    },
                )
            )
        )
        ledger = aliases.AliasLedger(str(tmp_path / "alias_ledger.json"))
        acl.activate_ledger(ledger)
        # The subject has an alias of its own, so the refusal below is about the
        # foreign agent and not about a subject with no space yet.
        own_alias, _issued = ledger.resolve_or_issue(ISSUER, "user-1")
        assert own_alias and own_alias != ALPHA
        subject = acl.Principal(client_id=OAUTH_CLIENT, issuer=ISSUER, subject="user-1")

        foreign = await _call_as(subject, tool, _args(tool, ALPHA, refs[ALPHA]))
        mine = await _call_as(subject, tool, {**_args(tool, ALPHA, refs[ALPHA]), "agent_id": acl.SELF_SENTINEL})

    assert foreign.get("error") == "permission_denied" and foreign.get("agent_id") == ALPHA, (
        f"{tool}: a per-subject principal reached another agent: {foreign}"
    )
    assert _secret(ALPHA) not in json.dumps(foreign, default=str), foreign
    assert mine.get("error") != "permission_denied", f"{tool}: the subject's own space was refused: {mine}"


# ---------------------------------------------------------------------------
# The operating-context gate refuses writes into an unregistered bucket
# ---------------------------------------------------------------------------

_REJECT_SIDECAR = """
version = 1
context_revision = "2026-10-15.1"

[instructions]
summary = "registry: \\"\\" and \\"known\\"."

[registry]
project_ids = ["", "known"]
enforce = "reject"
"""

WRITER = "agent.y10-writer"


@pytest.fixture
def reject_context(tmp_path, monkeypatch):
    path = tmp_path / "operating-context.toml"
    path.write_text(_REJECT_SIDECAR, encoding="utf-8")
    stat = path.stat()
    # A fresh mtime, so a sidecar cached by an earlier test is re-read.
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "on")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(path))


async def _write_store(project_id):
    return await server.do_store_boundary(WRITER, content="gate probe zebra", project_id=project_id)


async def _write_declare(project_id):
    return await server.do_declare_associations_boundary(
        WRITER,
        associations={
            "entities": [{"name": "GateProbe"}, {"name": "GatePeer"}],
            "relations": [{"subject": "GateProbe", "predicate": "uses", "object": "GatePeer"}],
        },
        project_id=project_id,
    )


async def _write_archive(project_id):
    return await server.do_archive_episode_boundary(
        WRITER, [], summary="gate probe episode", keywords="gate", project_id=project_id
    )


#: Each write boundary, the call that writes through it, and the table it lands in.
_WRITES = {
    "store": (_write_store, "memories"),
    "declare_associations": (_write_declare, "entities"),
    "archive_episode": (_write_archive, "episodes"),
}


async def _rows_in(db, table: str, project_id: str) -> int:
    rows = await db.execute_fetchall(f"SELECT count(*) FROM {table} WHERE project_id = ?", (project_id,))
    return rows[0][0]


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
@pytest.mark.parametrize("tool", list(_WRITES))
async def test_reject_mode_refuses_a_write_into_an_unregistered_project(reject_context, tool):
    write, table = _WRITES[tool]
    async with _FreshDB() as fresh:
        refused = await write("ghost")
        landed_ghost = await _rows_in(fresh.db, table, "ghost")
        # The control: the same call into a registered bucket does write, so the
        # zero above is the gate's doing and not a call that writes nothing.
        accepted = await write("known")
        landed_known = await _rows_in(fresh.db, table, "known")

    assert refused.get("ok") is False and "not in registry" in refused.get("error", ""), (
        f"{tool}: reject mode let a write into an unregistered project through: {refused}"
    )
    assert landed_ghost == 0, f"{tool}: {landed_ghost} row(s) landed in the unregistered bucket"
    assert accepted.get("ok") is not False and "operating_context_warning" not in accepted, accepted
    assert landed_known > 0, f"{tool}: the registered write landed nothing: {accepted}"


class _GateReached(Exception):
    """Raised by the spy in place of the gate, so no boundary goes further."""


_PLACEHOLDERS = {str: "x", list: [], dict: {}, int: 5, bool: False}


def _placeholder(param: inspect.Parameter):
    annotation = param.annotation
    if isinstance(annotation, str):
        annotation = eval(annotation, vars(server))  # the module uses postponed annotations
    for kind, value in _PLACEHOLDERS.items():
        if annotation is kind or kind in getattr(annotation, "__args__", ()):
            return value
    return None


def _boundaries() -> dict[str, object]:
    return {
        name: fn
        for name, fn in vars(server).items()
        if name.startswith("do_") and name.endswith("_boundary") and inspect.iscoroutinefunction(fn)
    }


def test_the_boundary_discovery_sees_the_write_tools():
    """The comparison below is vacuous if discovery finds nothing; it finds at least these."""
    assert {f"do_{tool}_boundary" for tool in _WRITES} <= set(_boundaries())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_boundaries()))
async def test_every_boundary_gates_as_a_write_exactly_when_its_tool_writes(monkeypatch, name):
    """§5.1 damage asymmetry: a bad write pollutes a bucket, a bad read filter loses nothing.

    So the gate's ``write`` flag on each boundary has to agree with the tool's ACL
    level — a write tool that passed write=False would turn reject mode into a
    warning for it. Discovered from the module, so a boundary added later is held
    to the same rule without a test of its own.
    """
    seen: list[bool] = []

    def spy(project_id, agent_id, write):
        seen.append(write)
        raise _GateReached

    monkeypatch.setattr(server.operating_context, "check_project_id", spy)
    fn = _boundaries()[name]
    args = {
        p.name: _placeholder(p)
        for p in inspect.signature(fn).parameters.values()
        if p.default is inspect.Parameter.empty
    }
    with pytest.raises(_GateReached):
        await fn(**{**args, "agent_id": WRITER, "project_id": "ghost"})

    tool = name[len("do_"):-len("_boundary")]
    # A tool registered only under a setting has its row only then; the row it gets
    # is the conditional one.
    classification = acl.ACL_CLASSIFICATION.get(tool) or acl.CONDITIONAL_CLASSIFICATION[tool]
    demands = classification({"agent_id": WRITER})
    writes = max(required for _, required in demands) == acl.PERM_WRITE
    assert seen == [writes], f"{name}: gate called with write={seen}, but {tool} writes={writes}"


# ---------------------------------------------------------------------------
# main() refuses a count, budget or block gate it could only clamp
# ---------------------------------------------------------------------------


class _PastStartupValidation(Exception):
    """Raised by the first step after the configuration checks in server.main()."""


@pytest.fixture
def stop_after_validation(monkeypatch):
    """Stop main() at the step after its configuration checks.

    Reaching the sentinel means every check accepted the configuration, so an
    unfixed main() fails the test there instead of opening a database and serving.
    """
    monkeypatch.delenv("CPERSONA_ACL_FILE", raising=False)

    def _sentinel():
        raise _PastStartupValidation("server.main() went past its configuration checks")

    monkeypatch.setattr(server, "_preflight_http_auth", _sentinel)


@pytest.mark.asyncio
async def test_main_starts_past_validation_on_the_shipped_configuration(stop_after_validation):
    """The control: the refusals below come from the bad value, not from main() itself."""
    with pytest.raises(_PastStartupValidation):
        await server.main()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings, named",
    [
        (
            {"RECONSTRUCT_MAX_COUNT": 10, "RECONSTRUCT_DEFAULT_COUNT": 20},
            "CPERSONA_RECONSTRUCT_DEFAULT_COUNT",
        ),
        (
            {"RECONSTRUCT_MAX_COUNT": 10, "RECONSTRUCT_FORCED_COUNT": 11},
            "CPERSONA_RECONSTRUCT_FORCED_COUNT",
        ),
        (
            {"RECONSTRUCT_MAX_BUDGET": 20000, "RECONSTRUCT_DEFAULT_BUDGET": 6000, "RECONSTRUCT_FORCED_BUDGET": 50000},
            "CPERSONA_RECONSTRUCT_FORCED_BUDGET",
        ),
        (
            {"BLOCK_BUILD_ENABLED": False, "BLOCK_RETRIEVAL_ENABLED": True},
            "CPERSONA_BLOCK_RETRIEVAL_ENABLED",
        ),
    ],
    ids=["default-count", "forced-count", "forced-budget", "block-gates"],
)
async def test_main_refuses_to_start_on_a_value_it_could_only_clamp(
    monkeypatch, stop_after_validation, settings, named
):
    for attr, value in settings.items():
        monkeypatch.setattr(config, attr, value)
    with pytest.raises(ValueError, match=named):
        await server.main()


def test_a_forced_budget_above_the_maximum_is_refused_with_the_default_below_it(monkeypatch):
    """The forced-budget branch on its own: the default-budget branch before it passes."""
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 20000)
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_BUDGET", 6000)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", 20001)
    with pytest.raises(ValueError, match="CPERSONA_RECONSTRUCT_FORCED_BUDGET=20001 exceeds"):
        config.validate_reconstruct_counts()
    # At the maximum itself the configuration is valid: the bound is inclusive.
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", 20000)
    config.validate_reconstruct_counts()
