#!/usr/bin/env python3
"""strref.py exe substr... : find ANSI/UTF16/short strings containing substr and code addresses that reference them (imm32)."""
import sys,re,struct
d=open(sys.argv[1],'rb').read()
pe=struct.unpack_from('<I',d,0x3c)[0]; n=struct.unpack_from('<H',d,pe+6)[0]; opt=struct.unpack_from('<H',d,pe+20)[0]; base=struct.unpack_from('<I',d,pe+52)[0]
secs=[]
for i in range(n):
    o=pe+24+opt+40*i; vs,va,rs,ro=struct.unpack_from('<IIII',d,o+8); secs.append((base+va,vs,ro,rs))
def va(o):
    for a,vs,ro,rs in secs:
        if ro<=o<ro+rs: return o-ro+a
t0,tsz,tro,trs=secs[0]
for sub in sys.argv[2:]:
    seen=set()
    for enc in (sub.encode(),sub.encode('utf-16le')):
        for m in re.finditer(re.escape(enc),d,re.I):
            o=m.start()
            # walk back to string start (printable run)
            s=o
            step=2 if len(enc)>len(sub) else 1
            while s-step>=0 and 32<=d[s-step]<127 and (step==1 or d[s-step+1]==0): s-=step
            for cand in (s,s+1):   # s = long string data start, or s = shortstring len byte position+1
                v=va(cand)
                if v is None or v in seen: continue
                refs=[va(x.start()) for x in re.finditer(re.escape(struct.pack('<I',v)),d[tro:tro+trs+0x3000])]
                refs=[hex(tro+0+ (x.start())) for x in []]
                refs=[]
                for x in re.finditer(re.escape(struct.pack('<I',v)),d):
                    r=va(x.start())
                    if r and t0<=r<t0+trs+0x4000: refs.append(hex(r-1))
                if refs:
                    seen.add(v); e=d.find(b'\0\0' if step==2 else b'\0',cand)
                    txt=d[cand:e+ (1 if step==2 else 0)]
                    txt=txt.decode('utf-16le','replace') if step==2 else txt.decode('latin1')
                    print(sub,'| str',hex(v),repr(txt[:70]),'refs',refs[:8])
            # shortstring: len byte before
