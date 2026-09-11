using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.SPI;

namespace Antmicro.Renode.Peripherals.Sensors
{
    // Deterministic SPI models used by the real board drivers. These models
    // intentionally implement wire/register behavior, not a HAL shortcut.
    public sealed class SedsFlightSensorBus : ISPIPeripheral
    {
        private enum Device { Barometer, Gyroscope, Accelerometer }

        public SedsFlightSensorBus(ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue)
        {
            this.failureEvery = failureEvery;
            this.disconnectAfter = disconnectAfter;
            Reset();
        }

        public byte Transmit(byte value)
        {
            if(position == 0)
            {
                transactions++;
                faulted = transactions > disconnectAfter || (failureEvery != 0 && transactions % failureEvery == 0);
                address = (byte)(value & 0x7f);
                reading = (value & 0x80) != 0;
                if(reading && address == 0)
                {
                    zeroReads++;
                    if(zeroReads == 2) device = Device.Gyroscope;
                    else if(zeroReads >= 3) device = Device.Accelerometer;
                }
                position++;
                return 0;
            }

            if(faulted) return 0xff;

            if(!reading)
            {
                registers[Key(device, address)] = value;
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
            position = 0;
        }

        public void Reset()
        {
            registers.Clear();
            device = Device.Barometer;
            address = 0;
            position = 0;
            zeroReads = 0;
            reading = false;
            transactions = 0;
            faulted = false;
            // BMP390 calibration bytes: deterministic non-zero coefficients.
            for(byte register = 0x31; register <= 0x45; register++)
                registers[Key(Device.Barometer, register)] = (byte)(0x20 + register);
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
                return new byte[] { 0x00, 0x80, 0x65, 0x00, 0x80, 0x65 }[register - 0x04];
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
        private int zeroReads;
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
                reading = (value & 0x03) == 0x01;
                if(value == 0x78) ResetRegisters();
                position++;
                return faulted ? (byte)0xff : (byte)0;
            }

            if(faulted) return 0xff;
            if(reading)
            {
                var shift = 16 - 8 * Math.Min(position - 1, 2);
                var result = (byte)((sample >> shift) & 0xff);
                position++;
                if(position > 3)
                {
                    sample = (sample + 17u) & 0x00ffffffu;
                }
                return result;
            }

            // Configuration writes are retained so subsequent transactions
            // observe the same register state as the physical ADC.
            var address = (byte)((command >> 2) & 0x0f);
            registers[((int)address << 2) + Math.Min(position - 1, 3)] = value;
            position++;
            return 0;
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
            sample = 0x100000u;
        }

        private readonly Dictionary<int, byte> registers = new Dictionary<int, byte>();
        private readonly bool externalChipSelect;
        private bool selected;
        private byte command;
        private int position;
        private bool reading;
        private bool faulted;
        private uint sample;
        private ulong transactions;
        private readonly ulong failureEvery;
        private readonly ulong disconnectAfter;
    }
}
