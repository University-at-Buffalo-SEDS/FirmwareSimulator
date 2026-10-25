using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.DMA;
using Antmicro.Renode.Time;
class Program {
 static void Assert(bool v,string message){if(!v)throw new Exception(message);}
 static void Main(){
  var machine=new Machine();var bus=machine.Bus;
  bus.Registers[0x40020800]=37; // Actual G4 DMAMUX ADC3 request.
  bus.Registers[0x40000400]=1;bus.Registers[0x40000404]=0x20;
  bus.Registers[0x40000428]=1699;bus.Registers[0x4000042c]=199;
  bus.Registers[0x50000408]=5;bus.Registers[0x5000040c]=0x483;
  var dma=new SedsStm32Dma(machine,7);
  dma.WriteDoubleWord(12,16);dma.WriteDoubleWord(16,0x50000440);dma.WriteDoubleWord(20,0x20001000);
  dma.WriteDoubleWord(8,1|2|4|32|128|0x500);
  var timer=LimitTimer.For("uartDma0");
  Assert(bus.Samples==0 && dma.ReadDoubleWord(12)==16,"ADC DMA copied eagerly");
  Assert(timer.Limit==340000,"TIM3 PSC/ARR must produce500Hz at170MHz");
  for(int i=0;i<8;i++)timer.Fire();
  Assert(dma.ReadDoubleWord(12)==8 && bus.Samples==8,"half transfer count");
  for(uint i=0;i<8;i++)Assert(bus.Memory[0x20001000+i*2]==100+i,"16bit ADC sample order");
  Assert(((GPIO)dma.Connections[0]).IsSet && (dma.ReadDoubleWord(0)&4)!=0,"half IRQ");
  dma.WriteDoubleWord(4,4);Assert(!((GPIO)dma.Connections[0]).IsSet,"clear half IRQ");
  for(int i=0;i<8;i++)timer.Fire();
  Assert(dma.ReadDoubleWord(12)==16 && bus.Samples==16,"circular reload");
  Assert((dma.ReadDoubleWord(0)&2)!=0,"complete IRQ");dma.WriteDoubleWord(4,15);
  Assert(STM32G0DMA.LastClear==0,"owned IRQ clear reached eager fallback");
  dma.WriteDoubleWord(4,0xf0);Assert(STM32G0DMA.LastClear==0xf0,"unowned IRQ clear not forwarded");
  bus.Registers[0x40000400]=0;timer.Fire();Assert(bus.Samples==16,"stopped TIM3 transferred");
  bus.Registers[0x40000400]=1;bus.Registers[0x50000408]=1;timer.Fire();Assert(bus.Samples==16,"disarmed ADC transferred");
  bus.Registers[0x50000408]=5;bus.Registers[0x40020800]=36;timer.Fire();Assert(bus.Samples==16,"wrong DMA request transferred");
  bus.Registers[0x40020800]=37;bus.Registers[0x4000042c]=99;timer.Fire();
  Assert(timer.Limit==170000 && bus.Samples==17,"changed timer divider ignored");
  dma.Reset();timer.Fire();Assert(bus.Samples==17 && !((GPIO)dma.Connections[0]).IsSet,"reset left ADC active");
  Console.WriteLine("PASS: actual DMA model ADC trigger pacing,16bit samples,HT/TC,circular reload,stop/arm/mux/divider/reset");
 }
}
namespace Antmicro.Renode.Core {
 public interface IMachine{Bus GetSystemBus(object context);object ClockSource{get;}}
 public class Machine:IMachine{public Bus Bus=new();public Bus GetSystemBus(object o)=>Bus;public object ClockSource=>this;}
 public class Bus {
  public Dictionary<ulong,uint> Registers=new();public Dictionary<ulong,ushort> Memory=new();public uint Samples;
  public uint ReadDoubleWord(ulong address,object context){if(address==0x50000440)return 100+Samples++;return Registers.TryGetValue(address,out var v)?v:0;}
  public void WriteWord(ulong address,ushort v,object context){Memory[address]=v;}
  public byte ReadByte(ulong address,object context)=>(byte)ReadDoubleWord(address,context);
  public void WriteByte(ulong address,byte value,object context){Registers[address]=value;}
 }
 public interface IGPIOReceiver{void OnGPIO(int number,bool value);}
 public interface IGPIO{void Connect(IGPIOReceiver receiver,int number);void Set(bool state);}
 public class GPIO:IGPIO{public bool IsSet;private IGPIOReceiver receiver;private int number;public void Connect(IGPIOReceiver r,int n){receiver=r;number=n;}public void Set(bool state){IsSet=state;receiver?.OnGPIO(number,state);}}
 public interface INumberedGPIOOutput{IReadOnlyDictionary<int,IGPIO> Connections{get;}}
 public interface IKnownSize{long Size{get;}}
}
namespace Antmicro.Renode.Peripherals.Bus {public interface IDoubleWordPeripheral{uint ReadDoubleWord(long offset);void WriteDoubleWord(long offset,uint value);}}
namespace Antmicro.Renode.Peripherals.Timers {}
namespace Antmicro.Renode.Time {
 public enum Direction{Ascending}
 public class LimitTimer{
  static Dictionary<string,LimitTimer> timers=new();public static LimitTimer For(string n)=>timers[n];
  public LimitTimer(object clock,uint frequency,object owner,string name,ulong limit=1,Direction direction=Direction.Ascending,bool enabled=false,bool eventEnabled=true){Limit=limit;Enabled=enabled;timers[name]=this;}
  public ulong Limit,Value;public bool Enabled;public event Action LimitReached;
  public void Fire(){if(Enabled)LimitReached?.Invoke();}public void Reset(){Value=0;Enabled=false;}
 }
}
namespace Antmicro.Renode.Peripherals.DMA {
 public interface IDMA{void RequestTransfer(int channel);}
 // Fallback is intentionally inert: this contract fails if ADC accidentally
 // delegates its transfer to the eager UART/memory fallback implementation.
 public class STM32G0DMA{
  public STM32G0DMA(IMachine m,int n){for(int i=0;i<n;i++)outputs[i]=new GPIO();}
  Dictionary<int,IGPIO> outputs=new();public IReadOnlyDictionary<int,IGPIO> Connections=>outputs;
  public static uint LastClear;
  public uint ReadDoubleWord(long o)=>0;public void WriteDoubleWord(long o,uint v){if(o==4)LastClear=v;}
  public void OnGPIO(int n,bool v){}public void RequestTransfer(int n){}public void Reset(){}public long Size=>0x400;
 }
}
