using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Memory;
using Antmicro.Renode.Logging.Profiling;

namespace Antmicro.Renode.Peripherals.MTD
{
    // MappedMemory makes flash executable by Renode's translation engine. A
    // CPU memory hook retains STM32 programming rules and operation tracing.
    public sealed class SedsStm32FlashController : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32FlashController(IMachine machine, MappedMemory flash,
            string mcu, uint eraseSize, uint writeAlignment, ulong flashBase)
        {
            if(eraseSize == 0 || writeAlignment == 0) throw new ArgumentException("invalid flash geometry");
            this.machine = machine;
            this.flash = flash;
            this.mcu = mcu;
            this.eraseSize = eraseSize;
            this.writeAlignment = writeAlignment;
            this.flashBase = flashBase;
            flash.ResetByte = 0xff;
            flash.ZeroAll();
            shadow = new byte[checked((int)flash.Size)];
            for(var i = 0; i < shadow.Length; i++) shadow[i] = 0xff;
            InstallMemoryHooks();
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == StatusOffset) return status;
            if(offset == ControlOffset) return control | (locked ? LockBit : 0u);
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == KeyOffset)
            {
                if(keyStage == 0 && value == 0x45670123u) keyStage = 1;
                else if(keyStage == 1 && value == 0xCDEF89ABu) { locked = false; keyStage = 0; }
                else keyStage = 0;
                return;
            }
            if(offset == StatusClearOffset || (!IsH5 && offset == StatusOffset))
            {
                status &= ~value;
                return;
            }
            if(offset != ControlOffset || locked) return;
            control = value & ~LockBit;
            if((value & LockBit) != 0) locked = true;
            if((value & ProgramBit) != 0) BeginProgramming(); else EndProgramming();
            if((value & StartBit) != 0 && (value & EraseBit) != 0) EraseSelected(value);
        }

        // LoadELF/LoadBinary write directly to the backing store. Seal that
        // phase and snapshot bytes before firmware is allowed to execute.
        public void EndHostLoading()
        {
            var contents = flash.ReadBytes(0, checked((int)flash.Size));
            Array.Copy(contents, shadow, shadow.Length);
            hostLoadingEnded = true;
        }

        public void Reset()
        {
            locked = true;
            keyStage = 0;
            status = 0;
            control = 0;
            programming = false;
            programBytes = 0;
            programUnitStart = -1;
            powered = true;
            InstallMemoryHooks();
        }

        public long Size { get { return 0x400; } }
        public ulong GetOperationCount() { return operationCount; }
        public ulong GetEraseCount() { return eraseCount; }
        public ulong GetProgramCount() { return programCount; }
        public string GetOperationTrace() { return String.Join(",", operationTrace); }
        public bool GetPowerCutTriggered() { return powerCutTriggered; }

        public void ArmPowerCut(ulong operation)
        {
            if(operation == 0) throw new ArgumentException("power-cut operation must be positive");
            cutAfterOperation = operation;
            powerCutTriggered = false;
            powered = true;
        }

        public void DisarmPowerCut()
        {
            cutAfterOperation = ulong.MaxValue;
            powerCutTriggered = false;
            powered = true;
        }

        private void InstallMemoryHooks()
        {
            foreach(var cpu in machine.GetSystemBus(this).GetCPUs().OfType<ICPUWithMemoryAccessHooks>())
            {
                cpu.SetHookAtMemoryAccess((MemoryAccessHook)OnMemoryAccess);
            }
        }

        private void OnMemoryAccess(ulong _, MemoryOperation operation, ulong __,
            ulong physicalAddress, uint width, ulong ___)
        {
            if(operation != MemoryOperation.MemoryWrite || physicalAddress < flashBase
                || physicalAddress >= flashBase + (ulong)flash.Size) return;
            var target = machine.GetSystemBus(this).WhatIsAt(physicalAddress)?.Peripheral;
            if(target != flash) return;

            var offset = checked((int)(physicalAddress - flashBase));
            var count = checked((int)width);
            if(count <= 0 || offset > shadow.Length - count) return;
            var requested = flash.ReadBytes(offset, count);
            if(!hostLoadingEnded || !powered || !programming)
            {
                flash.WriteBytes(offset, shadow, offset, count);
                if(hostLoadingEnded && powered) status |= ProgrammingErrorBit;
                return;
            }

            if(programBytes == 0)
            {
                if(offset % writeAlignment != 0)
                {
                    flash.WriteBytes(offset, shadow, offset, count);
                    status |= ProgrammingErrorBit;
                    return;
                }
                programUnitStart = offset;
            }
            else if(offset != programUnitStart + programBytes || programBytes + count > writeAlignment)
            {
                flash.WriteBytes(offset, shadow, offset, count);
                status |= ProgrammingErrorBit;
                return;
            }

            var corrected = new byte[count];
            var transitionError = false;
            for(var n = 0; n < count; n++)
            {
                var oldValue = shadow[offset + n];
                if((requested[n] | oldValue) != oldValue) transitionError = true;
                corrected[n] = (byte)(oldValue & requested[n]);
                shadow[offset + n] = corrected[n];
            }
            flash.WriteBytes(offset, corrected);
            if(transitionError) status |= ProgrammingErrorBit;

            programBytes += (uint)count;
            if(programBytes == writeAlignment)
            {
                programBytes = 0;
                programUnitStart = -1;
                programCount++;
                status |= EndOfOperationBit;
                RecordEvent("program_unit");
            }
        }

        private void BeginProgramming()
        {
            if(programming) return;
            programming = true;
            programBytes = 0;
            programUnitStart = -1;
        }

        private void EndProgramming()
        {
            if(programming && programBytes != 0) status |= ProgrammingErrorBit;
            programming = false;
            programBytes = 0;
            programUnitStart = -1;
        }

        private void EraseSelected(uint value)
        {
            uint page;
            long bankOffset = 0;
            if(mcu == "stm32h523")
            {
                page = (value >> 6) & 0x1f;
                if((value & (1u << 31)) != 0) bankOffset = flash.Size / 2;
            }
            else page = (value >> 3) & 0x7f;
            if(mcu == "stm32u585" && (value & (1u << 11)) != 0) bankOffset = flash.Size / 2;
            var offset = checked(bankOffset + (long)page * eraseSize);
            if(offset < 0 || offset > flash.Size - eraseSize)
            {
                status |= ProgrammingErrorBit;
                return;
            }
            RecordEvent("erase_start");
            if(!powered)
            {
                control &= ~(StartBit | EraseBit);
                return;
            }
            flash.SetRange(offset, eraseSize, 0xff);
            for(long i = offset; i < offset + eraseSize; i++) shadow[checked((int)i)] = 0xff;
            eraseCount++;
            status |= EndOfOperationBit;
            control &= ~(StartBit | EraseBit);
            RecordEvent("erase_complete");
        }

        private void RecordEvent(string eventName)
        {
            operationCount++;
            operationTrace.Add(eventName);
            CheckPowerCut();
        }

        private void CheckPowerCut()
        {
            if(operationCount != cutAfterOperation) return;
            powered = false;
            powerCutTriggered = true;
            machine.LocalTimeSource.ExecuteInNearestSyncedState(_ => machine.Pause());
        }

        private bool IsH5 { get { return mcu == "stm32h523"; } }
        private bool IsTrustZonePart { get { return IsH5 || mcu == "stm32u585"; } }
        private long KeyOffset { get { return IsH5 ? 0x04 : 0x08; } }
        private long StatusOffset { get { return IsTrustZonePart ? 0x20 : 0x10; } }
        private long StatusClearOffset { get { return IsH5 ? 0x30 : StatusOffset; } }
        private long ControlOffset { get { return IsTrustZonePart ? 0x28 : 0x14; } }
        private uint LockBit { get { return IsH5 ? 1u : 1u << 31; } }
        private uint ProgramBit { get { return IsH5 ? 1u << 1 : 1u; } }
        private uint EraseBit { get { return IsH5 ? 1u << 2 : 1u << 1; } }
        private uint StartBit { get { return IsH5 ? 1u << 5 : 1u << 16; } }
        private uint EndOfOperationBit { get { return IsH5 ? 1u << 16 : 1u; } }
        private uint ProgrammingErrorBit { get { return IsH5 ? 1u << 18 : 1u << 3; } }

        private readonly IMachine machine;
        private readonly MappedMemory flash;
        private readonly byte[] shadow;
        private readonly string mcu;
        private readonly uint eraseSize;
        private readonly uint writeAlignment;
        private readonly ulong flashBase;
        private readonly List<string> operationTrace = new List<string>();
        private bool locked;
        private int keyStage;
        private uint status;
        private uint control;
        private bool programming;
        private uint programBytes;
        private long programUnitStart = -1;
        private bool hostLoadingEnded;
        private bool powered = true;
        private ulong cutAfterOperation = ulong.MaxValue;
        private bool powerCutTriggered;
        private ulong operationCount;
        private ulong eraseCount;
        private ulong programCount;
    }
}
