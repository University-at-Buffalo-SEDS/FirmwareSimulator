using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.GPIOPort
{
    // STM32 GPIO bank with one 0x400-byte window per port. It implements the
    // register behavior relied on by Cube HAL, including atomic BSRR/BRR
    // updates. Unsupported offsets fail closed instead of silently acting as
    // RAM. External pin voltages remain a board-level concern.
    public sealed class SedsStm32GpioBank : IDoubleWordPeripheral, IKnownSize,
        INumberedGPIOOutput, IGPIOReceiver
    {
        public SedsStm32GpioBank(uint ports = 8)
        {
            this.ports = ports;
            inputs = new uint[ports];
            driven = new uint[ports];
            Connections = Enumerable.Range(0, checked((int)ports * 16))
                .ToDictionary(index => index, _ => (IGPIO)new GPIO());
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            var key = Validate(offset);
            var register = key & 0x3ff;
            if(register == 0x10) // IDR: output pins read back their driven state.
            {
                var outputKey = (key & ~0x3ffL) | 0x14;
                var modeKey = key & ~0x3ffL;
                uint output;
                uint mode;
                registers.TryGetValue(outputKey, out output);
                registers.TryGetValue(modeKey, out mode);
                var port = (int)(key >> 10);
                uint inputValue = 0;
                for(var pin = 0; pin < 16; pin++)
                {
                    var isInput = ((mode >> (pin * 2)) & 3u) == 0;
                    var mask = 1u << pin;
                    if(isInput || IsOpenDrainReleased(key, pin, output))
                    {
                        if((driven[port] & mask) != 0) inputValue |= inputs[port] & mask;
                        else if(PullIsUp(key, pin)) inputValue |= mask;
                    }
                    else inputValue |= output & mask;
                }
                return inputValue;
            }
            if(register == 0x18 || register == 0x28) return 0; // write-only
            uint value;
            return registers.TryGetValue(key, out value) ? value : 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            var key = Validate(offset);
            var register = key & 0x3ff;
            var outputKey = (key & ~0x3ffL) | 0x14;
            if(register == 0x18) // BSRR
            {
                uint output;
                registers.TryGetValue(outputKey, out output);
                output |= value & 0xffff;
                output &= ~((value >> 16) & 0xffff);
                registers[outputKey] = output;
                UpdateOutputs(key >> 10, output);
                return;
            }
            if(register == 0x28) // BRR
            {
                uint output;
                registers.TryGetValue(outputKey, out output);
                registers[outputKey] = output & ~(value & 0xffff);
                UpdateOutputs(key >> 10, registers[outputKey]);
                return;
            }
            if(register == 0x10) return; // IDR is hardware-driven.
            registers[key] = value;
            if(register == 0x14) UpdateOutputs(key >> 10, value);
        }

        public void OnGPIO(int number, bool value)
        {
            DrivePin(number, value);
        }

        public void DrivePin(int number, bool value)
        {
            if(number < 0 || number >= checked((int)ports * 16)) return;
            var port = number / 16;
            var pin = number % 16;
            driven[port] |= 1u << pin;
            if(value) inputs[port] |= 1u << pin;
            else inputs[port] &= ~(1u << pin);
        }

        public void ReleasePin(int number)
        {
            if(number < 0 || number >= checked((int)ports * 16)) return;
            driven[number / 16] &= ~(1u << (number % 16));
        }

        public void Reset()
        {
            registers.Clear();
            Array.Clear(inputs, 0, inputs.Length);
            Array.Clear(driven, 0, driven.Length);
            foreach(var connection in Connections.Values) connection.Set(false);
        }

        public long Size { get { return ports * 0x400L; } }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        private long Validate(long offset)
        {
            if(offset < 0 || offset >= Size || (offset & 3) != 0)
                throw new ArgumentOutOfRangeException("offset", "invalid GPIO access");
            var register = offset & 0x3ff;
            // MODER through ASCR plus the U5/H5 security/privilege registers.
            if(register > 0x30)
                throw new NotSupportedException(String.Format("unsupported GPIO register offset 0x{0:x}", offset));
            return offset;
        }

        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private void UpdateOutputs(long port, uint value)
        {
            for(var pin = 0; pin < 16; pin++)
                Connections[checked((int)port * 16 + pin)].Set((value & (1u << pin)) != 0);
        }

        private readonly uint[] inputs;
        private readonly uint[] driven;
        private readonly uint ports;

        private bool PullIsUp(long key, int pin)
        {
            uint pulls;
            registers.TryGetValue((key & ~0x3ffL) | 0x0c, out pulls);
            return ((pulls >> (pin * 2)) & 3u) == 1u;
        }

        private bool IsOpenDrainReleased(long key, int pin, uint output)
        {
            uint type;
            registers.TryGetValue((key & ~0x3ffL) | 0x04, out type);
            return (type & (1u << pin)) != 0 && (output & (1u << pin)) != 0;
        }
    }
}
