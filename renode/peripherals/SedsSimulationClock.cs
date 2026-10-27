using System.IO;
using Antmicro.Renode.Core;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Timers;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // A host backend must not age routes faster than the emulated MCUs.
    // Millisecond ticks share the machine clock, including pauses; the
    // counter deliberately survives board resets because simulation time does.
    public sealed class SedsSimulationClock : IPeripheral
    {
        public SedsSimulationClock(IMachine machine, string path)
        {
            timer = new LimitTimer(machine.ClockSource, 1000, this, "hostClock",
                limit: 1, direction: Direction.Ascending, enabled: true, eventEnabled: true);
            timer.LimitReached += () => {
                elapsed += 1;
                // Readers must see a complete old or new snapshot. Truncating
                // the live file can make a host retain a stale pacing clock.
                var next = path + ".next";
                File.WriteAllText(next, elapsed.ToString(System.Globalization.CultureInfo.InvariantCulture));
                if(File.Exists(path))
                {
                    File.Replace(next, path, null);
                }
                else
                {
                    File.Move(next, path);
                }
            };
        }
        public void Reset() { }
        private readonly LimitTimer timer;
        private ulong elapsed;
    }
}
