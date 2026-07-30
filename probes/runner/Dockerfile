# The probe container: dependencies, and nothing that knows the library's version.
#
# Build from the repo root:  docker build -f probes/runner/Dockerfile -t gnews-probe .
#
# The library is MOUNTED at run time and reached through PYTHONPATH, not baked in. That is the
# whole point: the previous image COPYed the source and pip-installed it, so every commit needed
# an rsync-and-rebuild step, and that step is exactly what drifted -- it once carried decoderv1
# through decoderv4 for months after they were deleted, so the lab measured code that no longer
# existed. A mount cannot drift.
#
# Dependencies still come from setup.py rather than a list here, because a hand-kept list is the
# same failure one level down. The library is installed to resolve them and then uninstalled, so
# the ONLY way to satisfy `import googlenewsdecoder` in this image is the mount. A missing mount
# is then an ImportError rather than a silent fall-through to a stale installed copy.
#
# No openvpn, and no NET_ADMIN or /dev/net/tun for this container: gluetun holds the tunnel and
# this joins its network namespace.
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY setup.py README.md ./
COPY googlenewsdecoder ./googlenewsdecoder
# requests is not a library dependency. The probes need it as the control arm: it is the client
# Google walls, so it is how a probe tells a walled exit from a clean one.
RUN pip install --no-cache-dir '.[all]' requests \
    && pip uninstall -y --quiet googlenewsdecoder \
    && rm -rf /src

WORKDIR /probes
