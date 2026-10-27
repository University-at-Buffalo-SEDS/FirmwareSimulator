using System;
using System.Collections.Generic;
using System.Linq;
using System.IO;
using System.Text.Json;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

class Program
{
    static void Assert(bool value,string message) { if(!value) throw new Exception(message); }
    static void Advance(ulong until)
    {
        while(Clock.NowUs < until)
        {
            Clock.NowUs += 100;
            foreach(var timer in LimitTimer.All.ToArray()) timer.Fire();
        }
    }
    static void Fresh() { Clock.NowUs=0; LimitTimer.All.Clear(); }
    static SedsRadioMedium Medium(uint rate=64000,uint baud=115200,int tx=4096,int rx=4096,int burst=16,ulong idle=200,uint overhead=0,ulong turn=0)
        { return new SedsRadioMedium(rate,baud,tx,rx,burst,idle,overhead,turn); }
    static void PacketizationLatency(string scheduleFile)
    {
        var times=JsonSerializer.Deserialize<ulong[]>(File.ReadAllText(scheduleFile));
        Assert(times.Length>100 && times.Length<1000,"bounded production schedule required");
        foreach(var setting in new[]{(Burst:256,Idle:2000UL),(Burst:3968,Idle:50000UL)})
        {
            Fresh();var machine=new Machine();var gs=new SedsRadioHostPort(machine);
            var receiver=new SedsRadioHostPort(machine);
            var medium=Medium(tx:8192,rx:8192,burst:setting.Burst,idle:setting.Idle,overhead:160,turn:2000);
            medium.AttachHost(0,gs);medium.AttachHost(1,receiver);
            var received=new List<byte>();var deliveredAt=new List<ulong>();
            receiver.CharReceived += value => {received.Add(value);if(received.Count%42==0)deliveredAt.Add(Clock.NowUs);};
            var next=0;
            var end=times[times.Length-1]*1000+2000000;
            while(Clock.NowUs<end)
            {
                while(next<times.Length && times[next]*1000<=Clock.NowUs)
                {
                    // Raw transport probe, sized to the 42-byte writes in the
                    // Pi capture. No SDK/frame parser exists inside the modem.
                    for(var b=0;b<42;b++)gs.WriteChar((byte)((next+b)%251));
                    next++;
                }
                Advance(Clock.NowUs+100);
            }
            Assert(deliveredAt.Count==times.Length,"transport dropped a latency probe");
            Assert(received.Select((v,i)=>v==(byte)(((i/42)+(i%42))%251)).All(v=>v),"raw byte order changed");
            ulong worst=0;
            for(var i=0;i<times.Length;i++)worst=Math.Max(worst,deliveredAt[i]-times[i]*1000);
            Console.WriteLine($"RADIO_PACKETIZATION_MEASURED burst={setting.Burst} idle_us={setting.Idle} probes={times.Length} worst_us={worst} peak_tx={medium.ReadCounter(0,2)}");
            Assert(worst<500000,"small-command transport latency exceeded500ms: "+worst+"us");
            Assert(medium.ReadCounter(0,4)==0 && medium.ReadCounter(1,5)==0,"latency fix must not hide drops");
        }
        Console.WriteLine("RADIO_PRODUCTION_PACKETIZATION_LATENCY_PASS");
    }
    static void Main()
    {
        var schedule=Environment.GetEnvironmentVariable("RADIO_PACKETIZATION_SCHEDULE");
        if(schedule!=null){PacketizationLatency(schedule);return;}
        // An idle-delimited UART packet stays complete while another modem
        // owns the air. A later UART write cannot reopen that packet.
        foreach(var packetIdle in new ulong[]{10000,50000})
        {
            Fresh();
            var occupiedMachine=new Machine();
            var senderA=new SedsRadioHostPort(occupiedMachine);
            var senderB=new SedsRadioHostPort(occupiedMachine);
            var idleMedium=Medium(rate:1000,tx:32,rx:32,burst:32,idle:packetIdle);
            idleMedium.AttachHost(0,senderA);idleMedium.AttachHost(1,senderB);
            var receivedB=new List<byte>();senderA.CharReceived+=receivedB.Add;
            for(byte b=0;b<20;b++)senderA.WriteChar(b);
            Advance(packetIdle+10000); // A owns air for160ms after its packet seals.
            foreach(var b in new byte[]{101,102,103,104})senderB.WriteChar(b);
            Advance(packetIdle+90000); // B completed its idle gap while A still owns air.
            foreach(var b in new byte[]{105,106,107,108})senderB.WriteChar(b);
            Advance(packetIdle+200000);
            Assert(receivedB.SequenceEqual(new byte[]{101,102,103,104}),
                "busy air erased an already completed UART idle packet boundary");
            Assert(idleMedium.ReadCounter(1,0)==4 && idleMedium.ReadCounter(1,7)==4,
                "latched idle packet lost finite ownership of later bytes");
            Advance(packetIdle+250000);
            Assert(receivedB.SequenceEqual(new byte[]{101,102,103,104,105,106,107,108}),
                "latched idle packets changed byte order or dropped payload");
            Assert(idleMedium.ReadCounter(1,0)==0 && idleMedium.ReadCounter(1,4)==0,
                "latched idle packets changed FIFO admission accounting");
            Console.WriteLine("RADIO_BUSY_AIR_IDLE_BOUNDARY_PASS");
        }
        Fresh();
        var medium=Medium(burst:4);var machine=new Machine();var guest=new FakeUart(machine);
        var gs=new SedsRadioHostPort(machine);var rrls=new SedsRadioHostPort(machine);
        var gsRead=new List<byte>();var rrlsRead=new List<byte>();
        gs.CharReceived+=gsRead.Add;rrls.CharReceived+=rrlsRead.Add;
        medium.AttachUart(0,guest);medium.AttachHost(1,gs);medium.AttachHost(2,rrls);
        foreach(var b in new byte[]{1,2,3,4,5,6,7,8})gs.WriteChar(b);
        foreach(var b in new byte[]{21,22,23,24,25,26,27,28})rrls.WriteChar(b);
        Assert(gsRead.Count==0 && rrlsRead.Count==0 && guest.Received.Count==0,"instant delivery or local host echo");
        Assert(medium.ReadCounter(1,0)==8 && medium.ReadCounter(2,0)==8,"missing frozen-clock ownership");
        Advance(20000);
        Assert(gsRead.SequenceEqual(new byte[]{21,22,23,24,25,26,27,28}),"GS did not get only RRLS bytes");
        Assert(rrlsRead.SequenceEqual(new byte[]{1,2,3,4,5,6,7,8}),"RRLS did not get only GS bytes");
        Assert(guest.Received.Count==16,"guest missing host traffic or received echo");
        Assert(guest.Received.Take(8).SequenceEqual(new byte[]{1,2,3,4,21,22,23,24}),"contention not fair raw-byte bursts");
        Assert(medium.CompletedAirBits==128,"host inputs/outputs were retransmitted");
        Assert(machine.CrossDomainCalls==32,"delivery bypassed target time-domain event API");
        guest.Transmit(99);Advance(25000);
        Assert(gsRead.Last()==99 && rrlsRead.Last()==99 && guest.Received.Count==16,"guest TX was not broadcast without loopback");
        medium.SetLink(0,1,false);guest.Transmit(100);Advance(30000);
        Assert(gsRead.Last()==99 && rrlsRead.Last()==100 && medium.ReadCounter(1,6)==1,"directed outage did not isolate GS");
        medium.SetLink(0,1,true);guest.Transmit(101);Advance(35000);
        Assert(gsRead.Last()==101 && rrlsRead.Last()==101,"link recovery failed");
        Assert(medium.CompletedAirBits <= Clock.NowUs*64000/1000000,"aggregate airtime exceeded64kbps");
        gs.Reset();guest.Transmit(102);Advance(40000);Assert(gsRead.Last()==102,"MCU reset removed physical modem path");
        medium.DetachFrom(rrls);guest.Transmit(103);Advance(45000);
        Assert(gsRead.Last()==103 && rrlsRead.Last()==102,"detach left host event subscribed");
        Console.WriteLine("RADIO_HOST_GUEST_DIRECTION_FAIRNESS_LINKS_PASS");

        Fresh();machine=new Machine();guest=new FakeUart(machine);gs=new SedsRadioHostPort(machine);
        medium=Medium(tx:8,rx:8,burst:4);medium.AttachUart(0,guest);medium.AttachHost(1,gs);
        for(byte b=0;b<20;b++)gs.WriteChar(b);
        Assert(medium.ReadCounter(1,0)==8 && medium.ReadCounter(1,4)==12,"TX FIFO is unbounded or rejected ownership wrong");
        Advance(400); // First four-byte burst is now on air, not free queue credit.
        gs.WriteChar(50);
        Assert(medium.ReadCounter(1,0)==8 && medium.ReadCounter(1,4)==13,"in-flight TX escaped FIFO accounting");
        Advance(10000);
        Assert(guest.Received.SequenceEqual(Enumerable.Range(0,8).Select(x=>(byte)x)),"finite TX acceptance order changed");
        Assert(medium.ReadCounter(1,0)==0 && medium.ReadCounter(1,7)==8,"TX queue not retired on completion");
        Console.WriteLine("RADIO_TX_FINITE_OWNERSHIP_PASS");

        // Renode may enqueue cross-machine delivery for a later target time
        // domain. Detach/reattach must invalidate both that callback and an
        // already-captured UART event subscription.
        Fresh();machine=new Machine { DeferDelivery=true };guest=new FakeUart(machine);
        var receiver=new FakeUart(machine);medium=Medium(burst:1,idle:1);
        medium.AttachUart(0,guest);medium.AttachUart(1,receiver);
        var staleInput=receiver.CaptureTransmit();
        guest.Transmit(110);Advance(3000);
        Assert(machine.PendingCount==1 && medium.ReadCounter(1,8)==0,"delayed target event counted as actual delivery");
        medium.DetachFrom(receiver);
        var replacement=new FakeUart(machine);medium.AttachUart(1,replacement);
        staleInput(111);
        Assert(medium.ReadCounter(1,0)==0,"stale input callback entered replacement index");
        machine.FlushEvents();
        Assert(receiver.Received.Count==0 && replacement.Received.Count==0 && medium.ReadCounter(1,8)==0,"detached target event reached old or replacement UART");
        guest.Transmit(112);Advance(6000);
        medium.DetachFrom(replacement);medium.AttachUart(1,replacement);
        machine.FlushEvents();
        Assert(replacement.Received.Count==0 && medium.ReadCounter(1,8)==0,"reattaching the same UART revived old delivery");
        guest.Transmit(113);Advance(9000);machine.FlushEvents();
        Assert(replacement.Received.SequenceEqual(new byte[]{113}) && medium.ReadCounter(1,8)==1,"fresh attachment did not resume ordered delivery");
        medium.DetachFrom(replacement);medium.AttachUart(2,replacement);
        guest.Transmit(114);Advance(12000);machine.FlushEvents();
        Assert(replacement.Received.SequenceEqual(new byte[]{113,114}),"same UART at a new index failed");
        Console.WriteLine("RADIO_DEFERRED_DELIVERY_ATTACHMENT_IDENTITY_PASS");

        Fresh();machine=new Machine();guest=new FakeUart(machine);receiver=new FakeUart(machine);
        medium=Medium(burst:1,idle:1);medium.AttachUart(0,guest);medium.AttachUart(1,receiver);
        guest.Transmit(120);Advance(100); // Burst started, still on air.
        medium.DetachFrom(receiver);medium.AttachUart(1,receiver);
        Advance(1000);
        Assert(receiver.Received.Count==0,"attachment in the middle of an old burst received its bytes");
        guest.Transmit(121);Advance(1100); // Detach the sender while on air.
        medium.DetachFrom(guest);medium.AttachUart(0,guest);
        Advance(2000);
        Assert(receiver.Received.Count==0 && medium.ReadCounter(0,0)==0,"detached sender burst survived or contaminated new counters");
        guest.Transmit(122);Advance(2300); // Air completed; receiver UART byte not yet ready.
        medium.DetachFrom(receiver);medium.AttachUart(1,receiver);
        Advance(3000);
        Assert(receiver.Received.Count==0,"queued UART byte survived physical detach");
        guest.Transmit(123);Advance(5000);
        Assert(receiver.Received.SequenceEqual(new byte[]{123}),"physical reconnect failed after pending-air/RX cancellation");
        Console.WriteLine("RADIO_INFLIGHT_DETACH_REATTACH_PASS");

        Fresh();machine=new Machine();guest=new FakeUart(machine);gs=new SedsRadioHostPort(machine,1000);
        medium=Medium(rate:1000000,baud:1000,tx:128,rx:4,burst:4,idle:1);medium.AttachUart(0,guest);medium.AttachHost(1,gs);
        // A station with a slow UART must retain only its configured RX FIFO.
        // Different source UARTs collectively exceed that one output's rate.
        var other=new FakeUart(machine);medium.AttachUart(2,other);
        for(byte b=0;b<30;b++){guest.Transmit(b);other.Transmit((byte)(b+40));}
        Advance(310000);
        Assert(medium.ReadCounter(1,3)<=4 && medium.ReadCounter(1,5)>0,"RX FIFO overflow is not bounded/observable");
        Assert(medium.Snapshot().Contains("\"air_bits\"") && medium.Snapshot().Contains("\"index\":2"),"snapshot missing counters");
        Console.WriteLine("RADIO_RX_FINITE_OVERFLOW_PASS");
        var rejected=0;
        try { Medium(rate:0); } catch(Antmicro.Renode.Exceptions.RecoverableException){rejected++;}
        try { Medium(tx:4,burst:8); } catch(Antmicro.Renode.Exceptions.RecoverableException){rejected++;}
        try { medium.AttachHost(1,gs); } catch(Antmicro.Renode.Exceptions.RecoverableException){rejected++;}
        try { medium.AttachUart(3,gs); } catch(Antmicro.Renode.Exceptions.RecoverableException){rejected++;}
        try { medium.SetLink(1,1,true); } catch(Antmicro.Renode.Exceptions.RecoverableException){rejected++;}
        Assert(rejected==5,"invalid profile/attachment accepted");
        Console.WriteLine("RADIO_PROFILE_VALIDATION_PASS");
        Fresh();machine=new Machine();guest=new FakeUart(machine);other=new FakeUart(machine);
        medium=Medium(burst:64,overhead:32,turn:50);medium.AttachUart(0,guest);medium.AttachUart(1,other);
        for(var i=0;i<4096;i++){guest.Transmit((byte)i);other.Transmit((byte)(i+1));}
        Advance(1000000);
        Assert(medium.CompletedAirBits<=64000 && medium.CompletedAirBits>60000,"saturated aggregate airtime bound/throughput");
        Assert(medium.ReadCounter(0,7)>3000 && medium.ReadCounter(1,7)>3000,"a saturated sender starved");
        Assert(medium.ReadCounter(0,5)==0 && medium.ReadCounter(1,5)==0,"unexpected uncongested RX drop");
        Console.WriteLine("RADIO_SATURATED_AIRTIME_AND_FAIRNESS_PASS");
        var traceRoot=Path.Combine(Path.GetTempPath(),"seds-radio-trace-"+Guid.NewGuid().ToString("N"));
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RADIO_TRACE_DIR",traceRoot);
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS","2");
        try
        {
            Fresh();machine=new Machine();guest=new FakeUart(machine);gs=new SedsRadioHostPort(machine);
            medium=Medium(tx:8,rx:8,burst:4);medium.AttachUart(0,guest);medium.AttachHost(1,gs);
            gs.WriteChar(42);Advance(1000);gs.WriteChar(43);Advance(3000);gs.WriteChar(44);Advance(10000);
            var tx=File.ReadAllBytes(Path.Combine(traceRoot,"radio-station-1-tx.bin"));
            Assert(tx.Length==50 && System.Text.Encoding.ASCII.GetString(tx,0,8)=="SEDSRBT1","trace header/record size or window wrong");
            Assert(BitConverter.ToUInt32(tx,8)==1 && BitConverter.ToUInt32(tx,12)==9 && BitConverter.ToUInt32(tx,16)==1
                && BitConverter.ToUInt32(tx,20)==0 && BitConverter.ToUInt64(tx,24)==2000,"trace identity/bounds header wrong");
            Assert(BitConverter.ToUInt64(tx,32)==0 && tx[40]==42 && BitConverter.ToUInt64(tx,41)==1000 && tx[49]==43,"trace timestamps/raw bytes wrong");
            using(var status=JsonDocument.Parse(File.ReadAllText(Path.Combine(traceRoot,"radio-station-1-tx.bin.status.json"))))
                Assert(status.RootElement.GetProperty("finished").GetBoolean() && !status.RootElement.GetProperty("capped").GetBoolean()
                    && status.RootElement.GetProperty("records").GetUInt64()==2,"trace sidecar completion wrong");
            var rx=File.ReadAllBytes(Path.Combine(traceRoot,"radio-station-0-rx.bin"));
            Assert(rx.Length==50 && rx[40]==42 && rx[49]==43 && BitConverter.ToUInt32(rx,20)==1,"actual delivered UART RX not traced");
            Assert(guest.Received.SequenceEqual(new byte[]{42,43,44}),"trace window changed medium behavior");
            Directory.Delete(traceRoot,true);Directory.CreateDirectory(traceRoot);
            Fresh();machine=new Machine();guest=new FakeUart(machine);gs=new SedsRadioHostPort(machine);
            medium=Medium(tx:8,rx:8,burst:4);medium.AttachUart(0,guest);medium.AttachHost(1,gs);
            for(var i=0;i<470000;i++)gs.WriteChar((byte)i);
            Advance(10000);
            var path=Path.Combine(traceRoot,"radio-station-1-tx.bin");
            Assert(new FileInfo(path).Length<=4*1024*1024,"trace exceeded4MiB perstream cap");
            using(var status=JsonDocument.Parse(File.ReadAllText(path+".status.json")))
                Assert(status.RootElement.GetProperty("capped").GetBoolean() && status.RootElement.GetProperty("finished").GetBoolean(),"trace cap was silent");
            Assert(medium.ReadCounter(1,4)==469992 && medium.ReadCounter(1,7)==8,"capture altered finite modem admission");
            Directory.Delete(traceRoot,true);
            File.WriteAllText(traceRoot,"not a directory");
            var failed=false;try{Medium();}catch(IOException){failed=true;}
            Assert(failed,"capture I/O failure was hidden");
            Console.WriteLine("RADIO_TRACE_WINDOW_CAP_AND_IO_FAILURE_PASS");
        }
        finally
        {
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RADIO_TRACE_DIR",null);
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS",null);
            if(Directory.Exists(traceRoot))Directory.Delete(traceRoot,true);
            if(File.Exists(traceRoot))File.Delete(traceRoot);
        }
    }
}
class FakeUart : IUART
{
    public FakeUart(IMachine machine){Machine=machine;}
    public readonly IMachine Machine;
    public List<byte> Received=new();
    public void WriteChar(byte v){Received.Add(v);}
    public void Transmit(byte v){CharReceived?.Invoke(v);}
    public Action<byte> CaptureTransmit(){return CharReceived;}
    public event Action<byte> CharReceived;
    public uint BaudRate=>115200; public Bits StopBits=>Bits.One;public Parity ParityBit=>Parity.None;
    public void Reset(){}
}
static class Clock { public static ulong NowUs; }
namespace Antmicro.Renode.Exceptions { public class RecoverableException:Exception { public RecoverableException(string s):base(s){} } }
namespace Antmicro.Renode.Core
{
    public interface IExternal{}
    public interface IConnectable<T>{void AttachTo(T value);void DetachFrom(T value);}
    public interface IMachine { object ClockSource{get;}bool TryGetByName<T>(string name,out T peripheral) where T:class,Antmicro.Renode.Peripherals.IPeripheral;void HandleTimeDomainEvent<T>(Action<T> handler,T value,TimeStamp stamp); }
    public class Machine:IMachine
    {
        public object ClockSource=>this;public int CrossDomainCalls;
        public bool DeferDelivery;
        private readonly List<Action> pending=new();
        public int PendingCount=>pending.Count;
        public void FlushEvents(){var batch=pending.ToArray();pending.Clear();foreach(var callback in batch)callback();}
        public bool TryGetByName<T>(string name,out T peripheral) where T:class,Antmicro.Renode.Peripherals.IPeripheral {peripheral=null;return false;}
        public void HandleTimeDomainEvent<T>(Action<T> handler,T value,TimeStamp stamp){CrossDomainCalls++;if(DeferDelivery)pending.Add(()=>handler(value));else handler(value);}
    }
    public class Emulation { public ExternalManager ExternalsManager=new();public bool TryGetMachineByName(string name,out IMachine machine){machine=null;return false;} }
    public class EmulationManager {public static readonly EmulationManager Instance=new();public Emulation CurrentEmulation=new();}
    public class ExternalManager { public void AddExternal(IExternal value,string name){} }
    public static class Extensions
    {
        public static IMachine GetMachine(this IUART uart) => uart is FakeUart f ? f.Machine : ((SedsRadioHostPort)uart).Machine;
    }
}
namespace Antmicro.Renode.Peripherals { public interface IPeripheral{void Reset();} }
namespace Antmicro.Renode.Peripherals.UART
{
    public interface IUART:Antmicro.Renode.Peripherals.IPeripheral {event Action<byte> CharReceived;void WriteChar(byte v);uint BaudRate{get;}Bits StopBits{get;}Parity ParityBit{get;} }
    public enum Bits{One}public enum Parity{None}
}
namespace Antmicro.Renode.Time
{
    public enum Direction{Ascending}
    public struct TimeInterval {public double TotalMicroseconds;}
    public struct TimeStamp {public TimeInterval TimeElapsed;}
    public class TimeDomainsManager
    {
        public static readonly TimeDomainsManager Instance=new();
        public TimeStamp GetEffectiveVirtualTimeStamp()=>VirtualTimeStamp;
        public TimeStamp VirtualTimeStamp=>new TimeStamp{TimeElapsed=new TimeInterval{TotalMicroseconds=Clock.NowUs}};
    }
}
namespace Antmicro.Renode.Peripherals.Timers
{
    public class LimitTimer
    {
        public static readonly List<LimitTimer> All=new();
        public LimitTimer(object clock,uint frequency,Antmicro.Renode.Peripherals.IPeripheral owner,string name,ulong limit,Direction direction,bool enabled,bool eventEnabled){All.Add(this);}
        public event Action LimitReached;public void Fire(){LimitReached?.Invoke();}
    }
}

namespace Antmicro.Renode.Peripherals.Bus {public interface IDoubleWordPeripheral:Antmicro.Renode.Peripherals.IPeripheral{uint ReadDoubleWord(long offset);void WriteDoubleWord(long offset,uint value);}public interface IKnownSize{long Size{get;}}}

namespace Antmicro.Renode.Utilities { }
