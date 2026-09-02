FROM rust:1.88-bookworm AS groundstation
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libudev-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
ARG GROUNDSTATION_REPOSITORY=https://github.com/University-at-Buffalo-SEDS/GroundStation26.git
ARG GROUNDSTATION_REF=1133e64a24bafe1638eae904a395dab3bd9035af
RUN git init /groundstation \
    && git -C /groundstation remote add origin "${GROUNDSTATION_REPOSITORY}" \
    && git -C /groundstation fetch --depth 1 origin "${GROUNDSTATION_REF}" \
    && git -C /groundstation checkout --detach FETCH_HEAD \
    && cargo build --manifest-path /groundstation/Cargo.toml \
         -p groundstation_backend --release --features hitl_mode

FROM rust:1.88-bookworm AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock* ./
COPY src src
COPY core core
COPY mcu mcu
COPY peripherals peripherals
COPY renode renode
COPY tests tests
RUN cargo test --locked
RUN cargo build --release --locked

FROM debian:bookworm-slim AS renode
ARG TARGETARCH
ARG RENODE_BUILD=1.16.1+20260828git00139efee
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
      amd64) archive="renode-${RENODE_BUILD}.linux-portable.tar.gz"; checksum="03c2cd3bd457d6863157a006191ec806f1f6bd024ef5e43c62b05b959de2ed9f" ;; \
      arm64) archive="renode-${RENODE_BUILD}.linux-arm64-portable.tar.gz"; checksum="e4dd7a25a9717a685aae9bef72b76f162ae18662069c7aa9ade600eacde492cb" ;; \
      *) echo "unsupported container architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://builds.renode.io/${archive}" -o /tmp/renode.tar.gz \
    && echo "${checksum}  /tmp/renode.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/renode \
    && tar -xzf /tmp/renode.tar.gz --strip-components=1 -C /opt/renode \
    && rm /tmp/renode.tar.gz

FROM mcr.microsoft.com/dotnet/runtime:8.0-bookworm-slim
# One image contains every bundled profile. The board layout's `mcu` and
# optional runtime descriptor select the exact silicon platform.
LABEL org.opencontainers.image.source="https://github.com/University-at-Buffalo-SEDS/FirmwareSimulator"
ENV SIM_ARCH=all
ENV FIRMWARE_SIM_ROOT=/opt/firmware-sim
ENV FIRMWARE_SIM_CONTAINER=1
ENV RENODE=/opt/renode/renode
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libudev1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/target/release/firmware-sim /usr/local/bin/firmware-sim
COPY --from=groundstation /groundstation/target/release/groundstation_backend /usr/local/bin/groundstation_backend
COPY --from=groundstation /groundstation/backend/layout /opt/groundstation/backend/layout
COPY --from=groundstation /groundstation/backend/config /opt/groundstation/backend/config
COPY --from=groundstation /groundstation/backend/comms /opt/groundstation/backend/comms
COPY --from=renode /opt/renode /opt/renode
COPY renode /opt/firmware-sim/renode
RUN useradd --create-home --uid 10001 simulator
RUN mkdir -p /opt/groundstation/backend/data \
    && chown -R simulator:simulator /opt/groundstation \
    && ln -s /opt/groundstation /groundstation
USER simulator
ENV HOME=/home/simulator
WORKDIR /home/simulator
ENTRYPOINT ["firmware-sim"]
CMD ["--help"]
