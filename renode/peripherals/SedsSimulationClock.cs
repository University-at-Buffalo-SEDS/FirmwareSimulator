using System.IO;
using Antmicro.Renode.Core;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Timers;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // A host backend must not age routes faster than the emulated MCUs.
    // Ten-millisecond ticks share the machine clock, including pauses; the
    // counter deliberately survives board resets because simulation time does.
    public sealed class SedsSimulationClock : IPeripheral
    {
        public SedsSimulationClock(IMachine machine, string path)
        {
            timer = new LimitTimer(machine.ClockSource, 1000, this, "hostClock",
                limit: 10, direction: Direction.Ascending, enabled: true, eventEnabled: true);
            timer.LimitReached += () => {
                elapsed += 10;
                File.WriteAllText(path, elapsed.ToString(System.Globalization.CultureInfo.InvariantCulture));
            };
        }
        public void Reset() { }
        private readonly LimitTimer timer;
        private ulong elapsed;
    }
}
