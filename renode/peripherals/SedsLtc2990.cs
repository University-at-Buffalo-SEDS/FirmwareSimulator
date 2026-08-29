using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public sealed class SedsLtc2990 : II2CPeripheral
    {
        public SedsLtc2990()
        {
            Reset();
        }

        public void Write(byte[] data)
        {
            if(data == null || data.Length == 0) return;
            pointer = data[0];
            for(var index = 1; index < data.Length; index++)
                registers[pointer++] = data[index];
        }

        public byte[] Read(int count)
        {
            var result = new byte[count];
            for(var index = 0; index < count; index++)
                result[index] = registers[pointer++];
            return result;
        }

        public void FinishTransmission() { }

        public void Reset()
        {
            Array.Clear(registers, 0, registers.Length);
            pointer = 0;
            registers[0x00] = 0x0f;
            // Valid, deterministic rail/current conversions.
            for(var register = 0x06; register <= 0x0d; register += 2)
            {
                registers[register] = 0x90;
                registers[register + 1] = 0x00;
            }
        }

        private readonly byte[] registers = new byte[256];
        private byte pointer;
    }
}
