import sys,struct,os
d=open(sys.argv[1],'rb').read(); base=0x401000-0x400
for t in sys.argv[2:]:
    tv=int(t,16); out=[]
    for o in range(0x400,0x400+0x390000):
        if d[o]==0xE8:
            r=struct.unpack_from('<i',d,o+1)[0]
            if o+base+5+r==tv: out.append(hex(o+base))
    print(t,'callers',out)
