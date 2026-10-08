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
        CheckByteRegisters();
        CheckIdleAndOverrun();
        CheckConcurrentEnqueueDuringTimerDisable();
        var uart = new SedsStm32Uart(new Machine());
        uart.WriteDoubleWord(0, 13 | (1u << 29));
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
            while(received < Count) LimitTimer.For("uartTx").Fire();
        });
        if(!Task.WaitAll(new[] { writer, timer }, TimeSpan.FromSeconds(30)))
            throw new Exception($"TX stalled after {received}/{Count} bytes");
        var rxWriter = Task.Run(() => {
            for(var i = 0; i < Count; i++) {
                while((uart.ReadDoubleWord(0x1c) & 32) != 0) Thread.Yield();
                uart.WriteChar(Pattern(i));
                LimitTimer.For("uartRx").Fire();
            }
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
    static void CheckByteRegisters()
    {
        var uart = new SedsStm32Uart(new Machine());
        uart.WriteDoubleWord(0, 13);
        uart.WriteDoubleWord(0x0c, 1476);
        byte sent = 0;
        uart.CharReceived += value => sent = value;
        uart.WriteByte(0x28, 0xa5);
        if(uart.TransmittedBytes != 0) throw new Exception("DMA bypassed UART wire timing");
        LimitTimer.For("uartTx").Fire();
        if(sent != 0xa5) throw new Exception("Byte DMA write lost its byte");
        uart.WriteChar(0x5a);
        uart.WriteChar(0x42);
        if((uart.ReadDoubleWord(0x1c) & 32) != 0) throw new Exception("RX bypassed wire timing");
        LimitTimer.For("uartRx").Fire();
        if(uart.ReadByte(0x24) != 0x5a) throw new Exception("Byte RDR consumed wrong byte");
        LimitTimer.For("uartRx").Fire();
        if(uart.ReadDoubleWord(0x24) != 0x42) throw new Exception("RDR consumed wrong byte");
        Console.WriteLine("PASS: byte DMA registers preserve data and wire timing");
    }
    static void CheckIdleAndOverrun()
    {
        var uart = new SedsStm32Uart(new Machine());
        uart.WriteDoubleWord(0, 0x15); // UE, RE, IDLEIE
        uart.WriteDoubleWord(0x0c, 17000);
        uart.WriteChar(1);
        LimitTimer.For("uartRx").Fire();
        if((uart.ReadDoubleWord(0x1c) & 16) != 0) throw new Exception("premature IDLE");
        LimitTimer.For("uartIdle").Fire();
        if(!uart.IRQ.IsSet || (uart.ReadDoubleWord(0x1c) & 16) == 0) throw new Exception("missing IDLE IRQ");
        uart.WriteDoubleWord(0x20, 16);
        if(uart.IRQ.IsSet) throw new Exception("IDLECF failed");
        uart.WriteChar(2); LimitTimer.For("uartRx").Fire();
        if((uart.ReadDoubleWord(0x1c) & 8) == 0) throw new Exception("missing overrun");
        uart.WriteDoubleWord(0x20, 8);
        if((uart.ReadDoubleWord(0x1c) & 8) != 0) throw new Exception("ORECF failed");
        uart.Reset();
        if((uart.ReadDoubleWord(0x1c) & 56) != 0) throw new Exception("reset retained RX state");
        Console.WriteLine("PASS: receive wire pacing, IDLE interrupt, overrun and reset");
    }
    static void CheckConcurrentEnqueueDuringTimerDisable()
    {
        var uart = new SedsStm32Uart(new Machine());
        uart.WriteDoubleWord(0, 13);
        uart.WriteDoubleWord(0x0c, 1476);
        uart.WriteDoubleWord(0x28, 1);
        LimitTimer.For("uartTx").BeforeDisable = () => uart.WriteDoubleWord(0x28, 2);
        LimitTimer.For("uartTx").Fire();
        if(!LimitTimer.For("uartTx").Enabled) throw new Exception("UART enqueue lost its timer wake-up");
        LimitTimer.For("uartTx").Fire();
        if(uart.TransmittedBytes != 2) throw new Exception("UART failed to drain concurrent enqueue");
        Console.WriteLine("PASS: enqueue during timer disable cannot lose its wake-up");
    }
}
namespace Antmicro.Renode.Core
{
    public interface IMachine { object ClockSource { get; } }
    public class Machine : IMachine { public object ClockSource => null; }
    public class GPIO { public bool IsSet; public void Set(bool value) { IsSet=value; } }
}
namespace Antmicro.Renode.Peripherals.Bus
{
    public interface IDoubleWordPeripheral {}
    public interface IBytePeripheral {}
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
        static System.Collections.Generic.Dictionary<string, LimitTimer> timers = new();
        public static LimitTimer For(string name) => timers[name];
        public LimitTimer(object clock, uint frequency, object owner, string name,
            ulong limit, Direction direction, bool enabled, bool eventEnabled) { timers[name] = this; }
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
