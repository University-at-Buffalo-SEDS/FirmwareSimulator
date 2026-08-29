using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.SPI;

namespace Antmicro.Renode.Peripherals.Sensors
{
    // Deterministic SPI models used by the real board drivers. These models
    // intentionally implement wire/register behavior, not a HAL shortcut.
    public sealed class SedsFlightSensorBus : ISPIPeripheral
    {
        private enum Device { Barometer, Gyroscope, Accelerometer }

        public SedsFlightSensorBus()
        {
            Reset();
        }

        public byte Transmit(byte value)
        {
            if(position == 0)
            {
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
    }

    public sealed class SedsNeoM9N : ISPIPeripheral
    {
        public SedsNeoM9N()
        {
            Reset();
        }

        public byte Transmit(byte value)
        {
            // Host clocks data with 0xff. Configuration bytes are accepted;
            // the deterministic NMEA stream resumes on subsequent clocks.
            if(output.Count == 0) FillNmea();
            return output.Dequeue();
        }

        public void FinishTransmission() { }

        public void Reset()
        {
            output.Clear();
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
    }
}
