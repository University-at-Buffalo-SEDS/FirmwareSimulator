using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.Timers;

sealed class TestMachine : IMachine
{
    public object ClockSource { get { return this; } }
    public Action ResetAction;
    public int Resets;
    public void RequestReset() { Resets++; if(ResetAction != null) ResetAction(); }
}

static class Program
{
    private const uint BusClock = 1u << 10;
    private const uint WriteAccess = 1u << 8;
    private const uint BackupReset = 1u << 16;
    private static void Require(bool value, string why) { if(!value) throw new Exception(why); }
    private static uint Value(int index) { return 0x13579BDFu ^ unchecked((uint)index * 0x10203041u); }
    private static long Offset(int index) { return 0x100 + index * 4; }

    private static void ColdAndAccessGates()
    {
        var pwr = new SedsStm32Pwr("g4");
        var rcc = new SedsStm32Rcc("g4", pwr);
        var tamp = new SedsStm32G4Tamp(rcc, pwr);
        Require(rcc.ReadDoubleWord(0x58) == 0x400 && rcc.G4BackupBusEnabled, "RM0440 RTCAPBEN cold default");
        for(var index = 0; index < 32; index++) Require(tamp.ReadDoubleWord(Offset(index)) == 0, "cold backup values start zero");
        rcc.WriteDoubleWord(0x58, 0);
        pwr.WriteDoubleWord(0, WriteAccess);
        tamp.WriteDoubleWord(0x178, 0x12345678);
        rcc.WriteDoubleWord(0x58, BusClock);
        Require(tamp.ReadDoubleWord(0x178) == 0, "clock-off write cannot appear later");
        pwr.WriteDoubleWord(0, 0);
        tamp.WriteDoubleWord(0x178, 0x12345678);
        Require(tamp.ReadDoubleWord(0x178) == 0, "DBP-off write refused");
        pwr.WriteDoubleWord(0, WriteAccess);
        tamp.WriteDoubleWord(0x178, 0x12345678);
        Require(tamp.ReadDoubleWord(0x178) == 0x12345678, "enabled write has actual readback");
        rcc.WriteDoubleWord(0x58, 0);
        tamp.WriteDoubleWord(0x178, 0xFFFFFFFF);
        Require(tamp.ReadDoubleWord(0x178) == 0, "APB-off reads do not expose registers");
        rcc.WriteDoubleWord(0x58, BusClock);
        Require(tamp.ReadDoubleWord(0x178) == 0x12345678, "APB-off write retained old value");
        // PWREN clocks PWR access; do not treat clearing it as clearing an
        // already programmed DBP permission. PWR bus gating is outside subset.
        rcc.WriteDoubleWord(0x58, BusClock | (1u << 28));
        pwr.WriteDoubleWord(0, WriteAccess);
        rcc.WriteDoubleWord(0x58, BusClock);
        tamp.WriteDoubleWord(0x178, 0xABCD9876);
        Require(tamp.ReadDoubleWord(0x178) == 0xABCD9876, "stored DBP survives PWREN clear");
    }

    private static void AllWordsAndExactOffsets()
    {
        var pwr = new SedsStm32Pwr("g4");
        var rcc = new SedsStm32Rcc("g4", pwr);
        var tamp = new SedsStm32G4Tamp(rcc, pwr);
        rcc.WriteDoubleWord(0x58, BusClock);
        pwr.WriteDoubleWord(0, WriteAccess);
        for(var index = 0; index < 32; index++) tamp.WriteDoubleWord(Offset(index), Value(index));
        foreach(var offset in new long[] { -4, 0xFC, 0x101, 0x17D, 0x180, 0x400 })
        {
            tamp.WriteDoubleWord(offset, 0xFFFFFFFF);
            Require(tamp.ReadDoubleWord(offset) == 0, "unsupported offset cannot alias a backup word");
        }
        for(var index = 0; index < 32; index++) Require(tamp.ReadDoubleWord(Offset(index)) == Value(index), "32 arbitrary independent words");
        tamp.Reset();
        pwr.Reset();
        rcc.WriteDoubleWord(0x90, 0); // With DBP reset, this cannot alter BDCR.
        rcc.Reset();
        Require(!pwr.BackupWriteEnabled && rcc.G4BackupBusEnabled && rcc.ReadDoubleWord(0x58) == 0x400, "normal reset restores RTCAPBEN1 and DBP0");
        for(var index = 0; index < 32; index++) Require(tamp.ReadDoubleWord(Offset(index)) == Value(index), "ordinary reset retains backup contents");
        tamp.WriteDoubleWord(0x100, 0xFFFFFFFF);
        Require(tamp.ReadDoubleWord(0x100) == Value(0), "post-reset writes require DBP again");
    }

    private static void WatchdogResetRetainsBank()
    {
        var pwr = new SedsStm32Pwr("g4");
        var rcc = new SedsStm32Rcc("g4", pwr);
        var tamp = new SedsStm32G4Tamp(rcc, pwr);
        rcc.WriteDoubleWord(0x58, BusClock);
        pwr.WriteDoubleWord(0, WriteAccess);
        tamp.WriteDoubleWord(0x17C, 0xBADC0FFE);
        var machine = new TestMachine();
        machine.ResetAction = () => { tamp.Reset(); pwr.Reset(); rcc.Reset(); };
        var watchdog = new SedsStm32Iwdg(machine, rcc);
        watchdog.WriteDoubleWord(0, 0xCCCC);
        LimitTimer.Last.Fire();
        Require(machine.Resets == 1 && watchdog.GetResetCount() == 1, "actual watchdog expiry requests reset");
        Require(rcc.G4BackupBusEnabled && !pwr.BackupWriteEnabled, "IWDG reset restores RTCAPBEN1 and DBP0");
        Require(tamp.ReadDoubleWord(0x17C) == 0xBADC0FFE, "IWDG reset retains backup contents");
        Require((rcc.ReadDoubleWord(0x94) & (1u << 29)) != 0, "watchdog reset-cause behavior unchanged");
    }

    private static void ProtectedBackupResetAndColdInstance()
    {
        var pwr = new SedsStm32Pwr("g4");
        var rcc = new SedsStm32Rcc("g4", pwr);
        var tamp = new SedsStm32G4Tamp(rcc, pwr);
        rcc.WriteDoubleWord(0x58, BusClock);
        pwr.WriteDoubleWord(0, WriteAccess);
        for(var index = 0; index < 32; index++) tamp.WriteDoubleWord(Offset(index), Value(index));
        pwr.WriteDoubleWord(0, 0);
        rcc.WriteDoubleWord(0x90, BackupReset);
        Require(!rcc.G4BackupResetAsserted && tamp.ReadDoubleWord(0x100) == Value(0), "DBP protects backup reset");
        pwr.WriteDoubleWord(0, WriteAccess);
        const uint lsco = (1u << 24) | (1u << 25);
        const uint rtcConfiguration = (1u << 15) | (1u << 8) | 1u;
        rcc.WriteDoubleWord(0x90, lsco | rtcConfiguration);
        Require(rcc.ReadDoubleWord(0x90) == (lsco | rtcConfiguration), "ordinary requested BDCR configuration stored");
        rcc.WriteDoubleWord(0x90, rcc.ReadDoubleWord(0x90) | BackupReset);
        Require(rcc.G4BackupResetAsserted, "permitted BDRST assertion");
        Require(rcc.ReadDoubleWord(0x90) == (lsco | BackupReset), "BDRST clears RTC config but preserves requested LSCOEN and LSCOSEL");
        rcc.WriteDoubleWord(0x90, BackupReset | (1u << 24) | rtcConfiguration);
        Require(rcc.ReadDoubleWord(0x90) == (BackupReset | (1u << 24)), "held reset honors requested LSCO bits without retaining old LSCOSEL");
        tamp.WriteDoubleWord(0x178, 0xFEED1234);
        Require(tamp.ReadDoubleWord(0x178) == 0, "held backup reset blocks writes");
        rcc.Reset();
        pwr.Reset();
        tamp.Reset();
        rcc.WriteDoubleWord(0x58, BusClock);
        rcc.WriteDoubleWord(0x90, 0);
        Require(rcc.G4BackupResetAsserted, "normal reset preserves BDCR, protected release refused");
        Require(rcc.ReadDoubleWord(0x90) == (BackupReset | (1u << 24)), "system reset leaves LSCO outside RTC domain and DBP blocks its mutation");
        pwr.WriteDoubleWord(0, WriteAccess);
        rcc.WriteDoubleWord(0x90, 1u << 24);
        Require(rcc.ReadDoubleWord(0x90) == (1u << 24), "explicit reset release can preserve requested LSCOEN");
        for(var index = 0; index < 32; index++) Require(tamp.ReadDoubleWord(Offset(index)) == 0, "backup reset cleared every word");
        tamp.WriteDoubleWord(0x178, 0x12345678);
        Require(tamp.ReadDoubleWord(0x178) == 0x12345678, "release permits actual writes again");
        var coldPwr = new SedsStm32Pwr("g4");
        var coldRcc = new SedsStm32Rcc("g4", coldPwr);
        var coldTamp = new SedsStm32G4Tamp(coldRcc, coldPwr);
        coldRcc.WriteDoubleWord(0x58, BusClock);
        Require(coldTamp.ReadDoubleWord(0x178) == 0, "new powered instance does not inherit another bank");
    }

    private static void RealReadbackSequenceHasFailureControls()
    {
        var pwr = new SedsStm32Pwr("g4");
        var rcc = new SedsStm32Rcc("g4", pwr);
        var tamp = new SedsStm32G4Tamp(rcc, pwr);
        const uint magic = 0x41425254;
        rcc.WriteDoubleWord(0x58, BusClock);
        tamp.WriteDoubleWord(0x178, magic);
        tamp.WriteDoubleWord(0x17C, ~magic);
        Require(tamp.ReadDoubleWord(0x178) != magic, "unpermitted firmware marker sequence must fail");
        pwr.WriteDoubleWord(0, WriteAccess);
        tamp.WriteDoubleWord(0x178, magic);
        Require(tamp.ReadDoubleWord(0x17C) != ~magic, "torn marker remains a failed readback");
        tamp.WriteDoubleWord(0x17C, ~magic);
        Require(tamp.ReadDoubleWord(0x178) == magic && tamp.ReadDoubleWord(0x17C) == ~magic, "c7 backup readback succeeds only after both real writes");
        tamp.WriteDoubleWord(0x17C, magic);
        Require(tamp.ReadDoubleWord(0x17C) != ~magic, "incorrect complement is not repaired by model");
        tamp.WriteDoubleWord(0x170, 0x4C41554E);
        Require(tamp.ReadDoubleWord(0x178) == magic, "launch marker and abort marker are independent words");
    }

    private static void OtherVariantsRemainUnchanged()
    {
        var h5Pwr = new SedsStm32Pwr("h5");
        h5Pwr.WriteDoubleWord(0, WriteAccess);
        Require(!h5Pwr.BackupWriteEnabled, "G4 DBP location is not H5 permission");
        h5Pwr.WriteDoubleWord(0x24, 1);
        Require(h5Pwr.BackupWriteEnabled, "H5 existing DBP permission unchanged");
        foreach(var variant in new[] { "h5", "u5", "h7", "h7rs" })
        {
            var rcc = new SedsStm32Rcc(variant);
            rcc.WriteDoubleWord(0x90, 0x12345678);
            Require(rcc.ReadDoubleWord(0x90) == 0x12345678, "G4 BDCR protection cannot affect other variants");
            rcc.Reset();
            Require(rcc.ReadDoubleWord(0x90) == 0, "other variant register reset unchanged");
            Require(!rcc.G4BackupBusEnabled && !rcc.G4BackupResetAsserted, "G4-only predicate stays scoped");
        }
    }

    static void Main()
    {
        ColdAndAccessGates();
        AllWordsAndExactOffsets();
        WatchdogResetRetainsBank();
        ProtectedBackupResetAndColdInstance();
        RealReadbackSequenceHasFailureControls();
        OtherVariantsRemainUnchanged();
        Console.WriteLine("PASS G4 TAMP actual register storage, write gates, retained resets, protected BDRST and readback failure controls");
    }
}

namespace Antmicro.Renode.Core { public interface IMachine { object ClockSource { get; } void RequestReset(); } }
namespace Antmicro.Renode.Peripherals.Bus
{
    public interface IDoubleWordPeripheral { uint ReadDoubleWord(long offset); void WriteDoubleWord(long offset, uint value); void Reset(); }
    public interface IKnownSize { long Size { get; } }
}
namespace Antmicro.Renode.Logging
{
    public enum LogLevel { Warning, Error }
    public static class Logging { public static void Log(this object owner, LogLevel level, string text, params object[] args) { } }
}
namespace Antmicro.Renode.Time { public enum Direction { Ascending, Descending } }
namespace Antmicro.Renode.Peripherals.Timers
{
    public sealed class LimitTimer
    {
        public LimitTimer(object clock, long frequency, object owner, string name, ulong limit,
            Antmicro.Renode.Time.Direction direction, bool enabled, bool eventEnabled)
        { Last = this; Limit = limit; Enabled = enabled; }
        public static LimitTimer Last;
        public event Action LimitReached;
        public ulong Limit { get; set; }
        public ulong Value { get; set; }
        public bool Enabled { get; set; }
        public void Fire() { if(Enabled && LimitReached != null) LimitReached(); }
    }
}
