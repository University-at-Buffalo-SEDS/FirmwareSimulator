using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;
class Program {
 static uint Check(byte[] b,int length) {uint h=2166136261;for(int i=0;i<length;i++)h=unchecked((h^b[i])*16777619);return h;}
 static byte[] Frame(char type,uint id) {int length=Length((byte)type);var b=Enumerable.Repeat((byte)0xEE,length).ToArray();b[0]=(byte)type;BitConverter.GetBytes(id).CopyTo(b,1);BitConverter.GetBytes(Check(b,length-4)).CopyTo(b,length-4);return b;}
 static int Length(byte b) {switch((char)b){case 'R':return 192;case 'G':return 48;case 'C':return 56;case 'A':return 64;case 'L':return 24;default:return 0;}}
 class Reader {
  List<byte> pending=new();public ulong Bad;public Action<char,uint> Complete;
  public void Read(byte b) {pending.Add(b);while(pending.Count>0){int length=Length(pending[0]);if(length==0){pending.RemoveAt(0);continue;}if(pending.Count<length)return;var frame=pending.Take(length).ToArray();if(Check(frame,length-4)!=BitConverter.ToUInt32(frame,length-4)){Bad++;pending.RemoveAt(0);continue;}pending.RemoveRange(0,length);Complete?.Invoke((char)frame[0],BitConverter.ToUInt32(frame,1));}}
 }
 static void Write(SedsRadioHostPort port,byte[] b){foreach(var value in b)port.WriteChar(value);}
 static void Advance(ulong until){while(Clock.NowUs<until){Clock.NowUs+=100;foreach(var timer in LimitTimer.All.ToArray())timer.Fire();}}
 static object Run(uint cost) {
  Clock.NowUs=0;LimitTimer.All.Clear();var m=new Machine();var gs=new SedsRadioHostPort(m);var rf=new SedsRadioHostPort(m);var rrls=new SedsRadioHostPort(m);
  // Effective service-rate envelope only. No claim that retries always occur,
  // or that this deterministic fair medium implements proprietary ASYNC MAC.
  var medium=new SedsRadioMedium(64000/cost,115200,8192,8192,3968,50000,160,2000);
  medium.AttachHost(0,gs);medium.AttachHost(1,rf);medium.AttachHost(2,rrls);
  var first=new Dictionary<uint,ulong>();var ackAt=new Dictionary<uint,ulong>();var writes=new Dictionary<uint,int>();var next=new Dictionary<uint,ulong>();
  var rfRead=new Reader();var gsRead=new Reader();gs.CharReceived+=gsRead.Read;rf.CharReceived+=rfRead.Read;
  rfRead.Complete=(type,id)=>{if(type=='C')Write(rf,Frame('A',id));};
  gsRead.Complete=(type,id)=>{if(type=='A'&&!ackAt.ContainsKey(id))ackAt[id]=Clock.NowUs;};
  ulong nextRf=0,nextGs=0,nextRrls=0;uint rfSeq=0,gsSeq=0;
  for(uint id=0;id<4;id++){first[id]=5000000UL+id*5000000UL;next[id]=first[id];writes[id]=0;}
  while(Clock.NowUs<35000000UL) {
   if(Clock.NowUs>=nextRf){Write(rf,Frame('R',rfSeq++));nextRf+=175663;} //1093 B/s
   if(Clock.NowUs>=nextGs){Write(gs,Frame('G',gsSeq++));nextGs+=170818;} //281 B/s
   if(Clock.NowUs>=nextRrls){Write(rrls,Frame('L',0));nextRrls+=1000000;}
   for(uint id=0;id<4;id++)if(!ackAt.ContainsKey(id)&&writes[id]<18&&Clock.NowUs>=next[id]){Write(gs,Frame('C',id));writes[id]++;next[id]+=250000;}
   Advance(Clock.NowUs+100);
  }
  var latency=Enumerable.Range(0,4).Select(i=>ackAt.TryGetValue((uint)i,out var stamp)?(double?)(stamp-first[(uint)i])/1000:null).ToArray();
  if(cost==1&&(latency.Any(v=>v==null||v>500)||medium.ReadCounter(0,4)!=0||medium.ReadCounter(1,4)!=0))throw new Exception("nominal transport control failed");
  return new {effective_air_bps=64000/cost,air_service_cost_factor=cost,first_ack_ms=latency,command_uart_copies=writes.OrderBy(v=>v.Key).Select(v=>v.Value).ToArray(),gs_peak=medium.ReadCounter(0,2),rf_peak=medium.ReadCounter(1,2),gs_refused_bytes=medium.ReadCounter(0,4),rf_refused_bytes=medium.ReadCounter(1,4),gs_invalid_records=gsRead.Bad,rf_invalid_records=rfRead.Bad};
 }
 static void Main(){foreach(uint cost in new uint[]{1,2,4,6,8,10})Console.WriteLine(JsonSerializer.Serialize(Run(cost)));}
}

