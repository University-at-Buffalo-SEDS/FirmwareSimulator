using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Timers;
namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // stdout belongs to this exact executor child. No ambient clock file is read.
    public sealed class SedsOwnedSimulationClock : IPeripheral
    {
        public SedsOwnedSimulationClock(IMachine machine, string path)
        {
            timer = new LimitTimer(machine.ClockSource, 1000, this, "ownedClock",
                limit: 1, direction: Direction.Ascending, enabled: true, eventEnabled: true);
            timer.LimitReached += () => {
                if(elapsed == ulong.MaxValue) throw new InvalidOperationException("owned clock exhausted");
                elapsed++;
                if(elapsed % 10 == 0) Console.WriteLine("SEDS_OWNED_CLOCK " + elapsed);
            };
        }
        // Board reset cannot reset the execution instance clock.
        public void Reset() { }
        private readonly LimitTimer timer;
        private ulong elapsed;
    }
}
