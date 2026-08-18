"""
Minimal SoundFont 2 preset reader.

An .sf2 is a RIFF file. The preset list lives in the pdta LIST chunk, in a
sub-chunk called phdr, as an array of fixed 38-byte records:

    char     achPresetName[20]
    uint16   wPreset          (program number)
    uint16   wBank
    uint16   wPresetBagNdx
    uint32   dwLibrary
    uint32   dwGenre
    uint32   dwMorphology

The final record is an "EOP" sentinel and is discarded.

Reading the file directly means patch names work regardless of whether
FluidSynth's command socket is enabled.
"""

import struct
from pathlib import Path

PHDR_RECORD = 38


def _walk(data, start, end):
    """Yield (chunk_id, body_start, body_end) over RIFF chunks in a range."""
    pos = start
    while pos + 8 <= end:
        cid = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        stop = min(body + size, end)
        yield cid, body, stop
        pos = body + size + (size & 1)   # chunks are word-aligned


def read_presets(path):
    """Return [(bank, program, name), ...] sorted by (bank, program)."""
    data = Path(path).read_bytes()
    if data[0:4] != b"RIFF" or data[8:12] != b"sfbk":
        raise ValueError(f"{path} is not a SoundFont 2 file")

    riff_end = 8 + struct.unpack_from("<I", data, 4)[0]
    riff_end = min(riff_end, len(data))

    pdta = None
    for cid, body, stop in _walk(data, 12, riff_end):
        if cid == b"LIST" and data[body:body + 4] == b"pdta":
            pdta = (body + 4, stop)
            break
    if not pdta:
        raise ValueError(f"{path}: no pdta chunk")

    phdr = None
    for cid, body, stop in _walk(data, pdta[0], pdta[1]):
        if cid == b"phdr":
            phdr = (body, stop)
            break
    if not phdr:
        raise ValueError(f"{path}: no phdr chunk")

    start, stop = phdr
    presets = []
    count = (stop - start) // PHDR_RECORD
    for i in range(count):
        off = start + i * PHDR_RECORD
        raw_name = data[off:off + 20]
        name = raw_name.split(b"\0", 1)[0].decode("latin-1", "replace").strip()
        prog, bank = struct.unpack_from("<HH", data, off + 20)
        if name == "EOP" and i == count - 1:
            break
        if not name:
            continue
        presets.append((bank, prog, name))

    presets.sort(key=lambda p: (p[0], p[1]))
    return presets


if __name__ == "__main__":
    import sys
    for bank, prog, name in read_presets(sys.argv[1]):
        print(f"{bank:4d} {prog:4d}  {name}")
