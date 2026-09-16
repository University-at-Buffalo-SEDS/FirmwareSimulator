// Exercise the actual UART model with concurrent CPU, timer and RX calls.
// These narrow Renode API stubs isolate FIFO synchronization; register/timing
// fidelity remains covered by the Docker Renode peripheral contracts.
using System;
using System.Threading;
using System.Threading.Tasks;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Time;

class Program
{
    const int Count = 500000;
    static byte Pattern(int i) => (byte)(i * 73 + (i >> 8));
    static void Main()
    {
        CheckConcurrentEnqueueDuringTimerDisable();
        var uart = new SedsStm32Uart(new Machine());
        uart.WriteDoubleWord(0, 9 | (1u << 29));
        uart.WriteDoubleWord(0x0c, 1476);
        var received = 0;
        uart.CharReceived += value => {
            if(value != Pattern(received))
                throw new Exception($"TX byte {received}: got {value}, expected {Pattern(received)}");
            received++;
        };
        var writer = Task.Run(() => {
            for(var i = 0; i < Count; i++)
            {
                while((uart.ReadDoubleWord(0x1c) & 128) == 0) Thread.Yield();
                uart.WriteDoubleWord(0x28, Pattern(i));
            }
        });
        var timer = Task.Run(() => {
            while(received < Count) LimitTimer.Last.Fire();
        });
        if(!Task.WaitAll(new[] { writer, timer }, TimeSpan.FromSeconds(30)))
            throw new Exception($"TX stalled after {received}/{Count} bytes");
        var rxWriter = Task.Run(() => {
            for(var i = 0; i < Count; i++) uart.WriteChar(Pattern(i));
        });
        var rxReader = Task.Run(() => {
            for(var i = 0; i < Count; i++)
            {
                while((uart.ReadDoubleWord(0x1c) & 32) == 0) Thread.Yield();
                var value = uart.ReadDoubleWord(0x24);
                if(value != Pattern(i)) throw new Exception($"RX byte {i}: got {value}, expected {Pattern(i)}");
            }
        });
        if(!Task.WaitAll(new[] { rxWriter, rxReader }, TimeSpan.FromSeconds(30)))
            throw new Exception("RX FIFO stalled");
        Console.WriteLine($"PASS: {Count} concurrent TX and {Count} RX bytes preserved exactly");
    }
    static void CheckConcurrentEnqueueDuringTimerDisable()
    {
        var uart = new SedsStm32Uart(new Machine());
        uart.WriteDoubleWord(0, 9);
        uart.WriteDoubleWord(0x0c, 1476);
        uart.WriteDoubleWord(0x28, 1);
        LimitTimer.Last.BeforeDisable = () => uart.WriteDoubleWord(0x28, 2);
        LimitTimer.Last.Fire();
        if(!LimitTimer.Last.Enabled) throw new Exception("UART enqueue lost its timer wake-up");
        LimitTimer.Last.Fire();
        if(uart.TransmittedBytes != 2) throw new Exception("UART failed to drain concurrent enqueue");
        Console.WriteLine("PASS: enqueue during timer disable cannot lose its wake-up");
    }
}
namespace Antmicro.Renode.Core
{
    public interface IMachine { object ClockSource { get; } }
    public class Machine : IMachine { public object ClockSource => null; }
    public class GPIO { public void Set(bool value) {} }
}
namespace Antmicro.Renode.Peripherals.Bus
{
    public interface IDoubleWordPeripheral {}
    public interface IKnownSize {}
}
namespace Antmicro.Renode.Peripherals.UART
{
    public interface IUART {}
    public enum Bits { One }
    public enum Parity { None }
}
namespace Antmicro.Renode.Peripherals.Timers { }
namespace Antmicro.Renode.Time
{
    public enum Direction { Ascending }
    public class LimitTimer
    {
        public static LimitTimer Last;
        public LimitTimer(object clock, uint frequency, object owner, string name,
            ulong limit, Direction direction, bool enabled, bool eventEnabled) { Last = this; }
        private volatile bool enabled;
        public Action BeforeDisable;
        public bool Enabled
        {
            get => enabled;
            set
            {
                if(!value)
                {
                    var hook = BeforeDisable;
                    BeforeDisable = null;
                    hook?.Invoke();
                }
                enabled = value;
            }
        }
        public ulong Limit;
        public ulong Value;
        public event Action LimitReached;
        public void Reset() { Enabled = false; }
        public void Fire() { if(Enabled) LimitReached?.Invoke(); }
    }
}
