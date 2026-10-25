using System;
using System.Threading;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.Timers;
static class Clock { public static ulong Us; public static ulong? MachineUs; }
class Machine : IMachine { public Antmicro.Renode.Time.Stamp ElapsedVirtualTime {get{return new Antmicro.Renode.Time.Stamp(Clock.MachineUs ?? Clock.Us);}} public object ClockSource {get{return this;}} public void RequestReset(){} }
class Program
{
    static void Require(bool value,string why){if(!value)throw new Exception(why);}
    static void Unlock(SedsStm32H5Rtc rtc){rtc.WriteDoubleWord(0x24,0xCA);rtc.WriteDoubleWord(0x24,0x53);}
    static void Configure(SedsStm32H5Rtc rtc,uint divider=31){Unlock(rtc);rtc.WriteDoubleWord(0x0C,0x80);Require((rtc.ReadDoubleWord(0x0C)&0x40)!=0,"INITF needs enabled source");rtc.WriteDoubleWord(0x0C,0x180);rtc.WriteDoubleWord(0x10,divider<<16);rtc.WriteDoubleWord(0x18,0x20);rtc.WriteDoubleWord(0x0C,0x100);rtc.WriteDoubleWord(0x24,0xFF);}
    static void Main()
    {
        var pwr=new SedsStm32Pwr("h5");var rcc=new SedsStm32Rcc("h5",pwr);var rtc=new SedsStm32H5Rtc(new Machine(),rcc,pwr);
        rcc.WriteDoubleWord(0xF0,1u<<27);Require((rcc.ReadDoubleWord(0xF0)&(1u<<27))==0,"LSIRDY is read-only, no unconditional ready");
        rcc.WriteDoubleWord(0xF0,0x04008200);Require((rcc.ReadDoubleWord(0xF0)&0x8300)==0,"backup domain write gate");Require((rcc.ReadDoubleWord(0xF0)&0x0C000000)==0x0C000000,"LSI ready follows enable");
        rcc.WriteDoubleWord(0xA8,1u<<21);Unlock(rtc);rtc.WriteDoubleWord(0x0C,0x180);Require((rtc.ReadDoubleWord(0x0C)&0x300)==0,"DBP protects RTC writes");
        pwr.WriteDoubleWord(0x24,1);rcc.WriteDoubleWord(0xF0,0x04008200);
        rtc.WriteDoubleWord(0x0C,0x180);Require((rtc.ReadDoubleWord(0x0C)&0x300)==0,"WPR must unlock");
        rtc.WriteDoubleWord(0x24,0x53);rtc.WriteDoubleWord(0x0C,0x180);Require((rtc.ReadDoubleWord(0x0C)&0x300)==0,"wrong key sequence cannot unlock");
        Configure(rtc);var start=rtc.ReadDoubleWord(8);Require(start==uint.MaxValue,"binary initial count");
        Clock.Us+=12345000;Require(unchecked(start-rtc.ReadDoubleWord(8))==12345,"LSI/32 gives nominal1kHz");
        var same=rtc.ReadDoubleWord(8);for(int i=0;i<100;i++)Require(rtc.ReadDoubleWord(8)==same,"reads do not advance virtual clock");Thread.Sleep(5);Require(rtc.ReadDoubleWord(8)==same,"wall time cannot advance RTC");
        Clock.MachineUs=Clock.Us+2000;Require(unchecked(same-rtc.ReadDoubleWord(8))==2,"monitor reads use machine time without effective CPU timestamp");Clock.Us+=2000;Clock.MachineUs=null;same=rtc.ReadDoubleWord(8);
        rtc.WriteDoubleWord(0x10,0);Require(rtc.ReadDoubleWord(0x10)==31u<<16,"locked PRER write rejected");
        rcc.WriteDoubleWord(0xA8,0);Clock.Us+=1000000;Require(rtc.ReadDoubleWord(8)==0,"APB gate");rcc.WriteDoubleWord(0xA8,1u<<21);Require(unchecked(same-rtc.ReadDoubleWord(8))==1000,"APB clock does not gate RTC oscillator");
        same=rtc.ReadDoubleWord(8);rtc.Reset();pwr.Reset();rcc.NoteWatchdogReset();rcc.Reset();Clock.Us+=2000000;pwr.WriteDoubleWord(0x24,1);rcc.WriteDoubleWord(0xA8,1u<<21);Require(unchecked(same-rtc.ReadDoubleWord(8))==2000,"counter/config survives IWDG reset");Require((rcc.ReadDoubleWord(0xF4)&(1u<<29))!=0,"IWDG reset cause retained");
        rcc.WriteDoubleWord(0xF0,rcc.ReadDoubleWord(0xF0)&~(1u<<15));same=rtc.ReadDoubleWord(8);Clock.Us+=5000000;Require(rtc.ReadDoubleWord(8)==same,"RTCEN gates counter");rcc.WriteDoubleWord(0xF0,rcc.ReadDoubleWord(0xF0)|(1u<<15));Clock.Us+=1000;Require(unchecked(same-rtc.ReadDoubleWord(8))==1,"RTC resumes without counting disabled interval");
        rcc.WriteDoubleWord(0xF0,rcc.ReadDoubleWord(0xF0)&~(1u<<26));Require((rcc.ReadDoubleWord(0xF0)&(1u<<27))==0,"disabled LSI is not ready");same=rtc.ReadDoubleWord(8);Clock.Us+=1000000;Require(rtc.ReadDoubleWord(8)==same,"LSI gate stops counter");rcc.WriteDoubleWord(0xF0,rcc.ReadDoubleWord(0xF0)|(1u<<26));
        Configure(rtc);start=rtc.ReadDoubleWord(8);Clock.Us+=(1UL<<32)*1000+5000;Require(unchecked(start-rtc.ReadDoubleWord(8))==5,"full32bit wrap");
        Configure(rtc,6);start=rtc.ReadDoubleWord(8);for(int i=0;i<3;i++){Clock.Us+=1000000;rtc.ReadDoubleWord(8);}Require(unchecked(start-rtc.ReadDoubleWord(8))==96000/7,"fractional divider does not drift");
        rcc.WriteDoubleWord(0xF0,rcc.ReadDoubleWord(0xF0)|(1u<<16));Require((rtc.ReadDoubleWord(0x0C)&0x300)==0,"backup reset clears configuration");Require((rcc.ReadDoubleWord(0xF0)&0x8300)==0,"backup reset clears RTC source/enable");
        rcc.WriteDoubleWord(0xF0,0x04008101);rcc.WriteDoubleWord(0xF0,0x04008201);Require((rcc.ReadDoubleWord(0xF0)&0x300)==0x100,"cannot steal configured RTC source");Unlock(rtc);rtc.WriteDoubleWord(0x0C,0x80);Require((rtc.ReadDoubleWord(0x0C)&0x40)==0,"unsupported clock source cannot fake INITF");
        foreach(var variant in new[]{"g4","u5","h7","h7rs"}){var other=new SedsStm32Rcc(variant);other.WriteDoubleWord(0x44,123);other.Reset();Require(other.ReadDoubleWord(0x44)==0,"other RCC reset unchanged");}
        Console.WriteLine("PASS H5 RTC clock/write gates, binary pacing/wrap, reset retention and negative source contracts");
    }
}
namespace Antmicro.Renode.Core { public interface IMachine {Antmicro.Renode.Time.Stamp ElapsedVirtualTime{get;}object ClockSource{get;}void RequestReset();} }
namespace Antmicro.Renode.Peripherals.Bus { public interface IDoubleWordPeripheral {uint ReadDoubleWord(long offset);void WriteDoubleWord(long offset,uint value);void Reset();}public interface IKnownSize {long Size{get;}} }
namespace Antmicro.Renode.Logging { public enum LogLevel { Warning,Error }public static class Logging {public static void Log(this object o,LogLevel level,string text,params object[] args){}} }
namespace Antmicro.Renode.Time { public enum Direction {Ascending,Descending}public sealed class TimeDomainsManager {public static TimeDomainsManager Instance=new TimeDomainsManager();public Stamp GetEffectiveVirtualTimeStamp(){return new Stamp();}}public sealed class Stamp {public Stamp():this(Clock.Us){}public Stamp(ulong us){TimeElapsed=new Elapsed(us);}public Elapsed TimeElapsed{get;private set;}}public sealed class Elapsed {public Elapsed(ulong us){TotalMicroseconds=us;}public ulong TotalMicroseconds{get;private set;}} }
namespace Antmicro.Renode.Peripherals.Timers {public sealed class LimitTimer {public LimitTimer(object clock,long freq,object owner,string name,ulong limit,Antmicro.Renode.Time.Direction direction,bool enabled,bool eventEnabled){Limit=limit;Enabled=enabled;}public event Action LimitReached;public ulong Limit{get;set;}public ulong Value{get;set;}public bool Enabled{get;set;}public void Fire(){if(LimitReached!=null)LimitReached();}} }

namespace Antmicro.Renode.Utilities { }
