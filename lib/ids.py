import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    v = (int(time.time() * 1000) & ((1 << 48) - 1)) << 80
    v |= int.from_bytes(os.urandom(10), "big")
    out = []
    for shift in range(125, -1, -5):
        out.append(_ALPHABET[(v >> shift) & 0x1F])
    return "".join(out)
