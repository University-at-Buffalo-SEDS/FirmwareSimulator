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
    // SEDS_UART_GATE is read once, when the model's type initializes, so it is
    // set before any UART exists. No gate file exists until CheckLinkGate.
    static readonly string GatePath = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"uart-gate-{Environment.ProcessId}.txt");
    static void Main()
    {
        Environment.SetEnvironmentVariable("SEDS_UART_GATE", $"other:usart1:/nonexistent;bench:usart2:{GatePath}");
        CheckByteRegisters();
        CheckLinkGate();
        CheckIdleAndOverrun();
        CheckG4FifoDepth();
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
    static void CheckG4FifoDepth()
    {
        var uart = new SedsStm32Uart(new Machine());
        uart.WriteDoubleWord(0, 13 | (1u << 29));
        uart.WriteDoubleWord(0x0c, 1476);
        // STM32G4 HAL defines RX_FIFO_DEPTH/TX_FIFO_DEPTH as eight words.
        for(byte value = 0; value < 8; value++)
        {
            uart.WriteChar(value); LimitTimer.For("uartRx").Fire();
            uart.WriteDoubleWord(0x28, value);
        }
        if((uart.ReadDoubleWord(0x1c) & 8) != 0) throw new Exception("eight-byte RX FIFO overran early");
        if((uart.ReadDoubleWord(0x1c) & 128) != 0) throw new Exception("TX FIFO exceeds G4 depth");
        uart.WriteChar(9); LimitTimer.For("uartRx").Fire();
        if((uart.ReadDoubleWord(0x1c) & 8) == 0) throw new Exception("ninth RX byte must overrun");
        for(byte value = 0; value < 8; value++)
            if(uart.ReadByte(0x24) != value) throw new Exception("RX FIFO overwrote accepted bytes");
        Console.WriteLine("PASS: G4 eight-byte FIFO headroom and bounded overrun");
    }
    // SEDS_UART_GATE cuts the selected UART (machine and peripheral names) in
    // both directions while its file reads "1", and reports it in a sidecar.
    static void CheckLinkGate()
    {
        // The stub timers are named, so the UART constructed last owns them.
        var uart = new SedsStm32Uart(new Machine("bench", "usart2"));
        uart.WriteDoubleWord(0, 13); uart.WriteDoubleWord(0x0c, 1476);
        var sent = new System.Collections.Generic.List<byte>();
        uart.CharReceived += value => sent.Add(value);
        void Transmit(SedsStm32Uart u, byte value) { u.WriteDoubleWord(0x28, value); LimitTimer.For("uartTx").Fire(); }
        bool Receive(SedsStm32Uart u, byte value)
        {
            u.WriteChar(value); LimitTimer.For("uartRx").Fire();
            return (u.ReadDoubleWord(0x1c) & 32) != 0 && u.ReadDoubleWord(0x24) == value;
        }
        Transmit(uart, 1);
        if(!Receive(uart, 2) || sent.Count != 1) throw new Exception("gate without its file cut the link");
        System.IO.File.WriteAllText(GatePath, "1");
        Thread.Sleep(80);  // the file is polled at most every 50 ms
        Transmit(uart, 3);
        if(sent.Count != 1) throw new Exception("cut link delivered a transmitted byte");
        if(Receive(uart, 4)) throw new Exception("cut link delivered a received byte");
        // Written on each transition (drop counts are refreshed every second).
        var status = System.IO.File.ReadAllText(GatePath + ".status");
        if(!status.StartsWith("cut=1 transitions=1 ")) throw new Exception($"gate status: {status}");
        System.IO.File.WriteAllText(GatePath, "0");
        Thread.Sleep(80);
        Transmit(uart, 6);
        if(sent.Count != 2 || sent[1] != 6 || !Receive(uart, 7)) throw new Exception("restored link lost bytes");
        System.IO.File.WriteAllText(GatePath, "1");
        var other = new SedsStm32Uart(new Machine("bench", "usart1"));
        other.WriteDoubleWord(0, 13); other.WriteDoubleWord(0x0c, 1476);
        var otherSent = 0;
        other.CharReceived += _ => otherSent++;
        Transmit(other, 5);
        if(otherSent != 1 || !Receive(other, 8)) throw new Exception("gate cut a UART it does not select");
        System.IO.File.Delete(GatePath); System.IO.File.Delete(GatePath + ".status");
        Console.WriteLine("PASS: SEDS_UART_GATE cuts only the selected UART, both ways, and reports it");
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
    public interface IMachine
    {
        object ClockSource { get; }
        TimeSource LocalTimeSource { get; }
        bool TryGetLocalName(object peripheral, out string name);
    }
    public class Machine : IMachine
    {
        public Machine(string name = "machine-0", string peripheral = "usart1") { Name = name; Peripheral = peripheral; }
        public readonly string Name, Peripheral;
        public object ClockSource => null;
        public TimeSource LocalTimeSource { get; } = new TimeSource();
        public bool TryGetLocalName(object peripheral, out string name) { name = Peripheral; return true; }
    }
    public class TimeSource { public TimeSpan ElapsedVirtualTime { get; set; } }
    public class Emulation
    {
        public bool TryGetMachineName(IMachine machine, out string name)
        {
            name = (machine as Machine)?.Name;
            return name != null;
        }
    }
    public class EmulationManager
    {
        public static EmulationManager Instance { get; } = new EmulationManager();
        public Emulation CurrentEmulation { get; } = new Emulation();
    }
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
