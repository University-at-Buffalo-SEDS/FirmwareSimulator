FROM --platform=linux/amd64 rust:1.85-bookworm AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock* ./
COPY src src
COPY core core
COPY peripherals peripherals
COPY tests tests
RUN cargo test --locked
RUN cargo build --release --locked

FROM --platform=linux/amd64 antmicro/renode:1.16.1
ARG SIM_ARCH=all
ENV SIM_ARCH=${SIM_ARCH}
ENV FIRMWARE_SIM_ROOT=/opt/firmware-sim
ENV FIRMWARE_SIM_CONTAINER=1
USER root
COPY --from=builder /src/target/release/firmware-sim /usr/local/bin/firmware-sim
COPY renode /opt/firmware-sim/renode
USER developer
ENTRYPOINT ["firmware-sim"]
CMD ["--help"]
