import struct, datetime
def read_dbf(path):
    with open(path, "rb") as f:
        hd = f.read(32)
        nrec, hlen, rlen = struct.unpack("<IHH", hd[4:12])
        fields = []
        while True:
            d = f.read(32)
            if d[0:1] == b"\r": break
            fields.append((d[:11].split(b"\0")[0].decode(), chr(d[11]), d[16], d[17]))
        f.seek(hlen)
        rows = []
        for _ in range(nrec):
            rec = f.read(rlen)
            if rec[0:1] == b"*": continue          # deleted
            pos, row = 1, {}
            for n, t, l, dc in fields:
                raw = rec[pos:pos + l].decode("latin-1").strip(); pos += l
                if t == "N" or t == "F":
                    try: row[n] = float(raw) if raw else None
                    except ValueError: row[n] = None
                elif t == "L": row[n] = {"T": True, "Y": True, "F": False, "N": False}.get(raw.upper())
                else: row[n] = raw or None
            rows.append(row)
    return fields, rows
