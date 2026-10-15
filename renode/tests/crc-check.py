c = monitor.Machine["sysbus.crc"]
c.WriteDoubleWord(0x10, 0xffffffff)
c.WriteDoubleWord(8, 1)
c.WriteDoubleWord(0, 0x12345678)
assert c.ReadDoubleWord(0) == 0xdf8a8a2b
print("CRC_POLY_WORD_PASS")
def reverse(v):
    r = 0
    for i in range(32):
        r = (r << 1) | (v & 1)
        v >>= 1
    return r

def reference(state, data):
    for b in data:
        state ^= b
        for bit in range(8):
            state = (state >> 1) ^ (0xedb88320 if state & 1 else 0)
    return state

def hardware(state, data):
    c.WriteDoubleWord(0x14, 0x04c11db7)
    c.WriteDoubleWord(0x10, reverse(state))
    c.WriteDoubleWord(8, 0xe1)
    off = 0
    while off + 4 <= len(data):
        c.WriteDoubleWord(0, data[off] | data[off+1] << 8 | data[off+2] << 16 | data[off+3] << 24)
        off += 4
    c.WriteDoubleWord(8, 0xa0)
    for b in data[off:]:
        c.WriteByte(0, b)
    return c.ReadDoubleWord(0)
assert hardware(0xffffffff, [ord(b) for b in "123456789"]) == 0x340bc6d9
print("CRC_IEEE_WORD_BYTE_PASS")
data = [(i * 73 + i // 251) & 255 for i in range(4096)]
for length in [0,1,2,3,4,5,9,14,35,64,128,255,256,1024,4096]:
    for seed in [0,0xffffffff,0x13579bdf]:
        want = reference(seed, data[:length])
        for split in [0,length//2,length]:
            got = hardware(seed, data[:split])
            got = hardware(got, data[split:length])
            assert got == want, (length, seed, split, got, want)
print("CRC_INCREMENTAL_LENGTHS_PASS")
c.Reset()
assert c.ReadDoubleWord(0) == 0xffffffff
assert c.ReadDoubleWord(8) == 0
assert c.ReadDoubleWord(0x14) == 0x04c11db7
assert c.BytesWritten == 0
print("CRC_RESET_PASS")
