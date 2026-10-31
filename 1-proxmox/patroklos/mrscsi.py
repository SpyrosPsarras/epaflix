#!/usr/bin/env python3
"""Send SCSI commands to a drive behind a MegaRAID/PERC controller (MFI pass-through, as smartctl -d megaraid)."""
import ctypes, os, struct, sys, time

MEGASAS_IOC_FIRMWARE = 0xC1944D01
MFI_CMD_PD_SCSI_IO = 0x04
DIR_NONE, DIR_WRITE, DIR_READ = 0x0000, 0x0008, 0x0010
NODE = "/dev/megaraid_sas_ioctl_node"


class IOVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


class IocPacket(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("host_no", ctypes.c_uint16), ("pad", ctypes.c_uint16), ("sgl_off", ctypes.c_uint32),
                ("sge_count", ctypes.c_uint32), ("sense_off", ctypes.c_uint32), ("sense_len", ctypes.c_uint32),
                ("frame", ctypes.c_uint8 * 128), ("sgl", IOVec * 16)]


assert ctypes.sizeof(IocPacket) == 404
libc = ctypes.CDLL(None, use_errno=True)


def node():
    if not os.path.exists(NODE):
        major = next(int(l.split()[0]) for l in open("/proc/devices") if l.split()[-1] == "megaraid_sas_ioctl")
        os.mknod(NODE, 0o600 | 0o020000, os.makedev(major, 0))
    return os.open(NODE, os.O_RDWR)


def scsi(fd, host, target, cdb, direction=DIR_NONE, data=b"", read_len=0):
    pkt = IocPacket()
    pkt.host_no = host
    buf = ctypes.create_string_buffer(data if direction == DIR_WRITE else read_len) if (data or read_len) else None
    sense = ctypes.create_string_buffer(64)
    xfer = len(data) if direction == DIR_WRITE else read_len
    f = bytearray(128)
    f[0], f[1], f[2], f[4], f[6] = MFI_CMD_PD_SCSI_IO, 64, 0xFF, target, len(cdb)
    struct.pack_into("<HHI", f, 0x10, direction, 0, xfer)
    struct.pack_into("<Q", f, 0x18, ctypes.addressof(sense))
    f[0x20:0x20 + len(cdb)] = cdb
    pkt.sense_off, pkt.sense_len = 0x18, 64
    if buf is not None:
        f[7] = 1
        struct.pack_into("<II", f, 0x30, 0, xfer)
        pkt.sge_count, pkt.sgl_off = 1, 0x30
        pkt.sgl[0].base, pkt.sgl[0].len = ctypes.addressof(buf), xfer
    ctypes.memmove(pkt.frame, bytes(f), 128)
    rc = libc.ioctl(fd, ctypes.c_ulong(MEGASAS_IOC_FIRMWARE), ctypes.byref(pkt))
    if rc != 0:
        raise OSError(ctypes.get_errno(), "ioctl failed")
    status, scsi_status = pkt.frame[2], pkt.frame[3]
    return status, scsi_status, (buf.raw[:xfer] if buf is not None and direction == DIR_READ else b""), sense.raw


def capacity(fd, host, t):
    st, ss, d, _ = scsi(fd, host, t, bytes([0x9E, 0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 32, 0, 0]), DIR_READ, read_len=32)
    last, blen = struct.unpack(">QI", d[:12])
    return st, ss, last + 1, blen, d[12] & 1


def progress(fd, host, t):
    st, ss, d, _ = scsi(fd, host, t, bytes([0x03, 0, 0, 0, 252, 0]), DIR_READ, read_len=252)
    key, asc, ascq = d[2] & 0x0F, d[12], d[13]
    pct = (struct.unpack(">H", d[16:18])[0] * 100 / 65536) if d[15] & 0x80 else None
    return st, key, asc, ascq, pct


def mode_select_4096(fd, host, t):
    hdr_desc = bytes([0, 0, 0, 0, 0, 0, 0, 8]) + bytes([0xFF, 0xFF, 0xFF, 0xFF, 0, 0x00, 0x10, 0x00])
    cdb = bytes([0x55, 0x10, 0, 0, 0, 0, 0, 0, len(hdr_desc), 0])
    return scsi(fd, host, t, cdb, DIR_WRITE, data=hdr_desc)[:2]


def format_unit_immed(fd, host, t):
    cdb = bytes([0x04, 0x10, 0, 0, 0, 0])
    return scsi(fd, host, t, cdb, DIR_WRITE, data=bytes([0x00, 0x02, 0x00, 0x00]))[:2]


def selftest():
    from unittest.mock import patch

    assert ctypes.sizeof(IocPacket) == 404
    cases = [
        (mode_select_4096, "55100000000000001000", "0000000000000008ffffffff00001000",
         "0440ff0001000a0100000000000000000800000010000000", 16),
        (format_unit_immed, "041000000000", "00020000",
         "0440ff000100060100000000000000000800000004000000", 4),
        (capacity, "9e100000000000000000000000200000", None,
         "0440ff000100100100000000000000001000000020000000", 32),
    ]
    for command, cdb_hex, data_hex, header_hex, xfer in cases:
        def check_ioctl(fd, request, arg):
            pkt = arg._obj
            assert fd == -1 and request.value == MEGASAS_IOC_FIRMWARE
            assert pkt.host_no == 0
            assert (pkt.sense_off, pkt.sense_len, pkt.sge_count, pkt.sgl_off) == (0x18, 64, 1, 0x30)
            frame = bytearray(pkt.frame)
            assert struct.unpack_from("<Q", frame, 0x18)[0] != 0
            frame[0x18:0x20] = bytes(8)
            expected = (bytes.fromhex(header_hex) + bytes(8)
                        + bytes.fromhex(cdb_hex).ljust(16, b"\x00")
                        + struct.pack("<II", 0, xfer) + bytes(72))
            assert frame == expected
            assert pkt.sgl[0].len == xfer
            data = ctypes.string_at(pkt.sgl[0].base, xfer)
            assert data == (bytes.fromhex(data_hex) if data_hex is not None else bytes(xfer))
            pkt.frame[2] = 0
            return 0

        with patch.object(libc, "ioctl", side_effect=check_ioctl) as ioctl:
            command(-1, 0, 1)
            assert ioctl.call_count == 1
    print("selftest: MODE SELECT(10), FORMAT UNIT, READ CAPACITY(16), MFI frames and 404-byte struct OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["selftest"]:
        selftest()
        sys.exit(0)
    action, host, targets = sys.argv[1], int(sys.argv[2]), [int(x) for x in sys.argv[3].split(",")]
    fd = node()
    for t in targets:
        if action == "cap":
            print(t, "status=%d scsi=%d blocks=%d block_len=%d prot_en=%d" % capacity(fd, host, t))
        elif action == "progress":
            print(t, "status=%d key=%x asc=%02x ascq=%02x pct=%s" % progress(fd, host, t))
        elif action == "format":
            st, ss, blocks, blen, _ = capacity(fd, host, t)
            print(t, "before: blocks=%d block_len=%d" % (blocks, blen))
            print(t, "mode select 4096: status=%d scsi=%d" % mode_select_4096(fd, host, t))
            print(t, "format unit (immed): status=%d scsi=%d" % format_unit_immed(fd, host, t))
        sys.stdout.flush()
