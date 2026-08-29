FROM rust:1.85-bookworm AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock* ./
COPY src src
COPY core core
COPY peripherals peripherals
COPY tests tests
RUN cargo test --locked
RUN cargo build --release --locked

FROM debian:bookworm-slim
ARG SIM_ARCH=all
ENV SIM_ARCH=${SIM_ARCH}
COPY --from=builder /src/target/release/firmware-sim /usr/local/bin/firmware-sim
USER 65532:65532
ENTRYPOINT ["firmware-sim"]
CMD ["--help"]
