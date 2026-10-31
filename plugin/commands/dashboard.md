---
description: Open the read-only CPersona dashboard for this memory store in the browser
---

Open the CPersona dashboard for me now, in my browser:

1. Call CPersona's `open_dashboard` tool with `open_browser: true`.
2. If the result says `"opened": true`, the page is already open: tell me so in one line.
3. Otherwise, open the returned `url` in my browser yourself, right away, with this machine's command: `open "<url>"` on macOS, `xdg-open "<url>"` on Linux, `start "" "<url>"` on Windows. A link from a remote server opens once and only for ten minutes, so do not wait for me to click it. Then tell me in one line that it is open.
4. Only if you cannot run a command here, give me the `url` as a link I can open.

Do not check the page by looking at my screen or my browser, or by fetching the `url` yourself: a remote link opens once, so loading it to check uses it up, and what is on my screen is mine to look at. Opening it is the whole task.

If the tool is not available, tell me why: the server registers it when CPersona runs on this machine (the stdio transport) with `CPERSONA_DASHBOARD` on, or on an HTTP server with `CPERSONA_DASHBOARD_REMOTE` on and `CPERSONA_DASHBOARD_PUBLIC_URL` set.

The tool is deprecated and goes in CPersona 2.7. Tell me in one line that I can open the dashboard myself, without you: `cpersona dashboard` in a terminal opens the database CPersona used last, and `cpersona dashboard --install-shortcut` makes a desktop shortcut for it.
