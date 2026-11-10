// Lock-order contract for the actual SedsStm32Uart and SedsStm32Dma models.
//
// The stubs reproduce the two Renode locks that matter, with Renode's
// semantics: every LimitTimer access takes the machine clock-source lock, and
// timer callbacks run while a thread advancing virtual time holds that lock;
// every SystemBus access to a peripheral holds that peripheral's access lock.
// An NVIC-like IRQ sink takes the clock lock when an IRQ rises (the NVIC
// enables SysTick), so a model that sets its IRQ under its own lock is caught.
//
// The scenario is the one that froze the linked bay: a CPU thread sends polled
// UART bytes (RFD modem AT session) through the bus while another thread
// advances the clock and services the same UART's circular RX DMA. Before the
// fix the two threads deadlocked within milliseconds. The contract requires
// both to finish, every TX byte to leave the wire once and in order, and every
// RX byte to land in RAM once and in order.
using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.DMA;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Time;

class Program
{
    const ulong Uart1 = 0x40013800, Dma1 = 0x40020000, Dmamux = 0x40020800, Ram = 0x20000000;
    const int RingBytes = 64;
    static int Bytes = 20000;
    static byte Pattern(int i) => (byte)(i * 37 + (i >> 7));
    static byte RxPattern(int i) => (byte)(i * 91 + 3 + (i >> 5));

    static int Main(string[] args)
    {
        if(args.Length > 0) Bytes = int.Parse(args[0]);
        var machine = new Machine();
        var bus = machine.Bus;
        var uart = new SedsStm32Uart(machine);
        var dma = new SedsStm32Dma(machine, 7);
        bus.Register(uart, Uart1, 0x400);
        bus.Register(dma, Dma1, 0x400);
        bus.Register(new MuxStub(), Dmamux, 0x100);
        uart.IRQ.Sink = machine.Nvic;

        // Firmware setup through the bus, as HAL does it.
        bus.WriteDoubleWord(Uart1 + 0x0c, 1476);            // BRR 115200 at 170 MHz
        bus.WriteDoubleWord(Uart1 + 0x08, 1u << 6);         // CR3.DMAR
        bus.WriteDoubleWord(Uart1 + 0x00, 13);              // UE | RE | TE
        bus.WriteDoubleWord(Dmamux + 0, 24);                // channel 0 <- USART1_RX
        bus.WriteDoubleWord(Dma1 + 12, RingBytes);          // CNDTR
        bus.WriteDoubleWord(Dma1 + 16, (uint)Uart1 + 0x24); // CPAR = RDR
        bus.WriteDoubleWord(Dma1 + 20, (uint)Ram);          // CMAR
        bus.WriteDoubleWord(Dma1 + 8, 1 | 32 | 128);        // EN | CIRC | MINC, periph -> memory

        var sent = new List<byte>();
        uart.CharReceived += value => sent.Add(value);
        var failure = (Exception)null;

        // CPU thread: HAL_UART_Transmit polling TXE, all through the bus.
        var cpu = new Thread(() => {
            try
            {
                for(var i = 0; i < Bytes; i++)
                {
                    while((bus.ReadDoubleWord(Uart1 + 0x1c) & 128) == 0) Thread.Yield();
                    bus.WriteDoubleWord(Uart1 + 0x28, Pattern(i));
                }
            }
            catch(Exception e) { failure = e; }
        }) { IsBackground = true, Name = "cpu" };

        // Time thread: the peer that advances this machine's clock. It feeds
        // RX bytes onto the wire and lets the DMA drain each into RAM.
        var time = new Thread(() => {
            try
            {
                for(var i = 0; i < Bytes; i++)
                {
                    uart.WriteChar(RxPattern(i));
                    for(var spins = 0; bus.RamWrites < i + 1; spins++)
                    {
                        machine.Clock.Advance(600);
                        if(spins > 100000) throw new Exception($"RX byte {i} never reached RAM");
                    }
                }
                while(sent.Count < Bytes) machine.Clock.Advance(2000);
            }
            catch(Exception e) { failure = e; }
        }) { IsBackground = true, Name = "time" };

        cpu.Start(); time.Start();
        var done = cpu.Join(TimeSpan.FromSeconds(60)) & time.Join(TimeSpan.FromSeconds(60));
        if(!done)
        {
            Console.WriteLine($"FAIL: deadlock: polled TX and RX DMA stopped after tx={sent.Count} rx={bus.RamWrites} of {Bytes}"
                + $" (cpu holds {machine.Describe(cpu)}, time holds {machine.Describe(time)})");
            return 1;
        }
        if(failure != null) { Console.WriteLine("FAIL: " + failure.Message); return 1; }
        for(var i = 0; i < Bytes; i++)
            if(sent[i] != Pattern(i)) { Console.WriteLine($"FAIL: TX byte {i}: {sent[i]} != {Pattern(i)}"); return 1; }
        if(sent.Count != Bytes) { Console.WriteLine($"FAIL: {sent.Count} TX bytes for {Bytes}"); return 1; }
        var log = bus.RamLog;
        if(log.Count != Bytes) { Console.WriteLine($"FAIL: {log.Count} RX DMA writes for {Bytes} bytes (lost or duplicated)"); return 1; }
        for(var i = 0; i < Bytes; i++)
        {
            var (address, value) = log[i];
            if(value != RxPattern(i) || address != Ram + (ulong)(i % RingBytes))
            {
                Console.WriteLine($"FAIL: RX byte {i} wrote {value:x2} at {address:x}, expected {RxPattern(i):x2} at {Ram + (ulong)(i % RingBytes):x}");
                return 1;
            }
        }
        Console.WriteLine($"PASS: {Bytes} polled TX bytes while circular RX DMA drained {Bytes} bytes on another clock thread: no deadlock, none lost or duplicated");
        return 0;
    }
}

class MuxStub : IDoubleWordPeripheral
{
    readonly uint[] regs = new uint[64];
    public uint ReadDoubleWord(long offset) => regs[offset / 4];
    public void WriteDoubleWord(long offset, uint value) => regs[offset / 4] = value;
}

namespace Antmicro.Renode.Core
{
    public interface IMachine
    {
        ClockSource ClockSource { get; }
        TimeSource LocalTimeSource { get; }
        SystemBus GetSystemBus(object context);
        bool TryGetLocalName(object peripheral, out string name);
    }
    public class Machine : IMachine
    {
        public readonly ClockSource Clock = new ClockSource();
        public readonly SystemBus Bus = new SystemBus();
        public readonly Nvic Nvic;
        public Machine() { Nvic = new Nvic(Clock); }
        public ClockSource ClockSource => Clock;
        public TimeSource LocalTimeSource { get; } = new TimeSource();
        public SystemBus GetSystemBus(object context) => Bus;
        public bool TryGetLocalName(object peripheral, out string name) { name = "usart1"; return true; }
        public string Describe(Thread t) => t.IsAlive ? "a lock (still blocked)" : "nothing (finished)";
    }
    public class TimeSource { public TimeSpan ElapsedVirtualTime => TimeSpan.Zero; }
    public class Emulation { public bool TryGetMachineName(IMachine m, out string name) { name = "bench"; return true; } }
    public class EmulationManager
    {
        public static EmulationManager Instance { get; } = new EmulationManager();
        public Emulation CurrentEmulation { get; } = new Emulation();
    }
    public interface IGPIOReceiver { void OnGPIO(int number, bool value); }
    public interface IGPIO { void Connect(IGPIOReceiver receiver, int number); void Set(bool state); }
    // Renode NVIC.OnGPIO: a rising IRQ enables SysTick, a LimitTimer call.
    public class Nvic : IGPIOReceiver
    {
        readonly ClockSource clock;
        public Nvic(ClockSource clock) { this.clock = clock; }
        public void OnGPIO(int number, bool value) { if(value) lock(clock.Sync) { } }
    }
    public class GPIO : IGPIO
    {
        public bool IsSet;
        public IGPIOReceiver Sink;
        IGPIOReceiver receiver; int number;
        public void Connect(IGPIOReceiver r, int n) { receiver = r; number = n; }
        public void Set(bool state) { IsSet = state; Sink?.OnGPIO(0, state); receiver?.OnGPIO(number, state); }
    }
    public interface INumberedGPIOOutput { IReadOnlyDictionary<int, IGPIO> Connections { get; } }
}
namespace Antmicro.Renode.Peripherals.Bus
{
    public interface IBusPeripheral { }
    public interface IDoubleWordPeripheral : IBusPeripheral { uint ReadDoubleWord(long offset); void WriteDoubleWord(long offset, uint value); }
    public interface IBytePeripheral : IBusPeripheral { byte ReadByte(long offset); void WriteByte(long offset, byte value); }
    public interface IKnownSize { long Size { get; } }
    public class Range { public ulong StartAddress, Size; }
    public class RegistrationPoint { public Range Range = new Range(); }
    public class BusRegistration
    {
        public IBusPeripheral Peripheral; public RegistrationPoint RegistrationPoint = new RegistrationPoint();
        public readonly object AccessLock = new object();
    }
    // Renode SystemBus: each peripheral access holds that peripheral's access
    // lock for the whole register handler. RAM has no such lock.
    public class SystemBus
    {
        readonly List<BusRegistration> registrations = new List<BusRegistration>();
        readonly object ramSync = new object();
        public readonly List<(ulong, byte)> RamLog = new List<(ulong, byte)>();
        public int RamWrites { get { lock(ramSync) return RamLog.Count; } }
        public void Register(IBusPeripheral p, ulong start, ulong size)
        {
            var r = new BusRegistration { Peripheral = p };
            r.RegistrationPoint.Range.StartAddress = start; r.RegistrationPoint.Range.Size = size;
            registrations.Add(r);
        }
        public BusRegistration WhatIsAt(ulong address, object context = null)
        {
            foreach(var r in registrations)
                if(address >= r.RegistrationPoint.Range.StartAddress && address < r.RegistrationPoint.Range.StartAddress + r.RegistrationPoint.Range.Size) return r;
            return null;
        }
        BusRegistration Find(ulong address, out long offset)
        {
            var r = WhatIsAt(address);
            offset = r == null ? 0 : (long)(address - r.RegistrationPoint.Range.StartAddress);
            return r;
        }
        public uint ReadDoubleWord(ulong address, object context = null)
        {
            var r = Find(address, out var offset);
            if(r == null) return 0;
            lock(r.AccessLock) return ((IDoubleWordPeripheral)r.Peripheral).ReadDoubleWord(offset);
        }
        public void WriteDoubleWord(ulong address, uint value, object context = null)
        {
            var r = Find(address, out var offset);
            if(r == null) return;
            lock(r.AccessLock) ((IDoubleWordPeripheral)r.Peripheral).WriteDoubleWord(offset, value);
        }
        public byte ReadByte(ulong address, object context = null)
        {
            var r = Find(address, out var offset);
            if(r == null) return 0;
            lock(r.AccessLock)
                return r.Peripheral is IBytePeripheral b ? b.ReadByte(offset) : (byte)((IDoubleWordPeripheral)r.Peripheral).ReadDoubleWord(offset);
        }
        public void WriteByte(ulong address, byte value, object context = null)
        {
            var r = Find(address, out var offset);
            if(r == null) { lock(ramSync) RamLog.Add((address, value)); return; }
            lock(r.AccessLock)
                if(r.Peripheral is IBytePeripheral b) b.WriteByte(offset, value);
                else ((IDoubleWordPeripheral)r.Peripheral).WriteDoubleWord(offset, value);
        }
        public void WriteWord(ulong address, ushort value, object context = null) { lock(ramSync) RamLog.Add((address, (byte)value)); }
    }
}
namespace Antmicro.Renode.Peripherals.UART
{
    public interface IUART { }
    public enum Bits { One }
    public enum Parity { None }
}
namespace Antmicro.Renode.Peripherals.Timers { }
namespace Antmicro.Renode.Peripherals.DMA
{
    public interface IDMA { void RequestTransfer(int channel); }
    public class STM32G0DMA
    {
        readonly Dictionary<int, Antmicro.Renode.Core.IGPIO> outputs = new Dictionary<int, Antmicro.Renode.Core.IGPIO>();
        public STM32G0DMA(Antmicro.Renode.Core.IMachine m, int n) { for(int i = 0; i < n; i++) outputs[i] = new Antmicro.Renode.Core.GPIO(); }
        public IReadOnlyDictionary<int, Antmicro.Renode.Core.IGPIO> Connections => outputs;
        public uint ReadDoubleWord(long o) => 0;
        public void WriteDoubleWord(long o, uint v) { }
        public void OnGPIO(int n, bool v) { }
        public void RequestTransfer(int n) { }
        public void Reset() { }
        public long Size => 0x400;
    }
}
namespace Antmicro.Renode.Time
{
    public enum Direction { Ascending }
    // Renode BaseClockSource: one lock per machine; Advance runs due timer
    // callbacks while holding it, and every timer access takes it.
    public class ClockSource
    {
        public readonly object Sync = new object();
        internal readonly List<LimitTimer> Timers = new List<LimitTimer>();
        public void Advance(ulong ticks)
        {
            lock(Sync)
            {
                foreach(var t in Timers.ToArray())
                {
                    if(!t.enabled) continue;
                    t.value += ticks;
                    if(t.value >= t.limit) { t.value = 0; t.Fire(); }
                }
            }
        }
    }
    public class LimitTimer
    {
        readonly ClockSource clock;
        internal bool enabled;
        internal ulong limit, value;
        public LimitTimer(object clock, long frequency, object owner, string name, ulong limit = 1,
            Direction direction = Direction.Ascending, bool enabled = false, bool eventEnabled = false)
        {
            this.clock = (ClockSource)clock; this.limit = limit; this.enabled = enabled;
            lock(this.clock.Sync) this.clock.Timers.Add(this);
        }
        public bool Enabled { get { lock(clock.Sync) return enabled; } set { lock(clock.Sync) enabled = value; } }
        public ulong Limit { get { lock(clock.Sync) return limit; } set { lock(clock.Sync) limit = value; } }
        public ulong Value { get { lock(clock.Sync) return value; } set { lock(clock.Sync) this.value = value; } }
        public event Action LimitReached;
        internal void Fire() => LimitReached?.Invoke();
        public void Reset() { lock(clock.Sync) { value = 0; enabled = false; } }
    }
}
