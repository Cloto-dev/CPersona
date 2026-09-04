# CPersona as a container.
#
# What this image serves: the Streamable HTTP transport, on 8402, with the
# database on a volume. Both of those are choices the container makes for you,
# and both are stated as environment variables below so `docker inspect` shows
# them and a `-e` overrides them.
#
# Running the stdio transport instead — the shape an MCP client spawns as a
# subprocess — needs an interactive stdin and the transport named:
#
#     docker run -i --rm -e CPERSONA_TRANSPORT=stdio -v cpersona-data:/data <image>
#
# The HTTP transport refuses to start without CPERSONA_AUTH_TOKEN. That is the
# server's own guard and this image does not weaken it: a published container
# port forwards to whatever the process bound, so binding inside the container
# is not evidence that only the container can reach it.

FROM python:3.13-slim AS build

WORKDIR /src

# Only the wheel's inputs; .dockerignore admits nothing else. Copied as separate
# layers from the install so editing the package does not re-resolve the build
# backend.
COPY pyproject.toml README.md LICENSE ./
COPY cpersona/ ./cpersona/
COPY skills/ ./skills/

# The wheel, not the source tree. The runtime stage installs this artifact, so
# the container runs what a `pip install cpersona` user runs rather than a
# second copy of the repository that happens to sit on sys.path -- the two
# diverge exactly when the packaging is wrong, which is the case worth catching.
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


FROM python:3.13-slim

# A fixed uid, because it outlives the image. A named volume inherits ownership
# from the image and needs nothing; a bind-mounted host directory does not, so
# the host side has to be writable by this uid (or `--user "$(id -u)"` passed).
# Naming the number here is what makes that instruction possible to follow.
RUN useradd --system --create-home --uid 10001 cpersona

COPY --from=build /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

# 8402 is the server's own default HTTP port; it is repeated here so that
# changing one without the other cannot go unnoticed, and so EXPOSE below names
# a port something actually binds.
#
# CPERSONA_HTTP_HOST is the one value this image must override. The server
# defaults to 127.0.0.1, which inside a container means the container's own
# loopback: a published port would forward to a socket no one is listening on,
# and the symptom is a connection refused that looks like a crash. Binding to
# every interface is safe here for the reason the server's own guard states --
# the bind address decides nothing, the token does.
ENV CPERSONA_DB_PATH=/data/cpersona.db \
    CPERSONA_TRANSPORT=streamable-http \
    CPERSONA_HTTP_HOST=0.0.0.0 \
    CPERSONA_HTTP_PORT=8402

# Created before the volume is declared, so a fresh named volume inherits this
# ownership instead of arriving as root-owned and unwritable.
RUN install -d -o cpersona -g cpersona /data
VOLUME ["/data"]

USER cpersona
WORKDIR /home/cpersona
EXPOSE 8402

# Liveness only, and deliberately not a claim about health: it asks whether the
# port is bound and the application answers HTTP. Any status counts, because the
# authenticated deployment answers 401 to an anonymous probe and a 401 is a
# served response. The container's readiness to answer is what a restart policy
# can act on; whether the memory inside it is sound is what check_health
# answers, and that needs credentials this probe must not carry.
# http.client rather than urllib: it returns the status instead of raising on
# 4xx, so the probe needs no exception handling to accept the 401, and a refused
# connection still raises and exits non-zero. That keeps it to one line, which
# is the difference between a probe someone can read and one they trust.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,http.client;c=http.client.HTTPConnection('127.0.0.1',int(os.environ.get('CPERSONA_HTTP_PORT','8402')),timeout=4);c.request('GET','/mcp');print(c.getresponse().status)"]

LABEL org.opencontainers.image.title="CPersona" \
      org.opencontainers.image.description="Persistent AI memory server (MCP)" \
      org.opencontainers.image.source="https://github.com/Cloto-dev/cpersona" \
      org.opencontainers.image.licenses="MIT"

CMD ["cpersona"]
