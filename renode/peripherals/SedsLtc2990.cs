using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public sealed class SedsLtc2990 : II2CPeripheral
    {
        public SedsLtc2990(ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue, string samples = "")
        {
            this.failureEvery = failureEvery;
            this.disconnectAfter = disconnectAfter;
            channelCodes = new ushort[] {4096,4096,4096,4096};
            if(!String.IsNullOrEmpty(samples))
            {
                var values = samples.Split(',');
                if(values.Length != 4) throw new ArgumentException("LTC2990 needs four conversion codes");
                for(var index=0;index<4;index++)
                {
                    if(!UInt16.TryParse(values[index],out channelCodes[index]) || channelCodes[index]>0x7fff)
                        throw new ArgumentException("LTC2990 conversion code must fit 15 bits");
                }
            }
            Reset();
        }

        public void Write(byte[] data)
        {
            if(data == null || data.Length == 0) return;
            // STM32 HAL memory writes may use RELOAD: Renode delivers the
            // register address and value in separate Write calls before STOP.
            for(var index = 0; index < data.Length; index++)
            {
                if(expectingRegister)
                {
                    pointer=data[index];
                    expectingRegister=false;
                    continue;
                }
                var register=pointer++;
                registers[register] = data[index];
                if(register==0x02) CompleteConversion();
            }
        }

        public byte[] Read(int count)
        {
            // Repeated START changes direction. Preserve the selected read
            // pointer but the next write begins with a new register address.
            expectingRegister=true;
            var result = new byte[count];
            accesses++;
            if(accesses > disconnectAfter || (failureEvery != 0 && accesses % failureEvery == 0))
            {
                // Invalid conversion/status, not 0xff (which falsely sets valid).
                // Electrical NACK behavior belongs to the I2C controller model.
                for(var index = 0; index < count; index++) result[index] = 0;
                return result;
            }
            for(var index = 0; index < count; index++)
            {
                var register=pointer++;
                result[index] = registers[register];
                // Rev. F: reading a result MSB clears its ready/DV flags.
                // Return the latched byte before clearing so split MSB/LSB
                // transactions see the same conversion as the real driver.
                if(register>=0x06 && register<=0x0c && (register&1)==0)
                {
                    registers[0x00] &= (byte)~(1 << (2+(register-0x06)/2));
                    registers[register] &= 0x7f;
                }
            }
            return result;
        }

        public void FinishTransmission() { expectingRegister=true; }

        public void Reset()
        {
            Array.Clear(registers, 0, registers.Length);
            pointer = 0;
            expectingRegister=true;
            accesses = 0;
        }

        // Supports the voltage (mode 7) and dual-differential (mode 6)
        // conversions exercised by the populated boards.
        private void CompleteConversion()
        {
            var ready=(registers[1]&7)==6 ? 0x14 : 0x3c;
            registers[0x00]=(byte)(ready|((registers[1]&0x40)==0?1:0));
            for(var channel=0;channel<4;channel++)
            {
                var register=6+2*channel;
                var valid=(ready&(1<<(2+channel)))!=0;
                var value=(ushort)((valid?0x8000:0)|channelCodes[channel]);
                registers[register]=(byte)(value>>8);
                registers[register+1]=(byte)value;
            }
        }

        private readonly ushort[] channelCodes;
        private readonly byte[] registers = new byte[256];
        private byte pointer;
        private bool expectingRegister;
        private ulong accesses;
        private readonly ulong failureEvery;
        private readonly ulong disconnectAfter;
    }
}
