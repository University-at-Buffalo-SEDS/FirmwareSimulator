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
            Connections = new Dictionary<int, IGPIO> { {0, new GPIO()}, {1, new GPIO()}, {2, new GPIO()} };
            timers = new LimitTimer[3];
            for(var i = 0; i < 3; i++)
            {
                var sensor = i;
                timers[i] = new LimitTimer(machine.ClockSource, 3200000, this,
                    "conversion" + i, limit: 32000, enabled: false, eventEnabled: true);
                timers[i].LimitReached += () => {
                    conversions[sensor]++;
                    if(transactions <= disconnectAfter) Connections[sensor].Set(true);
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
                if(sampleRead && !faulted) { samples[number]++; Connections[number].Set(false); }
                selected = false;
                position = 0;
            }
        }

        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

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
            Array.Clear(conversions, 0, conversions.Length);
            Array.Clear(samples, 0, samples.Length);
            foreach(var timer in timers) { timer.Reset(); timer.Enabled = false; }
            foreach(var output in Connections.Values) output.Set(false);
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
        }

        private byte ReadRegister(Device selected, byte register)
        {
            if(register == 0)
            {
                if(selected == Device.Barometer) return 0x60;
                if(selected == Device.Gyroscope) return 0x0f;
                return 0x1e;
            }
            if(selected == Device.Barometer && register == 0x03) return 0x60;
            // Stable approximately-resting raw samples.
            if(selected == Device.Barometer && register >= 0x04 && register <= 0x09)
                return new byte[] { 0x00, 0x80, 0x65, 0x00, 0x6a, 0x31 }[register - 0x04];
            if(selected == Device.Accelerometer && register >= 0x12 && register <= 0x17)
                return new byte[] { 0, 0, 0, 0, 0x00, 0x40 }[register - 0x12];
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
        private readonly ulong[] conversions = new ulong[3];
        private readonly ulong[] samples = new ulong[3];
        private readonly List<string> initializationTrace = new List<string>();
        private bool reading;
        private bool faulted;
        private ulong transactions;
        private readonly ulong failureEvery;
        private readonly ulong disconnectAfter;
    }

    public sealed class SedsNeoM9N : ISPIPeripheral
    {
        public SedsNeoM9N(ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue)
        {
            this.failureEvery = failureEvery;
            this.disconnectAfter = disconnectAfter;
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
            if(output.Count == 0) FillNmea();
            return output.Dequeue();
        }

        public void FinishTransmission() { position = 0; }

        public void Reset()
        {
            output.Clear();
            position = 0;
            transactions = 0;
            faulted = false;
            FillNmea();
        }

        private void FillNmea()
        {
            const string sentences =
                "$GNGGA,120000.00,4300.0000,N,07846.8000,W,1,12,0.8,300.0,M,-34.0,M,,*6E\r\n" +
                "$GNRMC,120000.00,A,4300.0000,N,07846.8000,W,0.0,0.0,290826,,,A*65\r\n";
            foreach(var character in sentences) output.Enqueue((byte)character);
        }

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
        public SedsMcp3564r(ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue, bool externalChipSelect = false)
        {
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
                if(reading && registerAddress == 0)
                {
                    SelectChannel();
                    var format = (Register(4) >> 4) & 3;
                    dataBytes = format == 0 ? 3 : 4;
                    var code = samples[channel] & 0x00ffffffu;
                    var signedCode = (code & 0x00800000u) != 0 ? code | 0xff000000u : code;
                    data = format == 0 ? code : format == 1 ? code << 8
                        : format == 2 ? signedCode : ((ScanMask() != 0 ? (uint)channel : 0u) << 28) | (signedCode & 0x0fffffffu);
                }
                if(value == 0x78) ResetRegisters();
                position++;
                return faulted ? (byte)0xff : (byte)0;
            }

            if(faulted) return 0xff;
            if(reading && registerAddress == 0)
            {
                var shift = 8 * (dataBytes - 1 - Math.Min(position - 1, dataBytes - 1));
                var result = (byte)((data >> shift) & 0xff);
                position++;
                if(position == dataBytes + 1)
                {
                    samples[channel] = (samples[channel] + 17u) & 0x00ffffffu;
                    channel = (channel + 7) & 7;
                }
                return result;
            }

            // Increment across register boundaries during a multi-register
            // write (CONFIG0..MUX are each one byte; SCAN/TIMER are three).
            var key = (registerAddress << 2) + registerByte;
            byte output = 0;
            if(reading) registers.TryGetValue(key, out output);
            else registers[key] = value;
            registerByte++;
            if(registerByte == RegisterWidth(registerAddress))
            {
                registerByte = 0;
                registerAddress = (registerAddress + 1) & 0x0f;
            }
            position++;
            return output;
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

        private byte ScanMask()
        {
            byte mask;
            return registers.TryGetValue((7 << 2) + 2, out mask) ? mask : (byte)0;
        }

        private void SelectChannel()
        {
            var mask = ScanMask();
            if(mask == 0)
            {
                channel = (Register(6) >> 4) & 7;
                return;
            }
            for(var i = 0; i < 8; i++)
            {
                if((mask & (1 << channel)) != 0) return;
                channel = (channel + 7) & 7;
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
            faulted = false;
            position = 0;
            reading = false;
            ResetRegisters();
        }

        private void ResetRegisters()
        {
            registers.Clear();
            for(var i = 0; i < samples.Length; i++) samples[i] = (uint)(i + 1) * 0x100000u;
            channel = 7; // SCAN visits enabled inputs from MSb to LSb.
        }

        private readonly Dictionary<int, byte> registers = new Dictionary<int, byte>();
        private readonly bool externalChipSelect;
        private bool selected;
        private byte command;
        private int position;
        private bool reading;
        private bool faulted;
        private readonly uint[] samples = new uint[8];
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
