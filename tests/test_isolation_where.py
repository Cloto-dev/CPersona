"""Unit tests for cpersona.isolation.isolation_where (2.5.0, Task #180).

The helper is the single source for the 3-axis read predicate; these tests pin
the per-axis contract (agent=exact, project=γ, channel=knob2 v2) and the alias
/ gluing accessors, so a semantics drift fails here before it reaches a recall
path. Behaviour-preservation note: every expectation below is the literal
fragment the pre-2.5.0 hand-rolled sites emitted.
"""

from cpersona.isolation import isolation_where


# --- agent_id: hard isolation, exact match, no γ union ---


def test_agent_exact():
    f = isolation_where(agent_id="a1")
    assert f.clause == "agent_id = ?"
    assert f.params == ("a1",)


def test_agent_none_means_no_filter():
    assert isolation_where(agent_id=None).clause == ""
    assert isolation_where(agent_id=None).params == ()


def test_agent_empty_string_fails_closed():
    # '' is NOT the maintenance "scan all agents" spelling (that is `agent_id or
    # None` at the call site) — it binds an exact '' match, which no do_store row
    # carries. A forgotten `or None` yields an empty result, never a cross-agent
    # leak (bug-044 class).
    f = isolation_where(agent_id="")
    assert f.clause == "agent_id = ?"
    assert f.params == ("",)


# --- project_id: γ semantics (delegated to the vendored gamma_clause) ---


def test_project_gamma_union():
    f = isolation_where(project_id="px")
    assert f.clause == "project_id IN (?, ?)"
    assert f.params == ("px", "")


def test_project_empty_is_global_only():
    f = isolation_where(project_id="")
    assert f.clause == "project_id = ?"
    assert f.params == ("",)


def test_project_none_is_no_filter():
    assert isolation_where(project_id=None).clause == ""


# --- channel: knob2 v2 ('' = no filter; named = named ∪ channel-global '') ---


def test_channel_named_includes_global():
    f = isolation_where(channel="discord")
    assert f.clause == "(channel = ? OR channel = '')"
    assert f.params == ("discord",)


def test_channel_empty_is_no_filter():
    # Deliberately different from project_id: '' on the channel axis reads
    # everything (all channels), it does NOT narrow to channel-global rows.
    assert isolation_where(channel="").clause == ""
    assert isolation_where(channel=None).clause == ""


# --- composition, ordering, accessors, alias ---


def test_all_axes_compose_in_identity_index_order():
    f = isolation_where(agent_id="a1", project_id="px", channel="chat")
    assert f.clause == (
        "agent_id = ? AND project_id IN (?, ?) AND (channel = ? OR channel = '')"
    )
    assert f.params == ("a1", "px", "", "chat")


def test_accessors_glue_and_empty_forms():
    f = isolation_where(agent_id="a1")
    assert f.and_clause == " AND agent_id = ?"
    assert f.where == " WHERE agent_id = ?"
    empty = isolation_where()
    assert empty.clause == "" and empty.and_clause == "" and empty.where == ""
    assert empty.params == ()


def test_alias_prefixes_every_column():
    f = isolation_where(agent_id="a1", project_id="px", channel="chat", alias="m")
    assert f.clause == (
        "m.agent_id = ? AND m.project_id IN (?, ?) AND (m.channel = ? OR m.channel = '')"
    )
    assert f.params == ("a1", "px", "", "chat")
