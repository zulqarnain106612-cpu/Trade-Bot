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

# Debian security updates for packages already in the base. Pinning by digest
# fixes the OS packages at whatever the last upstream rebuild shipped, and
# that lags the security archive -- python:3.11-slim's current digest carries
# gzip, libpcre2, libsqlite3 and perl-base at versions with 3 CRITICAL and 10
# HIGH advisories, all of them already fixed in deb13u1/deb13u2. Re-pinning
# cannot help: this *is* the newest digest.
#
# `upgrade`, never `install`: this is allowed to move packages the base chose,
# and is not allowed to add any. The lists are dropped again so the package
# index does not ship in the image.
RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq \
    && rm -rf /var/lib/apt/lists/*

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

# The base image ships setuptools 79.0.1, and both packages the Trivy step in
# security.yml fails on are inside its `_vendor` tree rather than in
# requirements.txt: jaraco.context 5.3.0 (CVE-2026-23949, path traversal) and
# wheel 0.45.1 (CVE-2026-24049, privilege escalation). Installing the fixed
# versions alongside does not help -- a vendored copy is not a dependency pip
# can resolve -- so setuptools itself has to move. 81.0.0 is the first release
# that vendors jaraco.context 6.1.0 and wheel 0.46.3.
#
# The upper bound is not caution, it is a behaviour change: setuptools 82
# removed `pkg_resources`, which several scientific packages still import at
# runtime. Raising it is a deliberate decision, not a version bump.
#
# This belongs in the runtime stage, not the build stage: `COPY --from=build
# /install` overlays the base's site-packages rather than replacing it, so an
# upgrade performed there would leave the vulnerable dist-info in place for
# the scanner to find.
RUN python -m pip install --no-cache-dir --upgrade "setuptools>=81.0.0,<82.0.0"

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
