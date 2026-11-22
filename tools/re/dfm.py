import struct,sys,os
def rsrc(d):
    pe=struct.unpack_from('<I',d,0x3c)[0]; nsec=struct.unpack_from('<H',d,pe+6)[0]; opt=struct.unpack_from('<H',d,pe+20)[0]
    secs=[]
    for i in range(nsec):
        o=pe+24+opt+40*i; name=d[o:o+8].rstrip(b'\0'); vs,va,rs,ro=struct.unpack_from('<IIII',d,o+8); secs.append((name,va,vs,ro,rs))
    magic=struct.unpack_from('<H',d,pe+24)[0]; dd=pe+24+(96 if magic==0x10b else 112)
    rva=struct.unpack_from('<I',d,dd+8*2)[0]
    def off(r):
        for n,va,vs,ro,rs in secs:
            if va<=r<va+max(vs,rs): return r-va+ro
    base=off(rva); out=[]
    def name(e):
        if e&0x80000000:
            o=base+(e&0x7fffffff); n=struct.unpack_from('<H',d,o)[0]; return d[o+2:o+2+2*n].decode('utf-16le')
        return e
    def walk(o,path):
        nn,ni=struct.unpack_from('<HH',d,o+12)
        for i in range(nn+ni):
            idn,ofs=struct.unpack_from('<II',d,o+16+8*i)
            if ofs&0x80000000: walk(base+(ofs&0x7fffffff),path+[name(idn)])
            else:
                r,sz=struct.unpack_from('<II',d,base+ofs); out.append((path+[name(idn)],d[off(r):off(r)+sz]))
    walk(base,[]); return out,secs
def dfm2txt(b):
    o=4; out=[]
    def sstr():
        nonlocal o; n=b[o]; s=b[o+1:o+1+n].decode('latin1'); o+=1+n; return s
    def val():
        nonlocal o; t=b[o]; o+=1
        if t==0: return None
        if t==1:
            items=[]
            while b[o]!=0: items.append(val())
            o+=1; return items
        if t==2: v=struct.unpack_from('<b',b,o)[0]; o+=1; return v
        if t==3: v=struct.unpack_from('<h',b,o)[0]; o+=2; return v
        if t==4: v=struct.unpack_from('<i',b,o)[0]; o+=4; return v
        if t==5: o+=10; return 'ext'
        if t in (6,7): return sstr()
        if t==8: return False
        if t==9: return True
        if t==10: n=struct.unpack_from('<I',b,o)[0]; o+=4+n; return f'<bin {n}>'
        if t==11:
            s=[]
            while True:
                x=sstr()
                if x=='': break
                s.append(x)
            return s
        if t in (12,20): n=struct.unpack_from('<I',b,o)[0]; s=b[o+4:o+4+n].decode('latin1'); o+=4+n; return s
        if t==13: return 'nil'
        if t==14:
            items=[]
            while b[o]!=0:
                if b[o] in (2,3,4): val()
                o+=1; props={}
                while b[o]!=0: k=sstr(); props[k]=val()
                o+=1; items.append(props)
            o+=1; return items
        if t==15: v=struct.unpack_from('<f',b,o)[0]; o+=4; return v
        if t==16: v=struct.unpack_from('<d',b,o)[0]; o+=8; return v
        if t==18: n=struct.unpack_from('<I',b,o)[0]; s=b[o+4:o+4+2*n].decode('utf-16le'); o+=4+2*n; return s
        if t==19: v=struct.unpack_from('<q',b,o)[0]; o+=8; return v
        raise ValueError(f'type {t} at {o}')
    def obj(ind):
        nonlocal o
        if b[o]&0xf0==0xf0:
            fl=b[o]; o+=1
            if fl&2: val()
        cls=sstr(); nm=sstr(); out.append('  '*ind+f'object {nm}: {cls}')
        while b[o]!=0:
            k=sstr(); v=val(); out.append('  '*ind+f'  {k} = {v!r}'[:200])
        o+=1
        while b[o]!=0: obj(ind+1)
        o+=1
    obj(0); return '\n'.join(out)
if __name__=='__main__':
    d=open(sys.argv[1],'rb').read(); res,secs=rsrc(d)
    for path,data in res:
        if data[:4]==b'TPF0':
            try: t=dfm2txt(data)
            except Exception as e: t='ERR '+str(e)
            open(f'{sys.argv[2]}/{path[1]}.txt','w').write(t); print(path[1],len(data),len(t.splitlines()))
