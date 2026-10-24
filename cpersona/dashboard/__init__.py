"""A local, read-only dashboard for a CPersona store.

It opens the SQLite file with ``mode=ro`` and serves one page on 127.0.0.1, so a
person can see what their agents have stored without going through an agent. It
uses the standard library only and writes nothing: the read-only connection is
what refuses a write, not a promise kept by the code.

- ``store`` reads the database (the read-only contract lives there).
- ``web`` serves the page and its JSON on the loopback interface.
- ``python -m cpersona.dashboard`` and ``cpersona dashboard`` start it.
"""
