"""Execute the actual bounded CAN wire scheduler, including arbitration."""
from pathlib import Path
import shutil,subprocess,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
@unittest.skipUnless(shutil.which('mcs') and shutil.which('mono'),'Mono compiler/runtime required')
class CanWireArbitrationTests(unittest.TestCase):
 def test_priority_controller_fifo_active_frame_and_bounded_refill(self):
  source=(ROOT/'renode/peripherals/SedsStm32H5Fdcan.cs').read_text();model=source[source.index('        private static uint WireArbitrationKey('):source.index('        private static readonly ConditionalWeakTable')]
  code=r'''
using System;using System.Collections.Generic;
using Antmicro.Renode.Core;using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;
namespace Antmicro.Renode.Core {public interface IMachine{object ClockSource{get;}}}
namespace Antmicro.Renode.Peripherals {public interface IPeripheral{void Reset();}}
namespace Antmicro.Renode.Time {public enum Direction{Ascending}}
namespace Antmicro.Renode.Peripherals.Timers {
 public class LimitTimer {
  public static LimitTimer Last;public event Action LimitReached;
  public bool Enabled;public ulong Limit,Value;
  public LimitTimer(object clock,long frequency,object owner,string name,ulong limit,Direction direction,bool enabled,bool eventEnabled){Last=this;Limit=limit;Enabled=enabled;}
  public void Fire(){if(!Enabled)throw new Exception("wire clock disabled");LimitReached();}
 }
}
class Machine:IMachine{public object ClockSource{get{return this;}}}
class Program {
'''+model+r'''
 static void Require(bool b,string reason){if(!b)throw new Exception(reason);}
 static uint Ext(uint id){return WireArbitrationKey((1u<<30)|id);}
 static void Main(){
  var a=new object();var b=new object();var done=new List<int>();
  var wire=new WireScheduler(new Machine());var clock=LimitTimer.Last;
  wire.Enqueue(new WireRequest(a,0,600,Ext(100),()=>done.Add(100)));
  wire.Enqueue(new WireRequest(a,1,600,Ext(1),()=>done.Add(1)));
  wire.Enqueue(new WireRequest(b,0,600,Ext(3),()=>done.Add(3)));
  clock.Fire();Require(done.Count==0,"selection must not complete a zero-time frame");
  clock.Fire();Require(done.Count==1&&done[0]==3,"FIFO tail cannot bypass its controller head");
  // An urgent arrival cannot preempt the frame already occupying the wire.
  wire.Enqueue(new WireRequest(b,0,600,Ext(0),()=>done.Add(0)));
  clock.Fire();Require(done[1]==100,"active frame retains wire ownership");
  clock.Fire();clock.Fire();Require(done.Count==4&&done[2]==0&&done[3]==1,"arbitrate only after each completed frame");
  Require(WireArbitrationKey(123u<<18)<WireArbitrationKey((1u<<30)|(123u<<18)),"standard beats same-base extended");
  Require(WireArbitrationKey((1u<<29)|(123u<<18))<WireArbitrationKey((1u<<30)|(123u<<18)),"standard remote beats same-base extended");
  foreach(uint metadata in new uint[]{1,129}) {
   wire=new WireScheduler(new Machine());clock=LimitTimer.Last;int bulk=0,acks=0,atFirstAck=-1;
   Action<uint> refill=null;
   refill=slot=>wire.Enqueue(new WireRequest(a,slot,600,Ext(metadata),()=>{bulk++;if(bulk<62)refill(slot);}));
   for(uint slot=0;slot<3;slot++)refill(slot);
   wire.Enqueue(new WireRequest(b,0,600,Ext(3),()=>{acks++;atFirstAck=bulk;}));
   for(int i=0;i<1000&&clock.Enabled;i++)clock.Fire();
   Require(bulk==64&&acks==1,"bounded refill retains every accepted frame");
   Require(metadata==1?atFirstAck==64:atFirstAck==0,"old schema priority starves ACK; corrected band releases it first");
  }
  Console.WriteLine("PASS: actual CAN scheduler, arbitration, controller FIFO, active frame, bounded refill and legacy starvation");
 }
}
'''
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'can.cs';path.write_text(code);exe=Path(tmp)/'can.exe';subprocess.run(['mcs','-out:'+str(exe),str(path)],check=True);subprocess.run(['mono',str(exe)],check=True)
