using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.IRQControllers
{
    // H5/U5 GPIO EXTI lines. Input number is port*16+pin; EXTICR chooses
    // the port. Pending edges latch independently of the interrupt mask.
    public sealed class SedsStm32Exti : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, INumberedGPIOOutput
    {
        public SedsStm32Exti()
        {
            var outputs = new Dictionary<int, IGPIO>();
            for(var i=0; i<16; i++) outputs[i] = new GPIO();
            Connections = outputs;
        }
        public long Size { get { return 0x400; } }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }
        public uint ReadDoubleWord(long offset) { uint value; return registers.TryGetValue(offset, out value) ? value : 0; }
        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0x0c || offset == 0x10) registers[offset] = ReadDoubleWord(offset) & ~value;
            else if(offset == 8) registers[0x0c] = ReadDoubleWord(0x0c) | value;
            else registers[offset] = value;
            Update();
        }
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= levels.Length) throw new ArgumentOutOfRangeException(nameof(number));
            var previous = levels[number]; levels[number] = value;
            var pin = number % 16;
            var port = (ReadDoubleWord(0x60 + (pin / 4)*4) >> ((pin%4)*8)) & 15;
            if(port != number/16 || previous == value) return;
            uint bit = 1u << pin;
            var trigger = value ? 0 : 4;
            var pending = value ? 0x0c : 0x10;
            if((ReadDoubleWord(trigger) & bit) != 0) registers[pending] = ReadDoubleWord(pending) | bit;
            Update();
        }
        private void Update()
        {
            var pending = (ReadDoubleWord(0x0c) | ReadDoubleWord(0x10)) & ReadDoubleWord(0x80);
            for(var i=0; i<16; i++) Connections[i].Set((pending & (1u << i)) != 0);
        }
        public void Reset() { registers.Clear(); Array.Clear(levels,0,levels.Length); Update(); }
        private readonly Dictionary<long,uint> registers = new Dictionary<long,uint>();
        private readonly bool[] levels = new bool[128];
    }
}
