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
            if(Environment.GetEnvironmentVariable("RADIO_INTERACTIVE_PACKETIZATION") == "1")
            {
                var interactiveLatency=deliveredAt[3]-times[3]*1000;
                Console.WriteLine($"RADIO_INTERACTIVE_PACKETIZATION idle_us={setting.Idle} sent_us={times[3]*1000} received_us={deliveredAt[3]} latency_us={interactiveLatency}");
                Assert(interactiveLatency<=100000,"critical per-hop packetization exceeded100ms: "+interactiveLatency+"us");
            }
        }
        Console.WriteLine("RADIO_PRODUCTION_PACKETIZATION_LATENCY_PASS");
    }
    // Opt-in RFD ASYNC command mode for one station (FIRMWARE_SIM_RFD_MODEM).
    static void ModemCommandMode()
    {
        Fresh();
        var machine=new Machine();var host=new SedsRadioHostPort(machine);var peer=new SedsRadioHostPort(machine);
        var medium=Medium(burst:256,idle:2000);
        medium.ConfigureModem(0,"6:SER_BRK_DETMS=20,19:RTSCTS=1");
        medium.AttachHost(0,host);medium.AttachHost(1,peer);
        var fromModem=new List<byte>();host.CharReceived+=fromModem.Add;
        var air=new List<byte>();peer.CharReceived+=air.Add;
        Func<string> text=()=>System.Text.Encoding.ASCII.GetString(fromModem.ToArray());
        Action<string> send=line=>{foreach(var c in line)host.WriteChar((byte)c);};
        Advance(1200000);send("+++");Advance(Clock.NowUs+1500000);
        Assert(text().Contains("OK\r\n"),"no OK one second after a guarded +++");
        Assert(air.Count==0,"+++ reached the air");
        foreach(var b in new byte[]{1,2,3})peer.WriteChar(b);
        Advance(Clock.NowUs+200000);
        Assert(!fromModem.Contains(1)&&medium.Snapshot().Contains("\"dropped_air\":3"),"command mode passed air data");
        fromModem.Clear();send("ATI5\r");Advance(Clock.NowUs+300000);
        Assert(text().StartsWith("ATI5\r")&&text().Contains("S6:SER_BRK_DETMS=20\r\n")&&text().Contains("S18:LBT_RSSI=0\r\n")
            &&text().Contains("S19:RTSCTS=1\r\n")&&!text().Contains("OK"),"ATI5 must echo and list the 4.03 layout without OK");
        fromModem.Clear();send("ATS19?\r");Advance(Clock.NowUs+100000);
        Assert(text()=="ATS19?\r1\r\n","ATSn? answer");
        fromModem.Clear();send("ATS19=0\r");Advance(Clock.NowUs+100000);
        Assert(text().EndsWith("OK\r\n"),"ATSn=X not acknowledged");
        fromModem.Clear();send("ATS99=1\r");Advance(Clock.NowUs+100000);
        Assert(text().EndsWith("ERROR\r\n"),"unknown register accepted");
        send("AT&W\r");Advance(Clock.NowUs+100000);send("ATZ\r");Advance(Clock.NowUs+2000000);
        var snapshot=medium.Snapshot();
        Assert(snapshot.Contains("\"S19:RTSCTS\":0")&&snapshot.Contains("\"S6:SER_BRK_DETMS\":20")&&snapshot.Contains("\"saves\":1")
            &&snapshot.Contains("\"reboots\":1")&&snapshot.Contains("\"state\":\"data\"")&&snapshot.Contains("\"writes\":\"19=0\""),
            "AT&W/ATZ did not persist and reboot: "+snapshot);
        air.Clear();send("a+b");Advance(Clock.NowUs+200000);
        Assert(System.Text.Encoding.ASCII.GetString(air.ToArray())=="a+b","data mode after ATZ must pass bytes, including an unguarded +");
        // Without ConfigureModem a station stays transparent.
        Fresh();var m2=new Machine();var a=new SedsRadioHostPort(m2);var b2=new SedsRadioHostPort(m2);
        var plain=Medium(burst:256,idle:2000);plain.AttachHost(0,a);plain.AttachHost(1,b2);
        var got=new List<byte>();b2.CharReceived+=got.Add;
        Advance(1200000);foreach(var c in "+++")a.WriteChar((byte)c);Advance(Clock.NowUs+1500000);
        Assert(System.Text.Encoding.ASCII.GetString(got.ToArray())=="+++"&&!plain.Snapshot().Contains("modem"),"transparent station changed");
        Console.WriteLine("RADIO_RFD_MODEM_COMMAND_MODE_PASS");
    }
    // RFD ASYNC broadcast: 1 + GLOBAL_RETRIES copies of each packet hold the air;
    // the receiver gets one copy. DUTY_CYCLE holds the sender off afterwards.
    static void BroadcastCopiesAndDutyCycle()
    {
        foreach(var (retries, duty) in new[]{(0u,100u),(10u,100u),(10u,50u)})
        {
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_GLOBAL_RETRIES",retries.ToString());
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_DUTY_CYCLE",duty.ToString());
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY","blind");
            Fresh();var m=new Machine();var tx=new SedsRadioHostPort(m);var rx=new SedsRadioHostPort(m);
            // 64 kbit/s, 50 ms SER_BRK idle cutoff, 160 bits of framing per copy.
            var medium=Medium(rate:64000,tx:4096,rx:4096,burst:3968,idle:50000,overhead:160);
            medium.AttachHost(0,tx);medium.AttachHost(1,rx);var got=new List<byte>();rx.CharReceived+=got.Add;
            // One 40-byte frame every 20 ms of host time for 2 s: idle gaps under 50 ms
            // never close a packet, so only MAX_DATA or a pause seals one.
            ulong t=0;var sent=0;
            for(var k=0;k<100;k++){foreach(var b in Enumerable.Range(0,40))tx.WriteChar((byte)b);sent+=40;t+=20000;Advance(t);}
            Advance(t+30000000);
            var copies=1+retries;var air=medium.CompletedAirBits;var packets=medium.ReadCounter(0,9);
            Assert(got.Count==sent && medium.ReadCounter(0,4)==0,"payload lost or refused: "+got.Count+"/"+sent);
            Assert(air==((ulong)sent*8+160UL*packets)*copies,"air bits do not count every broadcast copy");
            Console.WriteLine("RADIO_RFD_BROADCAST_COPIES retries="+retries+" duty="+duty+" packets="+packets+" air_bits="+air);
            if(retries==10&&duty==100)
            {
                // A paced host frame every 155 ms (100 ms burst + 55 ms gap): with
                // 11 copies each 40-byte packet holds about 8 ms * 11 = 88 ms of air.
                Fresh();m=new Machine();tx=new SedsRadioHostPort(m);rx=new SedsRadioHostPort(m);
                medium=Medium(rate:64000,tx:4096,rx:4096,burst:3968,idle:50000,overhead:160);medium.AttachHost(0,tx);medium.AttachHost(1,rx);
                t=0;for(var k=0;k<64;k++){foreach(var b in Enumerable.Range(0,40))tx.WriteChar((byte)b);t+=155000;Advance(t);}
                Advance(t+2000000);
                Assert(medium.ReadCounter(0,9)==64,"one SER_BRK-gapped host frame per packet: "+medium.ReadCounter(0,9));
            }
            if(duty==50)
            {
                Assert(Clock.NowUs>=air*1000000/64000*2-1000000,"duty cycle did not hold the sender off the air");
            }
        }
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_GLOBAL_RETRIES",null);
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_DUTY_CYCLE",null);
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY",null);
        // Default reading (on_loss): a clean link spends one copy per packet.
        {
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_GLOBAL_RETRIES","10");
            Fresh();var m=new Machine();var tx=new SedsRadioHostPort(m);var rx=new SedsRadioHostPort(m);
            var medium=Medium(rate:64000,tx:4096,rx:4096,burst:3968,idle:50000,overhead:160);
            medium.AttachHost(0,tx);medium.AttachHost(1,rx);
            foreach(var b in Enumerable.Range(0,40))tx.WriteChar((byte)b);Advance(2000000);
            Assert(medium.ReadCounter(0,9)==1&&medium.ReadCounter(0,11)==1&&medium.CompletedAirBits==480,"default on_loss must not resend on a clean link");
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_GLOBAL_RETRIES",null);
        }
        Console.WriteLine("RADIO_RFD_BROADCAST_COPIES_DUTY_PASS");
    }
    // Per-station RFD900x ASYNC air timing (docs/rfd-radio-model-audit.md).
    // Pinned arithmetic, all at 64 kbit/s air (S2=64), 115200 8N1 UART (87 us
    // per character), 160 bits of air framing per copy (assumption A3):
    //   40 B frame -> 40*87 = 3480 us UART in, S6 ms idle, copies*(320+160)
    //   bits / 64000 on air, 40*87 = 3480 us UART out at the receiver.
    // S6 is local to each modem and S5 is applied by the sending modem
    // (manual V1.1 Table 3-2 p. 6-7), so each direction has its own timing.
    static ulong FirstFrameLatency(SedsRadioMedium medium,SedsRadioHostPort from,List<(ulong At,byte Value)> to,ulong start,int count)
    {
        to.Clear();Advance(start);
        foreach(var b in Enumerable.Range(0,count))from.WriteChar((byte)b);
        Advance(start+1000000);
        Assert(to.Count==count,"frame not delivered: "+to.Count);
        return to[count-1].At-start;
    }
    static void PerStationModemAirTiming()
    {
        foreach(var mac in new[]{"arbiter","async"})
        foreach(var mode in new[]{"blind","on_loss"})
        {
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_MAC",mac);
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY",mode);
            Fresh();var m=new Machine();var gs=new SedsRadioHostPort(m);var rf=new SedsRadioHostPort(m);
            // The scenario profile (2 ms idle, 256 B) must not apply to modelled modems.
            var medium=Medium(rate:64000,tx:4096,rx:4096,burst:256,idle:2000,overhead:160,turn:2000);
            // GS at the drain profile, RF still at the 10-28 field readback.
            medium.ConfigureModem(0,"5:GLOBAL_RETRIES=1,6:SER_BRK_DETMS=10");
            medium.ConfigureModem(1,"5:GLOBAL_RETRIES=10,6:SER_BRK_DETMS=50");
            medium.AttachHost(0,gs);medium.AttachHost(1,rf);
            var atRf=new List<(ulong,byte)>();rf.CharReceived+=v=>atRf.Add((Clock.NowUs,v));
            var atGs=new List<(ulong,byte)>();gs.CharReceived+=v=>atGs.Add((Clock.NowUs,v));
            var up=FirstFrameLatency(medium,gs,atRf,2000000,40);
            var down=FirstFrameLatency(medium,rf,atGs,4000000,40);
            ulong gsCopies=mode=="blind"?2UL:1UL, rfCopies=mode=="blind"?11UL:1UL;
            // The arbiter delivers after the last copy; the async MAC delivers at
            // the first intact copy (a receiver cannot know more copies follow),
            // while the sender still holds the air for all of them.
            ulong Expect(ulong idle,ulong copies)=>3480+idle+((mac=="async"?1UL:copies)*480*1000000+63999)/64000+3480;
            Console.WriteLine($"RADIO_RFD_PER_STATION_TIMING mac={mac} mode={mode} gs_to_rf_us={up} expect={Expect(10000,gsCopies)} rf_to_gs_us={down} expect={Expect(50000,rfCopies)}");
            // 100 us medium quantum on each of the three stages.
            Assert(up>=Expect(10000,gsCopies)&&up<=Expect(10000,gsCopies)+300,"GS->RF timing does not follow the GS modem's S5/S6");
            Assert(down>=Expect(50000,rfCopies)&&down<=Expect(50000,rfCopies)+300,"RF->GS timing does not follow the RF modem's S5/S6");
            Assert(medium.ReadCounter(0,11)==gsCopies&&medium.ReadCounter(1,11)==rfCopies,"copies counter");
            var snap=medium.Snapshot();
            Assert(snap.Contains("\"global_retries\":1,\"packet_idle_us\":10000,\"max_data\":3968")
                &&snap.Contains("\"global_retries\":10,\"packet_idle_us\":50000"),"per-station parameters not reported: "+snap);
        }
        // A packet closes at the station's own MAX_DATA (S3), not the profile's.
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY",null);
        {
            Fresh();var m=new Machine();var a=new SedsRadioHostPort(m);var b=new SedsRadioHostPort(m);
            var medium=Medium(tx:4096,rx:4096,burst:256,idle:2000,overhead:160);
            medium.ConfigureModem(0,"3:MAX_DATA=100,5:GLOBAL_RETRIES=0,6:SER_BRK_DETMS=500");
            medium.AttachHost(0,a);medium.AttachHost(1,b);var got=new List<byte>();b.CharReceived+=got.Add;
            foreach(var x in Enumerable.Range(0,250))a.WriteChar((byte)x);
            Advance(3000000);
            Assert(got.Count==250&&medium.ReadCounter(0,9)==3,"MAX_DATA=100 must split 250 B into 3 packets: "+medium.ReadCounter(0,9));
        }
        // A cut receiver: blind sends 1+S5 copies regardless; on_loss resends
        // while any attached station lacks the packet, so a cut peer costs
        // every resend. The third station still gets the packet once.
        foreach(var mode in new[]{"blind","on_loss"})
        {
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY",mode);
            Fresh();var m=new Machine();var gs=new SedsRadioHostPort(m);var rf=new SedsRadioHostPort(m);var rrls=new SedsRadioHostPort(m);
            var medium=Medium(tx:4096,rx:4096,burst:256,idle:2000,overhead:160);
            medium.ConfigureModem(0,"5:GLOBAL_RETRIES=1,6:SER_BRK_DETMS=10");
            medium.AttachHost(0,gs);medium.AttachHost(1,rf);medium.AttachHost(2,rrls);
            var got=new List<byte>();rrls.CharReceived+=got.Add;
            medium.SetLink(0,1,false);
            foreach(var x in Enumerable.Range(0,40))gs.WriteChar((byte)x);
            Advance(1000000);
            Assert(got.Count==40&&medium.ReadCounter(1,6)==40&&medium.ReadCounter(0,11)==2,mode+": cut peer handling copies="+medium.ReadCounter(0,11));
        }
        // Seeded air loss: delivered + lost = sent; blind always spends 1+S5,
        // on_loss spends between 1 and 1+S5 and loses less than one blind try.
        foreach(var mode in new[]{"blind","on_loss"})
        {
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY",mode);
            Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_AIR_LOSS_PPM","300000");
            Fresh();var m=new Machine();var a=new SedsRadioHostPort(m);var b=new SedsRadioHostPort(m);
            var medium=Medium(tx:65536,rx:65536,burst:256,idle:2000,overhead:160);
            medium.ConfigureModem(0,"5:GLOBAL_RETRIES=3,6:SER_BRK_DETMS=5");
            medium.AttachHost(0,a);medium.AttachHost(1,b);var got=new List<byte>();b.CharReceived+=got.Add;
            ulong t=0;for(var k=0;k<400;k++){foreach(var x in Enumerable.Range(0,10))a.WriteChar((byte)x);t+=80000;Advance(t);}
            Advance(t+2000000);
            var packets=medium.ReadCounter(0,9);var copies=medium.ReadCounter(0,11);var lost=medium.ReadCounter(1,10);
            Console.WriteLine($"RADIO_RFD_AIR_LOSS mode={mode} packets={packets} copies={copies} delivered={got.Count} lost={lost}");
            Assert(packets==400&&(ulong)got.Count+lost==4000,"delivered+lost must equal sent");
            if(mode=="blind")Assert(copies==1600,"blind must send 1+S5 copies");
            else Assert(copies>400&&copies<1600,"on_loss must resend only on loss");
            // P(all 4 copies lost) = 0.3^4 = 0.8 %: 3.2 of 400 packets expected.
            Assert(lost<=200,"retries must cover most single-copy losses");
        }
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY",null);
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_AIR_LOSS_PPM",null);
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_MAC",null);
        Console.WriteLine("RADIO_RFD_PER_STATION_AIR_TIMING_PASS");
    }
    // Uncoordinated ASYNC access (datasheet s5.3): overlapping transmissions
    // are lost at receivers that hear both and at the transmitting stations
    // themselves (half duplex). LBT_RSSI != 0 makes a station wait for a clear
    // channel for 5-10 ms (SiK tdm.c) instead.
    static void AsyncCollisionsHalfDuplexAndLbt()
    {
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_MAC","async");
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY","blind");
        (SedsRadioMedium M,SedsRadioHostPort Gs,SedsRadioHostPort Rf,List<byte> AtRrls,List<byte> AtGs,List<byte> AtRf) Net(string regs)
        {
            Fresh();var m=new Machine();var gs=new SedsRadioHostPort(m);var rf=new SedsRadioHostPort(m);var rrls=new SedsRadioHostPort(m);
            var medium=Medium(tx:8192,rx:8192,burst:256,idle:2000,overhead:160,turn:2000);
            for(var k=0;k<3;k++)medium.ConfigureModem(k,regs);
            medium.AttachHost(0,rf);medium.AttachHost(1,gs);medium.AttachHost(2,rrls);
            var a=new List<byte>();rrls.CharReceived+=a.Add;var g=new List<byte>();gs.CharReceived+=g.Add;var r=new List<byte>();rf.CharReceived+=r.Add;
            return (medium,gs,rf,a,g,r);
        }
        // 1. Same instant, LBT off, S5=0: both packets collide at RRLS and
        // neither transmitter hears the other.
        {
            var n=Net("5:GLOBAL_RETRIES=0,6:SER_BRK_DETMS=5,18:LBT_RSSI=0");
            Advance(1000000);
            foreach(var x in Enumerable.Range(0,40)){n.Gs.WriteChar((byte)x);n.Rf.WriteChar((byte)x);}
            Advance(2000000);
            Assert(n.AtRrls.Count==0&&n.AtGs.Count==0&&n.AtRf.Count==0,"simultaneous transmissions must be lost: "+n.AtRrls.Count+"/"+n.AtGs.Count+"/"+n.AtRf.Count);
            Assert(n.M.ReadCounter(2,12)==2&&n.M.ReadCounter(2,10)==80&&n.M.ReadCounter(1,12)==1,"collision counters: "+n.M.Snapshot());
        }
        // 2. RF holds the air with a 1000 B packet (130 ms); GS's 40 B packet
        // becomes ready 20 ms in. LBT off: GS transmits over it, and RRLS loses
        // both, while GS (transmitting) loses RF's packet too.
        // 3. LBT on: GS defers until RF ends plus 5-10 ms; nothing is lost.
        foreach(var lbt in new[]{0,1})
        {
            var n=Net("5:GLOBAL_RETRIES=0,6:SER_BRK_DETMS=5,18:LBT_RSSI="+lbt);
            Advance(1000000);
            foreach(var x in Enumerable.Range(0,1000))n.Rf.WriteChar((byte)x);
            // RF: 1000 B take 87 ms of UART, then 5 ms idle: on air at about 92 ms.
            Advance(1000000+92000+20000);
            var gsStart=Clock.NowUs;
            foreach(var x in Enumerable.Range(0,40))n.Gs.WriteChar((byte)x);
            var gsAt=new List<ulong>();
            Advance(Clock.NowUs+2000000);
            if(lbt==0)
            {
                Assert(n.AtRrls.Count==0&&n.AtGs.Count==0&&n.M.ReadCounter(1,13)==0,"LBT off: overlap must lose both at RRLS and RF at GS: rrls="+n.AtRrls.Count+" gs="+n.AtGs.Count);
                Assert(n.AtRf.Count==0,"RF was transmitting, it cannot receive GS");
            }
            else
            {
                Assert(n.AtRrls.Count==1040&&n.AtGs.Count==1000&&n.AtRf.Count==40,"LBT on: nothing lost: rrls="+n.AtRrls.Count);
                Assert(n.M.ReadCounter(1,13)>0&&n.M.ReadCounter(2,12)==0,"LBT on: GS must defer, no collision");
            }
            Console.WriteLine($"RADIO_RFD_ASYNC_OVERLAP lbt={lbt} rrls={n.AtRrls.Count} gs={n.AtGs.Count} rf={n.AtRf.Count} gs_lbt_waits={n.M.ReadCounter(1,13)} rrls_collided={n.M.ReadCounter(2,12)}");
        }
        // 4. LBT on: the deferred GS packet starts 5-10 ms after RF's packet
        // ends. RF's copy: (1000*8+160)/64000 s = 127.5 ms from about 92 ms.
        {
            var n=Net("5:GLOBAL_RETRIES=0,6:SER_BRK_DETMS=5,18:LBT_RSSI=1");
            var at=new List<ulong>();var rf2=n.AtRf;
            Advance(1000000);
            foreach(var x in Enumerable.Range(0,1000))n.Rf.WriteChar((byte)x);
            Advance(1000000+112000);
            foreach(var x in Enumerable.Range(0,40))n.Gs.WriteChar((byte)x);
            ulong first=0;while(Clock.NowUs<4000000){Advance(Clock.NowUs+100);if(first==0&&rf2.Count>0)first=Clock.NowUs;}
            // RF on air at 1000000+87000+5000(+quantum); ends 127500 later.
            ulong rfEnd=1000000+87000+5000+127500;
            ulong gsCopy=(480UL*1000000+63999)/64000;
            var gsAirStart=first-gsCopy-87;
            Console.WriteLine($"RADIO_RFD_ASYNC_LBT_BACKOFF gs_air_start_after_rf_end_us={(long)gsAirStart-(long)rfEnd}");
            Assert(gsAirStart>=rfEnd+5000&&gsAirStart<=rfEnd+10000+400,"LBT backoff must be 5-10 ms after the channel clears");
        }
        // 5. Blind S5=1, LBT off. RF's 20 B packet is on air 6.74-11.74 ms
        // (copy 2 to 16.74 ms); GS's 40 B packet (written 1 ms later) is on air
        // 9.48-16.98 ms, copy 2 16.98-24.48 ms. Only GS's second copy is clear
        // at RRLS, so RRLS gets exactly GS's packet. RF, still within its 2 ms
        // turnaround at 16.98 ms, gets nothing.
        {
            var n=Net("5:GLOBAL_RETRIES=1,6:SER_BRK_DETMS=5,18:LBT_RSSI=0");
            Advance(1000000);
            foreach(var x in Enumerable.Range(0,20))n.Rf.WriteChar((byte)x);
            Advance(1001000);
            foreach(var x in Enumerable.Range(100,40))n.Gs.WriteChar((byte)x);
            Advance(3000000);
            Assert(n.AtRrls.Count==40&&n.AtRrls[0]==100&&n.AtRf.Count==0&&n.AtGs.Count==0,
                "only the clear second copy may arrive: rrls="+n.AtRrls.Count+" rf="+n.AtRf.Count+" gs="+n.AtGs.Count);
        }
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_MAC",null);
        Environment.SetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY",null);
        Console.WriteLine("RADIO_RFD_ASYNC_COLLISION_HALF_DUPLEX_LBT_PASS");
    }
    static void Main()
    {
        var schedule=Environment.GetEnvironmentVariable("RADIO_PACKETIZATION_SCHEDULE");
        if(schedule!=null){PacketizationLatency(schedule);return;}
        ModemCommandMode();
        BroadcastCopiesAndDutyCycle();
        PerStationModemAirTiming();
        AsyncCollisionsHalfDuplexAndLbt();
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
