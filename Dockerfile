ARG RENODE_PLATFORM=linux/amd64
FROM --platform=${RENODE_PLATFORM} rust:1.85-bookworm AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock* ./
COPY src src
COPY core core
COPY peripherals peripherals
COPY renode renode
COPY tests tests
RUN cargo test --locked
RUN cargo build --release --locked

FROM --platform=${RENODE_PLATFORM} antmicro/renode:nightly-dotnet@sha256:22c0a5e9bd9dbdee21db5db29211a34c173d0ad1cfcb661146596bfcfab3291a
# One image contains every supported MCU platform. The board layout's `mcu`
# field selects STM32G491, STM32H523, or STM32U585 at runtime.
ENV SIM_ARCH=all
ENV FIRMWARE_SIM_ROOT=/opt/firmware-sim
ENV FIRMWARE_SIM_CONTAINER=1
ENV RENODE=/opt/renode/renode
USER root
COPY --from=builder /src/target/release/firmware-sim /usr/local/bin/firmware-sim
COPY renode /opt/firmware-sim/renode
RUN useradd --create-home --uid 10001 simulator
USER simulator
ENV HOME=/home/simulator
WORKDIR /home/simulator
ENTRYPOINT ["firmware-sim"]
CMD ["--help"]
