"""Compatibility shim — the implementation moved into the ``cpersona`` package.

This file keeps ``python server.py`` working for the git-clone / ClotoHub
marketplace install path (and preserves that path's entry point). The real
server lives in ``cpersona/server.py``; for PyPI / uvx use the ``cpersona``
console script or ``python -m cpersona``.

Running ``python server.py`` from the repository root puts the root on
``sys.path``, so ``import cpersona`` resolves to the package directory beside
this file.
"""

from cpersona.server import run

if __name__ == "__main__":
    run()
