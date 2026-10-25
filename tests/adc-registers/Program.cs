using System;
using Antmicro.Renode.Peripherals.Sensors;
class Program {
 static void Assert(bool pass,string message){if(!pass)throw new Exception(message);}
 static void Main(){
  var adc=new SedsStm32Adc(bits:12,channels:1,samples:"1234");
  adc.WriteDoubleWord(0x0c,0x483);
  adc.WriteDoubleWord(8,1);Assert((adc.ReadDoubleWord(8)&1)==1,"enable did not latch");
  // HAL's RS-property mask intentionally writes zero to ADEN while starting.
  adc.WriteDoubleWord(8,4);Assert((adc.ReadDoubleWord(8)&5)==5,"HAL start cleared latched ADEN");
  adc.WriteDoubleWord(8,0);Assert((adc.ReadDoubleWord(8)&5)==5,"zero write cleared read-set command bits");
  adc.WriteDoubleWord(8,16);Assert((adc.ReadDoubleWord(8)&5)==1,"stop must disarm without disabling");
  adc.WriteDoubleWord(8,2);Assert((adc.ReadDoubleWord(8)&7)==0,"disable command did not clear ADEN");
  adc.WriteDoubleWord(0x0c,0);adc.WriteDoubleWord(8,1);adc.WriteDoubleWord(8,4);
  Assert((adc.ReadDoubleWord(8)&5)==1,"software conversion must self-clear start, retain enable");
  Assert(adc.ReadDoubleWord(0x40)==1234,"configured sample changed");
  Assert((adc.ReadDoubleWord(0)&4)==0,"DR read did not clear EOC");
  adc.Reset();Assert(adc.ReadDoubleWord(8)==0,"reset did not clear RS latch");
  adc.SetChannelSample(0,4095);Assert(adc.ReadDoubleWord(0x40)==4095,"dynamic maximum sample");
  bool invalid=false;try{adc.SetChannelSample(0,4096);}catch(ArgumentOutOfRangeException){invalid=true;}Assert(invalid&&adc.ReadDoubleWord(0x40)==4095,"range failure changed sample");
  invalid=false;try{adc.SetChannelSample(1,1);}catch(ArgumentOutOfRangeException){invalid=true;}Assert(invalid,"unknown channel accepted");
  var path=System.IO.Path.GetTempFileName();
  try{
   System.IO.File.WriteAllText(path,"1000,2000");var fixture=new SedsStm32Adc(bits:12,channels:2,samples:"1,2",stimulusPath:path);
   Assert(fixture.ReadDoubleWord(0x40)==1000,"first fixture sample");
   foreach(var bad in new[]{"partial","4096,1","1","1,2,3",new string('9',1025)}){
    System.IO.File.WriteAllText(path,bad);for(int i=0;i<10;i++)fixture.ReadDoubleWord(0x40);Assert(fixture.ReadDoubleWord(0x40)==1000,"invalid fixture changed last-good sample");
   }
   Assert(fixture.StimulusRejected>=5&&fixture.StimulusRejected<=6,"bounded rejected counter");
   System.IO.File.WriteAllText(path,"2048,4095");for(int i=0;i<10;i++)fixture.ReadDoubleWord(0x40);Assert(fixture.ReadDoubleWord(0x40)==2048,"recovered fixture");
   System.IO.File.Delete(path);for(int i=0;i<10;i++)fixture.ReadDoubleWord(0x40);Assert(fixture.ReadDoubleWord(0x40)==2048,"missing fixture changed last-good sample");
  }finally{System.IO.File.Delete(path);}
  Console.WriteLine("ADC_READ_SET_HAL_COMMANDS_PASS");
 }
}
namespace Antmicro.Renode.Core {public interface IKnownSize{long Size{get;}}}
namespace Antmicro.Renode.Peripherals.Bus {public interface IDoubleWordPeripheral{uint ReadDoubleWord(long offset);void WriteDoubleWord(long offset,uint value);}}
