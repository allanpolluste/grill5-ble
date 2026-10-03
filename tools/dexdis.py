"""Minimal Dalvik (.dex) disassembler - Python standard library only.

Prints fields and disassembled methods for every class whose descriptor starts with a prefix.
Good enough to read BLE protocol code (UUIDs, byte arrays, constants, call order) out of an APK
when Java/jadx is not available.

Usage:  unzip app.apk -d x
        python3 dexdis.py x/classes.dex Lcom/vendor/app/bluetooth/ > out.txt
"""
import sys, struct

d = open(sys.argv[1], 'rb').read()
want = sys.argv[2]  # class descriptor prefix, e.g. Lcom/irainxun/grilltempsense/bluetooth/

def u4(o): return struct.unpack_from('<I', d, o)[0]
def u2(o): return struct.unpack_from('<H', d, o)[0]
def uleb(p):
    r = s = 0
    while True:
        b = d[p]; p += 1; r |= (b & 0x7f) << s; s += 7
        if b < 0x80: return r, p

hdr = {k: u4(0x38 + 4 * i) for i, k in enumerate(
    ['str_n', 'str_o', 'typ_n', 'typ_o', 'pro_n', 'pro_o', 'fld_n', 'fld_o',
     'mth_n', 'mth_o', 'cls_n', 'cls_o'])}

strs = []
for i in range(hdr['str_n']):
    so = u4(hdr['str_o'] + 4 * i); _, p = uleb(so); e = d.index(b'\0', p)
    strs.append(d[p:e].decode('utf8', 'replace'))
types = [strs[u4(hdr['typ_o'] + 4 * i)] for i in range(hdr['typ_n'])]
def short(t): return t.split('/')[-1].rstrip(';')
fields = []
for i in range(hdr['fld_n']):
    o = hdr['fld_o'] + 8 * i
    fields.append(f"{short(types[u2(o)])}.{strs[u4(o + 4)]}")
methods = []
for i in range(hdr['mth_n']):
    o = hdr['mth_o'] + 8 * i
    methods.append(f"{short(types[u2(o)])}.{strs[u4(o + 4)]}")

# (name, format) per opcode
OP = {}
def setop(rng, fmt, names):
    for i, op in enumerate(rng): OP[op] = (names[i] if isinstance(names, list) else names, fmt)
setop([0x00], '10x', 'nop'); setop([0x01, 0x04, 0x07], '12x', 'move')
setop([0x02, 0x05, 0x08], '22x', 'move/16'); setop([0x03, 0x06, 0x09], '32x', 'move/32')
setop(range(0x0a, 0x0e), '11x', 'move-result'); setop([0x0e], '10x', 'return-void')
setop(range(0x0f, 0x12), '11x', 'return'); setop([0x12], '11n', 'const/4')
setop([0x13, 0x16], '21s', 'const/16'); setop([0x14, 0x17], '31i', 'const')
setop([0x15, 0x19], '21h', 'const/high16'); setop([0x18], '51l', 'const-wide')
setop([0x1a], '21c', 'const-string'); setop([0x1b], '31c', 'const-string')
setop([0x1c], '21c', 'const-class'); setop([0x1d, 0x1e], '11x', 'monitor')
setop([0x1f], '21c', 'check-cast'); setop([0x20], '22c', 'instance-of')
setop([0x21], '12x', 'array-length'); setop([0x22], '21c', 'new-instance')
setop([0x23], '22c', 'new-array'); setop([0x24], '35c', 'filled-new-array')
setop([0x25], '3rc', 'filled-new-array/range'); setop([0x26], '31t', 'fill-array-data')
setop([0x27], '11x', 'throw'); setop([0x28], '10t', 'goto'); setop([0x29], '20t', 'goto')
setop([0x2a], '30t', 'goto'); setop([0x2b], '31t', 'packed-switch'); setop([0x2c], '31t', 'sparse-switch')
setop(range(0x2d, 0x32), '23x', 'cmp')
setop(range(0x32, 0x38), '22t', ['if-eq', 'if-ne', 'if-lt', 'if-ge', 'if-gt', 'if-le'])
setop(range(0x38, 0x3e), '21t', ['if-eqz', 'if-nez', 'if-ltz', 'if-gez', 'if-gtz', 'if-lez'])
setop(range(0x3e, 0x44), '10x', 'unused')
setop(range(0x44, 0x4b), '23x', 'aget'); setop(range(0x4b, 0x52), '23x', 'aput')
setop(range(0x52, 0x59), '22c', 'iget'); setop(range(0x59, 0x60), '22c', 'iput')
setop(range(0x60, 0x67), '21c', 'sget'); setop(range(0x67, 0x6e), '21c', 'sput')
setop(range(0x6e, 0x73), '35c', 'invoke'); setop([0x73], '10x', 'unused')
setop(range(0x74, 0x79), '3rc', 'invoke/range'); setop([0x79, 0x7a], '10x', 'unused')
UN = ['neg-int', 'not-int', 'neg-long', 'not-long', 'neg-float', 'neg-double', 'int-to-long',
      'int-to-float', 'int-to-double', 'long-to-int', 'long-to-float', 'long-to-double',
      'float-to-int', 'float-to-long', 'float-to-double', 'double-to-int', 'double-to-long',
      'double-to-float', 'int-to-byte', 'int-to-char', 'int-to-short']
setop(range(0x7b, 0x90), '12x', UN)
BIN = ['add', 'sub', 'mul', 'div', 'rem', 'and', 'or', 'xor', 'shl', 'shr', 'ushr']
binn = [f"{b}-int" for b in BIN] + [f"{b}-long" for b in BIN] + \
       [f"{b}-float" for b in BIN[:5]] + [f"{b}-double" for b in BIN[:5]]
setop(range(0x90, 0xb0), '23x', binn)
setop(range(0xb0, 0xd0), '12x', [b + '/2addr' for b in binn])
setop(range(0xd0, 0xd8), '22s', [b + '-int/lit16' for b in ['add', 'rsub', 'mul', 'div', 'rem', 'and', 'or', 'xor']])
setop(range(0xd8, 0xe3), '22b', [b + '-int/lit8' for b in ['add', 'rsub', 'mul', 'div', 'rem', 'and', 'or', 'xor', 'shl', 'shr', 'ushr']])
setop(range(0xe3, 0xfa), '10x', 'unused'); setop([0xfa], '45cc', 'invoke-polymorphic')
setop([0xfb], '4rcc', 'invoke-polymorphic/range'); setop([0xfc], '35c', 'invoke-custom')
setop([0xfd], '3rc', 'invoke-custom/range'); setop([0xfe, 0xff], '21c', 'const-method')
SIZE = {'10x': 1, '12x': 1, '11n': 1, '11x': 1, '10t': 1, '20t': 2, '22x': 2, '21t': 2, '21s': 2,
        '21h': 2, '21c': 2, '23x': 2, '22b': 2, '22t': 2, '22s': 2, '22c': 2, '30t': 3, '32x': 3,
        '31i': 3, '31t': 3, '31c': 3, '35c': 3, '3rc': 3, '45cc': 4, '4rcc': 4, '51l': 5}
INV = {0x6e: 'virtual', 0x6f: 'super', 0x70: 'direct', 0x71: 'static', 0x72: 'interface',
       0x74: 'virtual', 0x75: 'super', 0x76: 'direct', 0x77: 'static', 0x78: 'interface'}

def ref(op, idx):
    if op in (0x1a, 0x1b): return repr(strs[idx])
    if op in (0x1c, 0x1f, 0x20, 0x22, 0x23, 0x24, 0x25): return short(types[idx])
    if 0x52 <= op <= 0x6d: return fields[idx]
    if 0x6e <= op <= 0x78 or op == 0xfa or op == 0xfb: return methods[idx]
    return str(idx)

def s16(v): return v - 0x10000 if v & 0x8000 else v
def s32(v): return v - 0x100000000 if v & 0x80000000 else v

def dis(code_off):
    regs, ins, outs, tries, dbg, n = struct.unpack_from('<HHHHII', d, code_off)
    base = code_off + 16
    pc = 0
    while pc < n:
        w = u2(base + 2 * pc); op = w & 0xff
        if op == 0 and w in (0x100, 0x200, 0x300):  # payloads
            if w == 0x300:
                width, cnt = u2(base + 2 * pc + 2), u4(base + 2 * pc + 4)
                pc += (8 + width * cnt + 1) // 2
            elif w == 0x100: pc += 4 + u2(base + 2 * pc + 2) * 2
            else: pc += 2 + u2(base + 2 * pc + 2) * 4
            continue
        name, fmt = OP[op]; a = w >> 8; o = base + 2 * pc; txt = ''
        if fmt == '12x' or fmt == '11n':
            va, vb = a & 0xf, a >> 4
            txt = f"v{va}, #{vb - 16 if vb > 7 else vb}" if fmt == '11n' else f"v{va}, v{vb}"
        elif fmt == '11x': txt = f"v{a}"
        elif fmt == '10t': txt = f"-> {pc + (a - 256 if a > 127 else a)}"
        elif fmt == '20t': txt = f"-> {pc + s16(u2(o + 2))}"
        elif fmt == '30t': txt = f"-> {pc + s32(u4(o + 2))}"
        elif fmt == '22x': txt = f"v{a}, v{u2(o + 2)}"
        elif fmt == '21t': txt = f"v{a}, -> {pc + s16(u2(o + 2))}"
        elif fmt == '21s': txt = f"v{a}, #{s16(u2(o + 2))}"
        elif fmt == '21h': txt = f"v{a}, #{hex(u2(o + 2))}<<"
        elif fmt == '21c': txt = f"v{a}, {ref(op, u2(o + 2))}"
        elif fmt == '23x': b = u2(o + 2); txt = f"v{a}, v{b & 0xff}, v{b >> 8}"
        elif fmt == '22b':
            b = u2(o + 2); lit = b >> 8; lit = lit - 256 if lit > 127 else lit
            txt = f"v{a}, v{b & 0xff}, #{lit} ({hex(lit & 0xff)})"
        elif fmt == '22t': txt = f"v{a & 0xf}, v{a >> 4}, -> {pc + s16(u2(o + 2))}"
        elif fmt == '22s': txt = f"v{a & 0xf}, v{a >> 4}, #{s16(u2(o + 2))}"
        elif fmt == '22c': txt = f"v{a & 0xf}, v{a >> 4}, {ref(op, u2(o + 2))}"
        elif fmt == '32x': txt = f"v{u2(o + 2)}, v{u2(o + 4)}"
        elif fmt == '31i':
            v = s32(u4(o + 2)); txt = f"v{a}, #{v} ({hex(v & 0xffffffff)})"
        elif fmt == '31c': txt = f"v{a}, {ref(op, u4(o + 2))}"
        elif fmt == '31t':
            tgt = base + 2 * (pc + s32(u4(o + 2)))
            if op == 0x26:
                width, cnt = u2(tgt + 2), u4(tgt + 4)
                vals = [int.from_bytes(d[tgt + 8 + i * width: tgt + 8 + (i + 1) * width], 'little', signed=True) for i in range(cnt)]
                txt = f"v{a}, [{', '.join(hex(x & ((1 << (8 * width)) - 1)) for x in vals)}]"
            elif op == 0x2b:
                cnt, first = u2(tgt + 2), s32(u4(tgt + 4))
                tg = [pc + s32(u4(tgt + 8 + 4 * i)) for i in range(cnt)]
                txt = f"v{a}, " + ', '.join(f"{first + i}->{t}" for i, t in enumerate(tg))
            else:
                cnt = u2(tgt + 2)
                ks = [s32(u4(tgt + 4 + 4 * i)) for i in range(cnt)]
                tg = [pc + s32(u4(tgt + 4 + 4 * cnt + 4 * i)) for i in range(cnt)]
                txt = f"v{a}, " + ', '.join(f"{k}->{t}" for k, t in zip(ks, tg))
        elif fmt in ('35c', '45cc'):
            cnt = a >> 4; g = a & 0xf; idx = u2(o + 2); r = u2(o + 4)
            rs = [r & 0xf, (r >> 4) & 0xf, (r >> 8) & 0xf, r >> 12, g][:cnt]
            txt = f"{{{', '.join('v%d' % x for x in rs)}}}, {ref(op, idx)}"
            if op in INV: name = 'invoke-' + INV[op]
        elif fmt in ('3rc', '4rcc'):
            idx = u2(o + 2); c = u2(o + 4)
            txt = f"{{v{c}..v{c + a - 1}}}, {ref(op, idx)}"
            if op in INV: name = 'invoke-' + INV[op] + '/range'
        elif fmt == '51l':
            v = struct.unpack_from('<q', d, o + 2)[0]; txt = f"v{a}, #{v}"
        print(f"    {pc:4d}: {name} {txt}")
        pc += SIZE[fmt]

for ci in range(hdr['cls_n']):
    co = hdr['cls_o'] + 32 * ci
    cname = types[u4(co)]
    if not cname.startswith(want): continue
    cd = u4(co + 24)
    print(f"\n######## {cname}")
    if not cd: continue
    p = cd
    sf, p = uleb(p); inf, p = uleb(p); dm, p = uleb(p); vm, p = uleb(p)
    idx = 0
    for _ in range(sf):
        di, p = uleb(p); _, p = uleb(p); idx += di; print(f"  static {fields[idx]}")
    idx = 0
    for _ in range(inf):
        di, p = uleb(p); _, p = uleb(p); idx += di; print(f"  field {fields[idx]}")
    for cnt in (dm, vm):
        idx = 0
        for _ in range(cnt):
            di, p = uleb(p); _, p = uleb(p); co2, p = uleb(p); idx += di
            print(f"  == method {methods[idx]}")
            if co2: dis(co2)
