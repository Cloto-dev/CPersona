# SuperAuditor standard

The SuperAuditor standard defines how a server reports the problems it finds in
its own stored state: on request, with honest counts, instead of attaching them
to every response. It now has its own repository,
**[Cloto-dev/superauditor](https://github.com/Cloto-dev/superauditor)**, with
the canonical text ([STANDARD.md](https://github.com/Cloto-dev/superauditor/blob/main/STANDARD.md),
v1.1), a response schema, conformance fixtures, a reference implementation to
copy, and a checker that tests a running server from outside. Version 1 was
first published on this page.

## How CPersona implements it

CPersona's `get_session_findings` ([tools](tools.md)) conforms to v1.1 and says
so in `_meta.superauditor`. The findings are the ones `check_health` computes,
delivered through a single detector.

| Requirement | In CPersona |
| --- | --- |
| Kinds and severity | Every health check is a kind with a fixed severity. A check that escalates by a fixed rule delivers each tier as its own kind: `null_embedding_expected` (info), `null_embedding` (warn), `null_embedding_pipeline_down` (critical). |
| A check that did not run | Delivered as a `check_crashed` finding (warn) that names the check, in place of that check's findings. The other checks are still delivered. |
| Reserved keys | A check's own `kind` field moves to `object_kind`, and its own graded `severity` to `health_severity`. If a check ever emitted either name itself, the call is refused rather than one field silently replacing another. |
| Isolation | Whole database: findings are not filtered by agent or project, so the tool needs read access to every agent. |
| Broadcast | None. CPersona never attached findings to other responses, so the migration requirement (C9) does not apply. |

The fixtures in `conformance/superauditor/v1/` are a copy of the standard's
`conformance/v1/`.

To check a running CPersona from outside:

```bash
uvx --from git+https://github.com/Cloto-dev/superauditor superauditor-check -- cpersona
```
