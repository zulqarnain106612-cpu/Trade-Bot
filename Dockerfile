# SUP-006 -- the container posture, written down where it is enforceable.
#
# Until this file existed the requirement had nothing to describe: there was no
# image, so "images run non-root with minimal privilege" was a rule with no
# subject. An unwritten posture is the one that gets decided by whoever writes
# the first Dockerfile under deadline, which is how a trading process ends up
# running as root with a package manager still installed.
#
# Every choice below is a defence against a specific thing:
#
#   multi-stage      the build tools (compilers, pip's cache, git) never reach
#                    the shipped layer, so an attacker who gets execution has
#                    nothing to build with
#   -slim base       fewer packages is fewer CVEs, and the scan job in
#                    security.yml has to pass on this image
#   non-root user    a process that cannot write to its own code cannot
#                    persist a modification of it
#   read-only-ready  nothing is written under the application directory at
#                    runtime, so the deployment can mount it read-only
#   no shell for the the application user has /usr/sbin/nologin: an exploit
#   app user         that lands on it does not get an interactive shell
#   pinned base      by digest, for the same reason actions are SHA-pinned --
#                    a tag is a pointer somebody else can move
#
# The runtime flags that complete this (--read-only, --cap-drop=ALL,
# --security-opt=no-new-privileges) belong to the deployment, not the image,
# and are documented in docs/security/SECURITY_POLICY.md. An image cannot
# enforce them; what it can do is be usable with them, which the health check
# below exercises.

# python:3.11-slim -- matches .python-version, which is what CI installs
FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS build

WORKDIR /build

# Copy only the manifest first so a source change does not invalidate the
# dependency layer -- a rebuild that re-resolves every package is a rebuild
# that can silently pick up something different.
COPY requirements.txt ./

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src ./src
COPY common ./common
COPY config ./config
COPY scripts ./scripts

# ---------------------------------------------------------------------------

FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

# A fixed uid/gid rather than whatever the base assigns: a volume mounted from
# the host has to match something, and "whatever useradd picked" is not a
# thing a deployment can match.
RUN groupadd --gid 10001 tradebot \
    && useradd --uid 10001 --gid 10001 --shell /usr/sbin/nologin --create-home tradebot

WORKDIR /app

COPY --from=build /install /usr/local
COPY --from=build --chown=root:root /build/src /app/src
COPY --from=build --chown=root:root /build/common /app/common
COPY --from=build --chown=root:root /build/config /app/config
COPY --from=build --chown=root:root /build/scripts /app/scripts

# Owned by root, run as tradebot: the running process cannot modify its own
# code. This is the single most useful property in the file.
USER 10001:10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/usr/local/bin:${PATH}"

EXPOSE 8000

# Exercises the read-only assumption: if anything in startup wanted to write
# under /app, this fails in CI rather than in production.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "src.api"]
