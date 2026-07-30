# Security Policy

## Supported Versions

Support follows the release-tier policy in [SUPPORT.md](SUPPORT.md).
Security fixes are provided for the Stable line, the Current line, and any
line inside its 30-day grace window.

| Line | Security fixes |
| --- | --- |
| Stable (2.4.x) | ✅ — but see *What a Stable fix may be* below: some land as a warning, not as enforcement |
| Current / Experimental (next-line releases and pre-releases) | ✅ shipped in the next release / pre-release |
| Lines in the 30-day grace window | ✅ — same qualification as Stable |
| EOL lines | ❌ maintainer discretion only |

### What a Stable fix may be

The Stable line's contract is behaviour preservation, and a fix that changes a
default from "starts" to "refuses to start" breaks it. So a security fix on
Stable can take either of two forms, and the distinction is material to anyone
deciding whether an upgrade closes their exposure:

- **Enforcement** — the unsafe path is refused or removed. The line's behaviour
  changes only when it was already unsafe.
- **Disclosure** — the unsafe path keeps working exactly as before, and what
  changes is that the software stops describing it as safe: corrected warnings,
  observed-exposure reporting, and a Known-issues entry naming the release that
  enforces it.

Where a fix landed as disclosure only, [SUPPORT.md § Known
issues](SUPPORT.md#known-issues) says so explicitly and names the line that
enforces. Do not read "fixed in the Stable line" as "enforced in the Stable
line" without checking there. Enforcement always lands on the Current line
first; if your deployment needs it, upgrade to that line rather than waiting
for it on Stable.

## Reporting a Vulnerability

Please use GitHub private vulnerability reporting ("Report a vulnerability"
under the repository's Security tab) if available, or email
`ClotoCore@proton.me`.

Do **not** open public issues for security reports.
