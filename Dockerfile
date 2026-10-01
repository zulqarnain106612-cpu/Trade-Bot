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

# The CPU torch index first, for the same reason ci.yml does it (GOV-015).
# PyPI's default torch wheel bundles the CUDA runtime: gigabytes of wheel for
# an image that has no GPU and never executes a line of it. Unpacked into a
# build layer it exhausted the runner's disk outright -- the container job
# died on "No space left on device" before it reached the scan. The range pin
# below is already satisfied when pip reaches requirements.txt, so that file
# stays the single source of truth for the version.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --prefix=/install \
         --index-url https://download.pytorch.org/whl/cpu "torch>=2.3,<3.0" \
    && python -m pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src ./src
COPY common ./common
COPY config ./config
COPY scripts ./scripts

# ---------------------------------------------------------------------------

FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

# Pin the base, then patch it.
#
# The digest above is the newest python:3.11-slim, and the Trivy gate still
# fails on it: Debian has a fixed perl-base and the image has not been rebuilt
# with it yet. That gap is normal and it is why pinning alone is not a
# security posture -- a digest is reproducible, not current.
#
# Security upgrades only, and no new packages: `upgrade` rather than `install`
# keeps the "no toolchain in the runtime layer" property that
# tests/supply_chain/test_container_posture.py asserts. The lists are removed
# in the same layer, or they ship inside the image for no reason.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# setuptools ships vendored dependencies (jaraco.*) that the scan reads out of
# their METADATA, and the base image's copy lags the fixed release. Upgrading
# it here is cheaper than carrying an ignore file that somebody has to
# remember to prune.
RUN python -m pip install --no-cache-dir --upgrade pip setuptools

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

# Remove the package manager from the runtime image.
#
# This started as a vulnerability hunt and ended as a posture fix, which is
# the more useful outcome. requirements.txt floors msgpack at 1.2.1 and the
# image carries msgpack-1.2.2 -- yet the scan kept reporting 1.1.2 installed.
# The second copy is pip's: pip vendors msgpack under `pip/_vendor`, pinned
# to whatever that pip release shipped, and no upgrade of the real package
# touches it.
#
# The fix is not to chase the vendored copy. A runtime container has no
# business carrying a package manager: it is an install capability handed to
# anyone who gets execution, and this file already claims "no toolchain in
# the runtime layer". Removing pip makes that claim true and takes the
# vendored tree with it.
#
# setuptools stays -- pkg_resources is still imported by parts of the ML
# stack at runtime, and removing it trades a scan finding for a crash.
RUN python -m pip uninstall --yes pip wheel \
    && rm -rf /usr/local/lib/python3.11/site-packages/pip \
    && rm -rf /root/.cache

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
