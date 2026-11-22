#!/usr/bin/env python3
"""dis.py exe start_va [n_bytes|'f'] : annotated disassembly (Delphi strings, float constants). 'f' = until first ret after start."""
import sys,struct,subprocess,re,os
exe=sys.argv[1]; d=open(exe,'rb').read()
pe=struct.unpack_from('<I',d,0x3c)[0]; n=struct.unpack_from('<H',d,pe+6)[0]; opt=struct.unpack_from('<H',d,pe+20)[0]; base=struct.unpack_from('<I',d,pe+52)[0]
secs=[]
for i in range(n):
    o=pe+24+opt+40*i; vs,va,rs,ro=struct.unpack_from('<IIII',d,o+8); secs.append((base+va,vs,ro,rs))
def off(va):
    for a,vs,ro,rs in secs:
        if a<=va<a+rs: return va-a+ro
def strat(va):
    o=off(va)
    if o is None or o<12: return None
    # Delphi unicode string: [codepage u16][elemsize u16][ref i32][len i32] data ; ansi: [ref][len] data
    ln=struct.unpack_from('<i',d,o-4)[0]; ref=struct.unpack_from('<i',d,o-8)[0]
    if ref==-1 and 0<ln<400:
        es=struct.unpack_from('<H',d,o-10)[0]
        if es==2:
            try: return 'U"'+d[o:o+2*ln].decode('utf-16le')+'"'
            except: return None
        s=d[o:o+ln]
        if all(32<=c<127 or c in (9,10,13) for c in s): return 'A"'+s.decode()+'"'
    # short string
    ln=d[o]
    if 2<ln<60 and all(32<=c<127 for c in d[o+1:o+1+ln]) and len(d)>o+1+ln: return 'S"'+d[o+1:o+1+ln].decode()+'"'
    return None
def fl(va,kind):
    o=off(va)
    if o is None: return None
    if kind=='DWORD': return 'f32=%g'%struct.unpack_from('<f',d,o)[0]
    if kind=='QWORD': return 'f64=%g'%struct.unpack_from('<d',d,o)[0]
    if kind=='TBYTE':
        m,e=struct.unpack_from('<QH',d,o); s=-1 if e&0x8000 else 1; e&=0x7fff
        return 'f80=%g'%(s*m/2**63*2.0**(e-16383)) if e else 'f80=0'
start=int(sys.argv[2],16); mode=sys.argv[3] if len(sys.argv)>3 else 'f'
nb=6000 if mode=='f' else int(mode,0)
o=off(start); open('/tmp/_sl.bin','wb').write(d[o:o+nb])
out=subprocess.run(['objdump','-D','-b','binary','-m','i386','-M','intel','--adjust-vma=%#x'%start,'/tmp/_sl.bin'],capture_output=True,text=True).stdout
for line in out.splitlines():
    m=re.match(r'\s*([0-9a-f]+):\s+((?:[0-9a-f]{2} )+)\s*(.*)',line)
    if not m: continue
    ins=m.group(3).strip(); ann=[]
    for a in re.findall(r'0x([0-9a-f]{6,8})',ins):
        va=int(a,16)
        if ins.startswith(('call','j')): continue
        k=re.search(r'(DWORD|QWORD|TBYTE) PTR (?:ds:)?0x'+a,ins)
        if k and ins.startswith('f'):
            x=fl(va,k.group(1)); 
            if x: ann.append(x)
        else:
            s=strat(va)
            if s: ann.append(s)
    print(m.group(1),ins,('   ; '+' '.join(ann)) if ann else '')
    if mode=='f' and (ins.startswith('ret')): break
