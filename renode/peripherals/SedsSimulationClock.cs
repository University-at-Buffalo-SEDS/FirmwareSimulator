using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Threading;
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
            // Opt-in fixed wall pace (FIRMWARE_SIM_WALL_PACE = wall seconds per
            // virtual second): hold the emulation so virtual time never runs
            // ahead of wall/pace. A host run under a matching time dilation then
            // shares one time base with the MCUs, so its wall-clock timers,
            // retries and freshness windows age at the boards' rate.
            var pace = Environment.GetEnvironmentVariable("FIRMWARE_SIM_WALL_PACE");
            double parsed;
            if(!string.IsNullOrEmpty(pace) && double.TryParse(pace, NumberStyles.Float, CultureInfo.InvariantCulture, out parsed) && parsed >= 1 && parsed <= 1000)
                wallPace = parsed;
            timer.LimitReached += () => {
                elapsed += 1;
                if(wallPace > 0)
                {
                    if(wall == null) wall = Stopwatch.StartNew();
                    var aheadMs = elapsed * wallPace - wall.Elapsed.TotalMilliseconds;
                    if(aheadMs >= 1) Thread.Sleep((int)aheadMs);
                    else if(-aheadMs > maxBehindMs) maxBehindMs = -aheadMs;
                    if(elapsed % 1000 == 0)
                        File.WriteAllText(path + ".pace", string.Format(CultureInfo.InvariantCulture, "{0} {1:F0} {2:F0}\n", elapsed, wall.Elapsed.TotalMilliseconds, maxBehindMs));
                }
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
        private readonly double wallPace;
        private Stopwatch wall;
        private double maxBehindMs;
    }
}
