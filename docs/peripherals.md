# Peripheral models

Built-in types are `imu`, `barometer`, `gps`, `adc`, and `pressure_transducer`. Every device requires `type` and `name`. Optional common behavior:

- `failure_every`: return a deterministic driver error every Nth access
- `disconnect_after`: behave as disconnected after N accesses

ADC models accept `bits` and `channels`; pressure transducers accept `max_psi`. The full test exercises ordinary data, intermittent failures, and disconnection without relying on hardware.

## Add a peripheral

1. Add its layout fields to `PeripheralSpec` in `peripherals/models.rs`.
2. Validate board-facing constraints in `validate`; reject impossible configurations instead of silently clamping them.
3. Add deterministic sampling in `sample`. Use the supplied seeded generator so CI failures reproduce.
4. Treat expected timeout, busy, corrupt-data, and disconnected behavior as observable results. Do not panic for a device-level failure.
5. Extend `schema/board-layout.schema.json` and add normal/fault/disconnect cases under `tests/`.
6. Add the device to the consuming board's `sim/board.json` and run `./build.py test --full`.

To exercise a real firmware driver on the host, expose its HAL calls behind a narrow C interface and add that driver to the board's unit-test command. The Rust model supplies device behavior; the board test owns the FFI adapter so vendor HAL types do not leak into this repository.

