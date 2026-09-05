"""CPersona — persistent AI memory MCP server."""

# The one place the version is written, and the one the build reads
# (``[tool.hatch.version]`` in pyproject points here), so the distribution's
# metadata is derived from this string rather than kept in step with it.
#
# It lives in the package, not in pyproject, because the running server has to
# be able to name itself. `python server.py` from a clone puts the repository
# root on sys.path, so `import cpersona` resolves to this directory whatever the
# environment holds — a checkout can therefore run code from one release while
# an installed distribution beside it records another, and asking
# importlib.metadata would answer for the distribution rather than for the code
# actually serving the request. Measured on a production instance: the source
# was four releases ahead of the dist-info the server was quoting.
__version__ = "2.5.12a3"
