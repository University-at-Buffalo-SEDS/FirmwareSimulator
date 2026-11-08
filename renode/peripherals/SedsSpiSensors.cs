using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Peripherals.Timers;

namespace Antmicro.Renode.Peripherals.Sensors
{
    // Deterministic SPI models used by the real board drivers. These models
    // intentionally implement wire/register behavior, not a HAL shortcut.
    public sealed class SedsFlightSensorBus : ISPIPeripheral, IGPIOReceiver, INumberedGPIOOutput
    {
        private enum Device { Barometer, Gyroscope, Accelerometer }

        public SedsFlightSensorBus(IMachine machine, ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue)
        {
            this.failureEvery = failureEvery;
            this.disconnectAfter = disconnectAfter;
            profile = SedsFlightProfile.FromEnvironment();
            if(profile != null)
            {
                launchPath = Environment.GetEnvironmentVariable("SEDS_FLIGHT_LAUNCH") ?? "";
                profileClock = new LimitTimer(machine.ClockSource, 1000, this, "flightProfileClock",
                    limit: 1, direction: Antmicro.Renode.Time.Direction.Ascending, enabled: true, eventEnabled: true);
                profileClock.LimitReached += () => profileClockMs++;
            }
            Connections = new Dictionary<int, IGPIO> { {0, new GPIO()}, {1, new GPIO()}, {2, new GPIO()} };
            // BMP390 datasheet 3.7.5.3: non-latched DRDY self-clears after
            // 2.5ms, independently of a data-register read. Firmware uses 0x42.
            barometerPulse = new LimitTimer(machine.ClockSource, 1000000, this,
                "barometerDrdyPulse", limit: 2500, enabled: false, eventEnabled: true,
                workMode: Antmicro.Renode.Time.WorkMode.OneShot);
            barometerPulse.LimitReached += () => SetBarometerInterrupt(false);
            timers = new LimitTimer[3];
            for(var i = 0; i < 3; i++)
            {
                var sensor = i;
                timers[i] = new LimitTimer(machine.ClockSource, 3200000, this,
                    "conversion" + i, limit: 32000, enabled: false, eventEnabled: true,
                    direction: Antmicro.Renode.Time.Direction.Ascending);
                timers[i].LimitReached += () => {
                    conversions[sensor]++;
                    if(transactions <= disconnectAfter)
                    {
                        if(sensor == 0)
                        {
                            barometerDataReady = true;
                            SetBarometerInterrupt(true);
                            barometerPulse.Reset();
                            barometerPulse.Enabled = (Get(Device.Barometer, 0x19) & 4) == 0;
                        }
                        else Connections[sensor].Set(true);
                    }
                };
            }
            Reset();
        }

        public byte Transmit(byte value)
        {
            if(!selected) return 0xff;
            if(device == Device.Barometer && initializationTrace.Count < 64)
                initializationTrace.Add(string.Format("{0}:{1:X2}", position, value));
            if(position == 0)
            {
                transactions++;
                faulted = transactions > disconnectAfter || (failureEvery != 0 && transactions % failureEvery == 0);
                address = (byte)(value & 0x7f);
                reading = (value & 0x80) != 0;
                sampleRead = reading && ((device == Device.Barometer && address == 4)
                    || (device == Device.Gyroscope && address == 2)
                    || (device == Device.Accelerometer && address == 0x12));
                position++;
                return 0;
            }

            if(faulted) return 0xff;

            if(!reading)
            {
                registers[Key(device, address)] = value;
                UpdateTimer(device);
                address++;
                position++;
                return 0;
            }

            // BMP390 and BMI088 accelerometer insert one dummy byte. BMI088
            // gyro returns register data immediately after the command byte.
            var dummyBytes = device == Device.Gyroscope ? 0 : 1;
            if(position++ <= dummyBytes)
            {
                return 0;
            }
            var result = ReadRegister(device, address);
            address++;
            return result;
        }

        public void FinishTransmission()
        {
            // HAL splits a register read into command/dummy/data transfers.
            // Only the physical GPIO CS edge terminates the transaction.
        }

        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number > 2) throw new ArgumentOutOfRangeException(nameof(number));
            if(!value) { device = (Device)number; selected = true; position = 0; sampleRead = false; }
            else if(selected && device == (Device)number)
            {
                if(sampleRead && !faulted)
                {
                    samples[number]++;
                    if(number != 0) Connections[number].Set(false);
                }
                selected = false;
                position = 0;
            }
        }

        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        private void SetBarometerInterrupt(bool asserted)
        {
            var activeHigh = (Get(Device.Barometer, 0x19) & 2) != 0;
            Connections[0].Set(asserted ? activeHigh : !activeHigh);
        }

        public string GetAcquisitionState()
        {
            return string.Format("baro power={0:X2} odr={1:X2} int={2:X2}; conversions={3},{4},{5}; reads={6},{7},{8}",
                Get(Device.Barometer, 0x1b), Get(Device.Barometer, 0x1d), Get(Device.Barometer, 0x19),
                conversions[0], conversions[1], conversions[2], samples[0], samples[1], samples[2])
                + "; baro SPI init=" + string.Join(",", initializationTrace);
        }

        private void UpdateTimer(Device sensor)
        {
            var timer = timers[(int)sensor];
            if(sensor == Device.Barometer)
            {
                timer.Limit = 16000UL << Math.Min(17, (int)Get(sensor, 0x1d));
                timer.Enabled = (Get(sensor, 0x1b) & 0x30) == 0x30 && (Get(sensor, 0x19) & 0x40) != 0;
            }
            else if(sensor == Device.Gyroscope)
            {
                var rates = new uint[] {2000, 2000, 1000, 400, 200, 100, 200, 100};
                timer.Limit = 3200000 / rates[Get(sensor, 0x10) & 7];
                timer.Enabled = (Get(sensor, 0x15) & 0x80) != 0;
            }
            else
            {
                var odr = Get(sensor, 0x40) & 15;
                timer.Limit = 256000UL >> Math.Min(7, Math.Max(0, odr - 5));
                timer.Enabled = (Get(sensor, 0x58) & 4) != 0 && (Get(sensor, 0x7d) & 4) != 0;
            }
        }

        private byte Get(Device sensor, byte reg)
        {
            byte value;
            return registers.TryGetValue(Key(sensor, reg), out value) ? value : (byte)0;
        }

        public void Reset()
        {
            registers.Clear();
            initializationTrace.Clear();
            device = Device.Barometer;
            address = 0;
            position = 0;
            selected = false;
            sampleRead = false;
            reading = false;
            transactions = 0;
            faulted = false;
            barometerDataReady = false;
            barometerPulse.Reset();
            barometerPulse.Enabled = false;
            Array.Clear(conversions, 0, conversions.Length);
            Array.Clear(samples, 0, samples.Length);
            foreach(var timer in timers) { timer.Reset(); timer.Enabled = false; }
            foreach(var output in Connections.Values) output.Set(false);
            registers[Key(Device.Barometer, 0x19)] = 0x02;
            // A deterministic valid trim set: 25 C and 100000 Pa for the raw
            // sample below, through the firmware's actual compensation math.
            for(byte register = 0x31; register <= 0x45; register++)
                registers[Key(Device.Barometer, register)] = 0;
            registers[Key(Device.Barometer, 0x31)] = 0xa8;
            registers[Key(Device.Barometer, 0x32)] = 0x61;
            registers[Key(Device.Barometer, 0x34)] = 0x40;
            registers[Key(Device.Barometer, 0x37)] = 0x40;
            registers[Key(Device.Barometer, 0x39)] = 0x40;
            registers[Key(Device.Barometer, 0x3c)] = 0xd4;
            registers[Key(Device.Barometer, 0x3d)] = 0x30;
            if(profile != null)
            {
                // Flight-profile trim set: the firmware's datasheet compensation
                // then reduces to P = raw / 128 Pa (PAR_P1 = 2^-7, every other
                // pressure term zero) and T = (raw - 6.4e6) / 65536 C, so the
                // 24-bit raw sample can carry 0..131071 Pa at 1/128 Pa.
                registers[Key(Device.Barometer, 0x36)] = 0x00;
                registers[Key(Device.Barometer, 0x37)] = 0x60;
                registers[Key(Device.Barometer, 0x3c)] = 0x00;
                registers[Key(Device.Barometer, 0x3d)] = 0x00;
            }
        }

        // ---- Flight-profile stimulus -------------------------------------
        // Enabled only when SEDS_FLIGHT_PROFILE names a profile CSV (see
        // scripts/flight_profile.py). Before the launch anchor exists the
        // model reports the profile's first (on-pad) row. The anchor file
        // (SEDS_FLIGHT_LAUNCH) holds the virtual millisecond of T0 on the
        // simulation clock; this model keeps its own 1 kHz virtual clock from
        // machine creation, matching SedsSimulationClock's time base.
        private void LatchBarometer()
        {
            var row = profile.Sample(ProfileTime());
            var pressure = (uint)Math.Max(0, Math.Min(0xffffff, Math.Round(row[SedsFlightProfile.Pressure] * 128.0)));
            var temperature = (uint)Math.Max(0, Math.Min(0xffffff, Math.Round(6400000.0 + row[SedsFlightProfile.Temperature] * 65536.0)));
            latchedBarometer[0] = (byte)pressure; latchedBarometer[1] = (byte)(pressure >> 8); latchedBarometer[2] = (byte)(pressure >> 16);
            latchedBarometer[3] = (byte)temperature; latchedBarometer[4] = (byte)(temperature >> 8); latchedBarometer[5] = (byte)(temperature >> 16);
            profileReads++;
        }

        private void LatchAccelerometer()
        {
            var row = profile.Sample(ProfileTime());
            // BMI088 datasheet scale: full scale 1.5 * 2^(range+1) g over a
            // signed 16-bit sample (FC firmware accl_lsb_to_mps2 reads the
            // range back and uses the same scale in m/s^2). Range 0x03 is
            // +/-24 g (~235 m/s^2); saturation is still recorded.
            var lsb = AccelerometerLsbMps2();
            EncodeAxes(latchedAccelerometer, 0, 0, row[SedsFlightProfile.SpecificForceZ], lsb, ref accelerometerSaturations);
            profileReads++;
        }

        // ACC_RANGE (0x41) resets to 0x01 (+/-6 g) on the real part.
        private double AccelerometerLsbMps2()
        {
            byte stored;
            var range = registers.TryGetValue(Key(Device.Accelerometer, 0x41), out stored) ? stored & 3 : 1;
            return 1.5 * (1 << (range + 1)) / 32768.0 * 9.80665;
        }

        private void LatchGyroscope()
        {
            var row = profile.Sample(ProfileTime());
            // BMI088 gyro: range register 0x0F, 0 => 2000 dps ... 4 => 125 dps, 32767 = full scale.
            var full = 2000.0 / (1 << Math.Min(4, (int)(Get(Device.Gyroscope, 0x0f) & 7)));
            EncodeAxes(latchedGyroscope, row[SedsFlightProfile.GyroX], row[SedsFlightProfile.GyroY], row[SedsFlightProfile.GyroZ], full / 32767.0, ref gyroscopeSaturations);
        }

        private static void EncodeAxes(byte[] output, double x, double y, double z, double unitsPerLsb, ref ulong saturations)
        {
            var axes = new[] { x, y, z };
            for(var i = 0; i < 3; i++)
            {
                var raw = Math.Round(axes[i] / unitsPerLsb);
                if(raw > 32767 || raw < -32768) saturations++;
                var value = (short)Math.Max(-32768, Math.Min(32767, raw));
                output[2 * i] = (byte)value;
                output[2 * i + 1] = (byte)(value >> 8);
            }
        }

        private double ProfileTime()
        {
            if(launchAnchorMs < 0 && profileClockMs >= nextAnchorPollMs)
            {
                nextAnchorPollMs = profileClockMs + 50;
                launchAnchorMs = SedsFlightProfile.ReadAnchor(launchPath);
            }
            // No anchor yet, or before it: the pad (Sample(t < 0)), never row 0.
            return launchAnchorMs < 0 ? -1.0 : (double)profileClockMs - launchAnchorMs;
        }

        public string GetFlightProfileState()
        {
            return string.Format("profile={0} anchor_ms={1} clock_ms={2} reads={3} accel_saturations={4} gyro_saturations={5}",
                profile == null ? "off" : profile.Path, launchAnchorMs, profileClockMs, profileReads, accelerometerSaturations, gyroscopeSaturations);
        }

        private readonly SedsFlightProfile profile;
        private readonly string launchPath;
        private readonly LimitTimer profileClock;
        private ulong profileClockMs;
        private ulong nextAnchorPollMs;
        private long launchAnchorMs = -1;
        private ulong profileReads, accelerometerSaturations, gyroscopeSaturations;
        private readonly byte[] latchedBarometer = new byte[6];
        private readonly byte[] latchedAccelerometer = new byte[6];
        private readonly byte[] latchedGyroscope = new byte[6];

        private byte ReadRegister(Device selected, byte register)
        {
            if(register == 0)
            {
                if(selected == Device.Barometer) return 0x60;
                if(selected == Device.Gyroscope) return 0x0f;
                return 0x1e;
            }
            if(selected == Device.Barometer && register == 0x03) return 0x60;
            if(selected == Device.Barometer && register == 0x11)
            {
                var status = barometerDataReady ? (byte)8 : (byte)0;
                barometerDataReady = false;
                barometerPulse.Enabled = false;
                SetBarometerInterrupt(false);
                return status;
            }
            if(profile != null)
            {
                if(selected == Device.Barometer && register >= 0x04 && register <= 0x09)
                {
                    if(register == 0x04) LatchBarometer();
                    return latchedBarometer[register - 0x04];
                }
                if(selected == Device.Accelerometer && register >= 0x12 && register <= 0x17)
                {
                    if(register == 0x12) LatchAccelerometer();
                    return latchedAccelerometer[register - 0x12];
                }
                if(selected == Device.Gyroscope && register >= 0x02 && register <= 0x07)
                {
                    if(register == 0x02) LatchGyroscope();
                    return latchedGyroscope[register - 0x02];
                }
            }
            // Stable approximately-resting raw samples.
            if(selected == Device.Barometer && register >= 0x04 && register <= 0x09)
                return new byte[] { 0x00, 0x80, 0x65, 0x00, 0x6a, 0x31 }[register - 0x04];
            if(selected == Device.Accelerometer && register >= 0x12 && register <= 0x17)
            {
                // At rest: +1 g on z at the configured range.
                var z = (short)Math.Round(9.80665 / AccelerometerLsbMps2());
                return new byte[] { 0, 0, 0, 0, (byte)z, (byte)(z >> 8) }[register - 0x12];
            }
            if(selected == Device.Gyroscope && register >= 0x02 && register <= 0x07)
                return 0;
            byte value;
            return registers.TryGetValue(Key(selected, register), out value) ? value : (byte)0;
        }

        private static int Key(Device selected, byte register)
        {
            return ((int)selected << 8) | register;
        }

        private readonly Dictionary<int, byte> registers = new Dictionary<int, byte>();
        private Device device;
        private byte address;
        private int position;
        private bool selected;
        private bool sampleRead;
        private readonly LimitTimer[] timers;
        private readonly LimitTimer barometerPulse;
        private bool barometerDataReady;
        private readonly ulong[] conversions = new ulong[3];
        private readonly ulong[] samples = new ulong[3];
        private readonly List<string> initializationTrace = new List<string>();
        private bool reading;
        private bool faulted;
        private ulong transactions;
        private readonly ulong failureEvery;
        private readonly ulong disconnectAfter;
    }

    // Time-indexed flight profile (scripts/flight_profile.py CSV). One parsed
    // copy per path is shared by every machine in the Renode process.
    public sealed class SedsFlightProfile
    {
        public const int Altitude = 0, Velocity = 1, Pressure = 2, Temperature = 3, SpecificForceZ = 4,
            GyroX = 5, GyroY = 6, GyroZ = 7, Latitude = 8, Longitude = 9, AltitudeMsl = 10, Columns = 11;

        public static SedsFlightProfile FromEnvironment()
        {
            var path = Environment.GetEnvironmentVariable("SEDS_FLIGHT_PROFILE");
            if(string.IsNullOrEmpty(path)) return null;
            lock(Cache)
            {
                SedsFlightProfile loaded;
                if(!Cache.TryGetValue(path, out loaded))
                {
                    loaded = new SedsFlightProfile(path);
                    Cache[path] = loaded;
                }
                return loaded;
            }
        }

        private SedsFlightProfile(string path)
        {
            Path = path;
            var culture = System.Globalization.CultureInfo.InvariantCulture;
            var times = new List<double>();
            var rows = new List<double[]>();
            string[] header = null;
            foreach(var raw in System.IO.File.ReadLines(path))
            {
                var line = raw.Trim();
                if(line.Length == 0 || line[0] == '#') continue;
                var parts = line.Split(',');
                if(header == null) { header = parts; continue; }
                Func<string, double> column = name => double.Parse(parts[Array.IndexOf(header, name)], culture);
                times.Add(column("t_ms"));
                rows.Add(new[] { column("alt_agl_m"), column("vel_mps"), column("pressure_pa"), column("temp_c"),
                    column("specific_force_z_mps2"), column("gyro_x_dps"), column("gyro_y_dps"), column("gyro_z_dps"),
                    column("lat"), column("lon"), column("alt_msl_m") });
                if(rows.Count > 1000000) throw new FormatException("flight profile too large");
            }
            if(rows.Count < 2) throw new FormatException("flight profile needs at least two rows");
            for(var i = 1; i < times.Count; i++)
                if(times[i] <= times[i - 1]) throw new FormatException("flight profile times must increase");
            this.times = times.ToArray();
            this.rows = rows.ToArray();
            // Before ignition the vehicle rests on the pad: the first row's
            // position/pressure/temperature, no motion, and +1 g specific
            // force. Row 0 is already the ignition instant (full thrust).
            pad = (double[])this.rows[0].Clone();
            pad[Velocity] = 0.0;
            pad[SpecificForceZ] = 9.80665;
            pad[GyroX] = pad[GyroY] = pad[GyroZ] = 0.0;
        }

        private readonly double[] pad;

        // Pad (at rest) state for any time before T0, including no anchor yet.
        public double[] Pad { get { return pad; } }

        public string Path { get; private set; }

        // Linear interpolation at t milliseconds after T0; clamps at both ends.
        public double[] Sample(double t)
        {
            if(t < 0) return pad;
            if(t <= times[0]) return rows[0];
            if(t >= times[times.Length - 1]) return rows[rows.Length - 1];
            var index = Array.BinarySearch(times, t);
            if(index >= 0) return rows[index];
            var upper = ~index;
            var lower = upper - 1;
            var w = (t - times[lower]) / (times[upper] - times[lower]);
            var result = new double[Columns];
            for(var i = 0; i < Columns; i++) result[i] = rows[lower][i] + w * (rows[upper][i] - rows[lower][i]);
            return result;
        }

        // Virtual millisecond of T0 on the simulation clock, or -1 if absent.
        public static long ReadAnchor(string path)
        {
            if(string.IsNullOrEmpty(path)) return -1;
            try
            {
                if(!System.IO.File.Exists(path)) return -1;
                var text = System.IO.File.ReadAllText(path).Trim();
                long value;
                return text.Length <= 20 && long.TryParse(text, System.Globalization.NumberStyles.None,
                    System.Globalization.CultureInfo.InvariantCulture, out value) ? value : -1;
            }
            catch(System.IO.IOException) { return -1; }
            catch(UnauthorizedAccessException) { return -1; }
        }

        private readonly double[] times;
        private readonly double[][] rows;
        private static readonly Dictionary<string, SedsFlightProfile> Cache = new Dictionary<string, SedsFlightProfile>();
    }

    public sealed class SedsNeoM9N : ISPIPeripheral
    {
        public SedsNeoM9N(IMachine machine, ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue,
            double latitude = 43.0, double longitude = -78.78, double altitudeM = 300.0, string stimulusPath = "")
        {
            if(Double.IsNaN(latitude) || Double.IsInfinity(latitude) || Math.Abs(latitude) > 90.0
                || Double.IsNaN(longitude) || Double.IsInfinity(longitude) || Math.Abs(longitude) > 180.0
                || Double.IsNaN(altitudeM) || Double.IsInfinity(altitudeM))
            {
                throw new ArgumentOutOfRangeException("GPS position must be finite and within geographic bounds");
            }
            this.stimulusPath = stimulusPath ?? "";
            validFix = this.stimulusPath.Length == 0;
            // Format once; 1Hz emission retains bounded storage and work.
            coordinates = FormatCoordinate(latitude, true) + "," + FormatCoordinate(longitude, false);
            altitude = altitudeM.ToString("0.0##", System.Globalization.CultureInfo.InvariantCulture);
            this.failureEvery = failureEvery;
            this.disconnectAfter = disconnectAfter;
            // Optional flight profile: position/altitude follow the trajectory
            // after the launch anchor (see SedsFlightSensorBus).
            profile = SedsFlightProfile.FromEnvironment();
            launchPath = Environment.GetEnvironmentVariable("SEDS_FLIGHT_LAUNCH") ?? "";
            updateTimer = new LimitTimer(machine.ClockSource, 1000, this,
                "gpsFix", limit: 1000, enabled: true, eventEnabled: true);
            updateTimer.LimitReached += () => {
                elapsedSeconds++;
                PollFixStimulus();
                // One pending fix bounds storage when the host stops reading.
                if(output.Count == 0) FillNmea();
            };
            Reset();
        }

        public byte Transmit(byte value)
        {
            if(position++ == 0)
            {
                transactions++;
                faulted = transactions > disconnectAfter || (failureEvery != 0 && transactions % failureEvery == 0);
            }
            if(faulted) return 0xff;
            // Host clocks data with 0xff. Configuration bytes are accepted;
            // the deterministic NMEA stream resumes on subsequent clocks.
            return output.Count == 0 ? (byte)0xff : output.Dequeue();
        }

        public void FinishTransmission() { position = 0; }

        public void Reset()
        {
            output.Clear();
            position = 0;
            transactions = 0;
            faulted = false;
            PollFixStimulus();
            FillNmea();
        }

        public ulong StimulusRejected { get; private set; }
        private void PollFixStimulus()
        {
            if(stimulusPath.Length == 0) return;
            try
            {
                var text = new char[17];
                int count;
                using(var stream = new System.IO.StreamReader(stimulusPath))
                    count = stream.ReadBlock(text, 0, text.Length);
                if(count > 16) throw new FormatException("oversized GPS fix fixture");
                var value = new string(text, 0, count).Trim();
                if(value != "0" && value != "1") throw new FormatException("invalid GPS fix fixture");
                validFix = value == "1";
            }
            catch(System.IO.IOException) { RejectStimulus(); }
            catch(UnauthorizedAccessException) { RejectStimulus(); }
            catch(FormatException) { RejectStimulus(); }
        }

        private void RejectStimulus()
        {
            if(StimulusRejected != ulong.MaxValue) StimulusRejected++;
        }

        private void FillNmea()
        {
            // SEDS_GPS_UTC_OFFSET_MS aligns receiver UTC with the GS network
            // epoch when the host clock starts later than this machine.
            var utc = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                .AddSeconds(elapsedSeconds).AddMilliseconds(GpsUtcOffsetMs);
            var culture = System.Globalization.CultureInfo.InvariantCulture;
            var time = utc.ToString("HHmmss", culture) + ".00";
            var coordinates = this.coordinates;
            var altitude = this.altitude;
            if(profile != null)
            {
                if(launchAnchorMs < 0) launchAnchorMs = SedsFlightProfile.ReadAnchor(launchPath);
                var nowMs = (double)elapsedSeconds * 1000.0;
                var row = profile.Sample(launchAnchorMs < 0 ? -1.0 : nowMs - launchAnchorMs);
                coordinates = FormatCoordinate(row[SedsFlightProfile.Latitude], true) + "," + FormatCoordinate(row[SedsFlightProfile.Longitude], false);
                altitude = row[SedsFlightProfile.AltitudeMsl].ToString("0.0", culture);
            }
            EnqueueNmea("GNGGA," + time + "," + coordinates + (validFix ? ",1,12,0.8," : ",0,00,99.9,") + altitude + ",M,-34.0,M,,");
            EnqueueNmea("GNRMC," + time + (validFix ? ",A," : ",V,") + coordinates + ",0.0,0.0,"
                + utc.ToString("ddMMyy", culture) + (validFix ? ",,,A" : ",,,N"));
        }

        private static string FormatCoordinate(double value, bool latitude)
        {
            var absolute = Math.Abs(value);
            var degrees = (int)Math.Floor(absolute);
            var minutes = Math.Round((absolute - degrees) * 60.0, 4, MidpointRounding.AwayFromZero);
            if(minutes >= 60.0) { degrees++; minutes = 0; }
            var culture = System.Globalization.CultureInfo.InvariantCulture;
            return degrees.ToString(latitude ? "00" : "000", culture)
                + minutes.ToString("00.0000", culture) + ","
                + (latitude ? (value < 0 ? "S" : "N") : (value < 0 ? "W" : "E"));
        }

        private void EnqueueNmea(string body)
        {
            byte checksum = 0;
            foreach(var character in body) checksum ^= (byte)character;
            var sentence = "$" + body + "*" + checksum.ToString("X2",
                System.Globalization.CultureInfo.InvariantCulture) + "\r\n";
            foreach(var character in sentence) output.Enqueue((byte)character);
        }

        private static readonly long GpsUtcOffsetMs = ParseOffset(Environment.GetEnvironmentVariable("SEDS_GPS_UTC_OFFSET_MS"));
        private static long ParseOffset(string text)
        {
            long value;
            return long.TryParse(text, System.Globalization.NumberStyles.AllowLeadingSign,
                System.Globalization.CultureInfo.InvariantCulture, out value) && Math.Abs(value) <= 3600000 ? value : 0;
        }
        private readonly SedsFlightProfile profile;
        private readonly string launchPath;
        private long launchAnchorMs = -1;
        private readonly string stimulusPath;
        private bool validFix;
        private readonly string coordinates;
        private readonly string altitude;

        // External GPS time continues across an MCU reset and SPI polling gaps.
        private readonly LimitTimer updateTimer;
        private ulong elapsedSeconds;
        private readonly Queue<byte> output = new Queue<byte>();
        private int position;
        private ulong transactions;
        private bool faulted;
        private readonly ulong failureEvery;
        private readonly ulong disconnectAfter;
    }

    // MCP3564R 24-bit sigma-delta ADC. The firmware exercises the real SPI
    // command/configuration path and reads deterministic, slowly varying raw
    // conversion values through the STM32 SPI/DMA peripherals.
    public sealed class SedsMcp3564r : ISPIPeripheral, IGPIOReceiver
    {
        public SedsMcp3564r(IMachine machine, ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue, bool externalChipSelect = false, string stimulusPath = "")
        {
            IRQ = new GPIO();
            if(machine != null)
            {
                conversion = new LimitTimer(machine.ClockSource, 1000000, this,
                    "adcConversion", limit: 1, enabled: false, eventEnabled: true,
                    workMode: Antmicro.Renode.Time.WorkMode.OneShot,
                    direction: Antmicro.Renode.Time.Direction.Ascending);
                conversion.LimitReached += Convert;
                readyRelease = new LimitTimer(machine.ClockSource, 1000000, this,
                    "adcReadyRelease", limit: 1, enabled: false, eventEnabled: true,
                    workMode: Antmicro.Renode.Time.WorkMode.OneShot,
                    direction: Antmicro.Renode.Time.Direction.Ascending);
                readyRelease.LimitReached += () => IRQ.Set(true);
            }
            this.stimulusPath = stimulusPath ?? "";
            this.externalChipSelect = externalChipSelect;
            this.failureEvery = failureEvery;
            this.disconnectAfter = disconnectAfter;
            Reset();
        }

        public byte Transmit(byte value)
        {
            if(externalChipSelect && !selected) return 0xff;
            if(position == 0)
            {
                transactions++;
                faulted = transactions > disconnectAfter
                    || (failureEvery != 0 && transactions % failureEvery == 0);
                command = value;
                registerAddress = (value >> 2) & 0x0f;
                registerByte = 0;
                reading = (value & 0x03) == 0x01;
                var dataReadyBeforeRead = dataReady;
                if(reading && registerAddress == 0)
                {
                    if(conversion == null) ConvertSample();
                    // ADCDATA is latched for the entire SPI transaction.
                    dataReady = false;
                    IRQ.Set(true);
                    var format = (Register(4) >> 4) & 3;
                    dataBytes = format == 0 ? 3 : 4;
                    var code = latchedCode & 0x00ffffffu;
                    var signedCode = (code & 0x00800000u) != 0 ? code | 0xff000000u : code;
                    data = format == 0 ? code : format == 1 ? code << 8
                        : format == 2 ? signedCode : ((ScanMask() != 0 ? (uint)latchedChannel : 0u) << 28) | (signedCode & 0x0fffffffu);
                }
                // DS20006391C6.2.6: device address01 has STAT[3] opposite
                // to address bit0. CRC/POR inactive plus DR gives 0x13/0x17.
                var status = (byte)(0x13 | (dataReadyBeforeRead ? 0 : 4));
                if(value == 0x78) ResetRegisters();
                if(value == 0x68) StartConversion();
                position++;
                // Legacy register-format tests explicitly use a null machine.
                return faulted ? (byte)0xff : conversion == null ? (byte)0 : status;
            }

            if(faulted) return 0xff;
            if(reading && registerAddress == 0)
            {
                var shift = 8 * (dataBytes - 1 - Math.Min(position - 1, dataBytes - 1));
                var result = (byte)((data >> shift) & 0xff);
                position++;
                if(position == dataBytes + 1)
                {
                    if(conversion == null) AdvanceChannel();
                }
                return result;
            }

            // Increment across register boundaries during a multi-register
            // write (CONFIG0..MUX are each one byte; SCAN/TIMER are three).
            var key = (registerAddress << 2) + registerByte;
            byte output = 0;
            if(reading) registers.TryGetValue(key, out output);
            else
            {
                registers[key] = value;
                if(registerAddress == 1 && (value & 3) != 3) StopConversion();
                else if(registerAddress == 1 && conversion != null && (value & 3) == 3) StartConversion();
            }
            registerByte++;
            if(registerByte == RegisterWidth(registerAddress))
            {
                registerByte = 0;
                registerAddress = (registerAddress + 1) & 0x0f;
            }
            position++;
            return output;
        }

        public GPIO IRQ { get; private set; }

        private void ConvertSample()
        {
            PollStimulus();
            SelectChannel();
            latchedChannel = channel;
            latchedCode = samples[channel];
        }

        private void AdvanceChannel()
        {
            if(stimulusPath.Length == 0 && channel < 12)
                samples[channel] = (samples[channel] + 17u) & 0x00ffffffu;
            channel = (channel + 15) & 15;
        }

        private void Convert()
        {
            if(!running) return;
            ConvertSample();
            AdvanceChannel();
            dataReady = true;
            if(transactions <= disconnectAfter) IRQ.Set(false);
            ArmConversion();
        }

        private void StartConversion()
        {
            channel = 15;
            dataReady = false;
            IRQ.Set(true);
            running = true;
            ArmConversion();
        }

        private void StopConversion()
        {
            running = false;
            dataReady = false;
            IRQ.Set(true);
            if(conversion != null) conversion.Enabled = false;
            if(readyRelease != null) readyRelease.Enabled = false;
        }

        private void ArmConversion()
        {
            if(conversion == null) return;
            // SCAN settles each channel; AZ_MUX takes both polarities.
            // 16MHz board MCLK / PRE / 4 = DMCLK. No analog/noise model.
            var config1 = Register(2);
            var osr = OsrTable[(config1 >> 2) & 15];
            var clocks = osr <= 512 ? 3 * osr : osr + 1024;
            var az = (Register(3) & 4) != 0 ? 2UL : 1UL;
            var prescaler = 1UL << (config1 >> 6);
            conversion.Reset();
            conversion.Limit = Math.Max(1UL, (clocks * az * prescaler + 3) / 4);
            conversion.Value = 0;
            conversion.Enabled = true;
            // DR pin releases 16 DMCLK before the next conversion even if
            // software never reads ADCDATA (DS20006391C section6.8.1).
            readyRelease.Reset();
            readyRelease.Limit = Math.Max(1UL, conversion.Limit - Math.Min(conversion.Limit - 1, 4 * prescaler));
            readyRelease.Value = 0;
            readyRelease.Enabled = true;
        }

        // Codes are the raw 24-bit two's-complement ADC representation, not calibrated units.
        public void SetChannelSample(uint channel, uint value)
        {
            if(channel >= samples.Length || value > 0x00ffffffu)
                throw new ArgumentOutOfRangeException("sample");
            samples[channel] = value;
        }

        public ulong StimulusRejected { get; private set; }
        private void PollStimulus()
        {
            // No fixture means no new filesystem operations and exact legacy drift.
            // Ten conversion reads are approximately20ms at500Hz aggregate scan rate.
            if(stimulusPath.Length == 0 || stimulusReads++ % 10 != 0) return;
            try
            {
                var text = new char[1025];
                int count;
                using(var stream = new System.IO.StreamReader(stimulusPath))
                    count = stream.ReadBlock(text, 0, text.Length);
                if(count > 1024) throw new FormatException("oversized sensor fixture");
                var parts = new string(text, 0, count).Trim().Split(',');
                if(parts.Length != samples.Length) throw new FormatException("channel count");
                var next = new uint[samples.Length];
                for(var i = 0; i < next.Length; i++)
                    if(!uint.TryParse(parts[i], System.Globalization.NumberStyles.None,
                        System.Globalization.CultureInfo.InvariantCulture, out next[i]) || next[i] > 0x00ffffffu)
                        throw new FormatException("invalid sensor sample");
                Array.Copy(next, samples, samples.Length);
            }
            catch(System.IO.IOException) { RejectStimulus(); }
            catch(UnauthorizedAccessException) { RejectStimulus(); }
            catch(FormatException) { RejectStimulus(); }
        }

        private void RejectStimulus()
        {
            if(StimulusRejected != ulong.MaxValue) StimulusRejected++;
        }

        private byte Register(int address)
        {
            byte value;
            return registers.TryGetValue(address << 2, out value) ? value : (byte)0;
        }

        private static int RegisterWidth(int address)
        {
            return address == 0 ? 4 : address >= 7 && address <= 10 ? 3 : address == 15 ? 2 : 1;
        }

        private ushort ScanMask()
        {
            // SCAN's low 16 bits select single-ended, differential, and
            // internal channels. The high byte contains scan timing options.
            byte high, low;
            registers.TryGetValue((7 << 2) + 1, out high);
            registers.TryGetValue((7 << 2) + 2, out low);
            return (ushort)((high << 8) | low);
        }

        private void SelectChannel()
        {
            var mask = ScanMask();
            if(mask == 0)
            {
                channel = (Register(6) >> 4) & 7;
                return;
            }
            for(var i = 0; i < 16; i++)
            {
                if((mask & (1 << channel)) != 0) return;
                channel = (channel + 15) & 15;
            }
        }

        public void FinishTransmission()
        {
            // HAL splits command and DMA data into controller transfers while
            // the GPIO-controlled chip select remains low throughout.
            if(externalChipSelect) return;
            position = 0;
            reading = false;
        }

        public void OnGPIO(int number, bool value)
        {
            if(number != 0) throw new ArgumentOutOfRangeException(nameof(number));
            selected = !value;
            if(value) { position = 0; reading = false; }
        }

        public void Reset()
        {
            selected = !externalChipSelect;
            transactions = 0;
            stimulusReads = 0;
            faulted = false;
            position = 0;
            reading = false;
            ResetRegisters();
        }

        private void ResetRegisters()
        {
            StopConversion();
            registers.Clear();
            for(var i = 0; i < samples.Length; i++) samples[i] = ((uint)(i + 1) * 0x100000u) & 0x00ffffffu;
            // IDs 8..11 are the four differential channel pairs. In
            // particular, ID 9 is the DAQ's P6 CH2-CH3 input.
            for(var i = 8; i < 12; i++) samples[i] = (samples[2 * (i - 8)] - samples[2 * (i - 8) + 1]) & 0x00ffffffu;
            samples[12] = 305652u; // Nominal 25 C die-sensor fixture at the board's 2.4 V reference.
            channel = 15; // SCAN visits enabled inputs from MSb to LSb.
        }

        private static readonly ulong[] OsrTable = {32,64,128,256,512,1024,2048,4096,8192,16384,20480,24576,40960,49152,81920,98304};
        private readonly LimitTimer conversion, readyRelease;
        private bool running, dataReady;
        private int latchedChannel;
        private uint latchedCode;
        private readonly string stimulusPath;
        private ulong stimulusReads;
        private readonly Dictionary<int, byte> registers = new Dictionary<int, byte>();
        private readonly bool externalChipSelect;
        private bool selected;
        private byte command;
        private int position;
        private bool reading;
        private bool faulted;
        private readonly uint[] samples = new uint[16];
        private int channel;
        private int registerAddress;
        private int registerByte;
        private int dataBytes;
        private uint data;
        private ulong transactions;
        private readonly ulong failureEvery;
        private readonly ulong disconnectAfter;
    }
}
