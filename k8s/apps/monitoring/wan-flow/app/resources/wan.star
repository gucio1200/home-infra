def octets(s):
    p = s.split(".")
    if len(p) != 4:
        return None
    o = []
    for x in p:
        if not x.isdigit():
            return None
        o.append(int(x))
    return o

def is_private(o):
    return (o[0] in (0, 10, 127) or o[0] >= 224 or
            (o[0] == 172 and o[1] >= 16 and o[1] <= 31) or
            (o[0] == 192 and o[1] == 168) or
            (o[0] == 169 and o[1] == 254) or
            (o[0] == 100 and o[1] >= 64 and o[1] <= 127))

def is_lan(o):
    return (o[0] == 192 and o[1] == 168 and o[2] != 100) or (o[0] == 172 and o[1] == 16 and o[2] == 99)

def apply(metric):
    lan = None
    remote = False
    for k in ("src", "dst", "xlat_src", "xlat_dst"):
        v = metric.fields.get(k)
        if type(v) != "string":
            continue
        o = octets(v)
        if o == None:
            continue
        if is_lan(o):
            if lan == None:
                lan = v
        elif not is_private(o):
            remote = True
    if lan == None or not remote:
        return None
    s = octets(str(metric.fields.get("src", "")))
    direction = "download" if s != None and not is_private(s) else "upload"
    b = metric.fields.get("in_bytes") or metric.fields.get("out_bytes") or 0
    key = lan + "/" + direction
    state[key] = state.get(key, 0) + b
    m = Metric("wan")
    m.tags["ip"] = lan
    m.tags["direction"] = direction
    m.fields["bytes_total"] = state[key]
    m.time = metric.time
    return m
