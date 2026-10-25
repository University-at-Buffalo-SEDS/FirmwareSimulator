# NeoM9N raw fix stimulus

The ordinary RF SPI GPS model accepts an opt-in `stimulusPath` constructor argument. Layouts pass it as `stimulus_path` on the `neo_m9n` peripheral, subject to the simulator's diagnostic-path allowlist. The file contains one `0` or `1`, with optional surrounding whitespace and a maximum of 16 characters. Use atomic file replacement.

`0` emits checksummed GGA sentences with quality 0, satellites 00 and HDOP 99.9, plus RMC status V / mode N. `1` restores the existing valid GGA/RMC stream, at the configured latitude/longitude/MSL altitude and current virtual GPS UTC. The firmware's real SPI parser and ordinary telemetry path decide whether a fix can be published; this does not write GroundStation eligibility, calibration, command ACKs or qualification state.

The file is read on reset and once per 1 Hz virtual GPS timer, independently of byte clocks. Missing/malformed/oversized input increments the saturating `StimulusRejected` diagnostic and retains the last good value. An explicitly configured path starts without a fix until a valid file is read. Output retains at most one complete pending GGA/RMC batch: a new input never splices an already queued sentence, so a complete older batch may drain before the next current batch. Without a path, bytes, cadence, failure injection and reset-time continuity are unchanged, with no added filesystem I/O.

For an ordinary flight acceptance test, create `0` before RF boot, prove routes/telemetry remain healthy while the GPS launch prerequisite is false, then atomically replace it with `1` and wait for a new RF GPS publication. FC's filtered GPS values cannot substitute for this receiver lock. The model uses its existing virtual epoch 2026-01-01 and does not model satellite acquisition, propagation, antenna placement or physical outputs.

Run `python3 -m unittest discover -s tests -p test_gps_nmea_model.py -v` with Mono, or select an existing local .NET 8 SDK image through `CSHARP_TEST_DOTNET_IMAGE`. The contract runs the actual model class and checks checksums, fix status, bounded pacing, invalid-input retention, reset continuity and exact default/empty-path byte equality.
