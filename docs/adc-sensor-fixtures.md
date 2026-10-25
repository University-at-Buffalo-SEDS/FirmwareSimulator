# Explicit external ADC sensor fixtures

An instruction-coupled `stm32_adc` peripheral can opt into `stimulus_path` under
`/simulation` or `/diagnostics`. Omit it to keep the original static input and
perform no additional filesystem operations. The fixture contains exactly one
unsigned raw integer per configured channel, separated by commas; values must
fit the ADC resolution. It changes only external sensor samples. Firmware
conversion cadence, ADC registers, DMA, interrupts, and transport remain intact.

The production peripheral also exposes `SetChannelSample(channel,value)` with
channel/resolution validation. File fixtures poll once per10data-register reads
(20ms at ValveBoard's500Hz acquisition rate), read at most1025characters and
validate the whole record before changing channels. Missing, partial, oversized
or out-of-range records retain all last-good samples and increment a saturating
`StimulusRejected` diagnostic counter without console log spam.

Fixture writers should atomically replace a regular file. Use a fresh writable
`/diagnostics` scenario directory for live model inputs. No ordinary profile
opts in. This is an explicit sensor-input assumption, not a pressure solver or
physical validation; controllers must derive changes from actual observed valve
states and must never inject acknowledgements or qualification completion.
