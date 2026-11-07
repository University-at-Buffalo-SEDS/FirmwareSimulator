using System;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // G4 TAMP backup-register subset. No RTC/calendar/tamper input behavior.
    public sealed class SedsStm32G4Tamp : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32G4Tamp(SedsStm32Rcc rcc, SedsStm32Pwr pwr)
        {
            if(rcc == null) throw new ArgumentNullException("rcc");
            if(pwr == null) throw new ArgumentNullException("pwr");
            this.rcc = rcc;
            this.pwr = pwr;
            rcc.G4BackupReset += BackupReset;
        }

        public uint ReadDoubleWord(long offset)
        {
            if(!rcc.G4BackupBusEnabled || rcc.G4BackupResetAsserted || !IsBackupRegister(offset)) return 0;
            return backup[(int)((offset - 0x100) / 4)];
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(!rcc.G4BackupBusEnabled || rcc.G4BackupResetAsserted || !pwr.BackupWriteEnabled || !IsBackupRegister(offset)) return;
            backup[(int)((offset - 0x100) / 4)] = value;
        }

        // A normal MCU/peripheral/IWDG reset is not backup-domain power loss.
        // RCC/PWR reset controls to hardware defaults; stored words stay.
        public void Reset() { }
        public long Size { get { return 0x400; } }

        private static bool IsBackupRegister(long offset)
        {
            return offset >= 0x100 && offset <= 0x17C && (offset & 3) == 0;
        }
        private void BackupReset() { Array.Clear(backup, 0, backup.Length); }
        private readonly SedsStm32Rcc rcc;
        private readonly SedsStm32Pwr pwr;
        private readonly uint[] backup = new uint[32];
    }
}
