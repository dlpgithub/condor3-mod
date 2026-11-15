import sys,os,math,numpy as np
sys.path.insert(0,os.path.expanduser('~/mnt/condor3-mod/tools'))
import c3d,apt
L=os.path.expanduser('~/mnt/Condor3/Landscapes/Slovenia3')
from collections import defaultdict
rows=defaultdict(list)
for a in apt.unpack(open(L+'/Slovenia3.apt','rb').read()):
    g=c3d.parse(open(f"{L}/Airports/{a['name']}G.c3d",'rb').read())
    for ob in g['objects']:
        v=g['vertices'][ob['first_vertex']:ob['first_vertex']+ob['vertex_count']].astype(float)
        A=np.c_[v[:,0],v[:,1],np.ones(len(v))]
        cu,ru=np.linalg.lstsq(A,v[:,6],rcond=None)[:2]; cv,rv=np.linalg.lstsq(A,v[:,7],rcond=None)[:2]
        eu=np.abs(A@cu-v[:,6]).max(); ev=np.abs(A@cv-v[:,7]).max()
        angU=math.degrees(math.atan2(cu[1],cu[0])); angV=math.degrees(math.atan2(cv[1],cv[0]))
        mpu=1/np.hypot(cu[0],cu[1]) if np.hypot(cu[0],cu[1])>1e-9 else float('inf'); mpv=1/np.hypot(cv[0],cv[1]) if np.hypot(cv[0],cv[1])>1e-9 else float('inf')
        rows[ob['name'].lower()].append((a['name'][:10],a['direction'],angU,angV,mpu,mpv,max(eu,ev),ob['texture'][-28:],tuple(round(c,2) for c in ob['color']),ob['params']))
for k,r in rows.items():
    print('==',k,len(r))
    for x in r[:int(sys.argv[1])]: print('  %-10s dir %6.1f gradU %7.1f gradV %7.1f  m/U %8.1f m/V %8.1f err %.4f %s %s %s'%x)
