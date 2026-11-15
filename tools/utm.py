"""Minimal WGS84 UTM forward/inverse (Kruger series), no deps."""
import math
A=6378137.0; F=1/298.257223563; K0=0.9996
_n=F/(2-F)
_A=A/(1+_n)*(1+_n**2/4+_n**4/64)
_al=[_n/2-2*_n**2/3+5*_n**3/16, 13*_n**2/48-3*_n**3/5, 61*_n**3/240]
_be=[_n/2-2*_n**2/3+37*_n**3/96, _n**2/48+_n**3/15, 17*_n**3/480]
_e=math.sqrt(F*(2-F))
def forward(lat,lon,zone):
    lam=math.radians(lon-((zone-1)*6-180+3)); phi=math.radians(lat)
    t=math.sinh(math.atanh(math.sin(phi))-_e*math.atanh(_e*math.sin(phi)))
    xi=math.atan2(t,math.cos(lam)); eta=math.atanh(math.sin(lam)/math.sqrt(1+t*t))
    E=K0*_A*(eta+sum(a*math.cos(2*(j+1)*xi)*math.sinh(2*(j+1)*eta) for j,a in enumerate(_al)))
    N=K0*_A*(xi+sum(a*math.sin(2*(j+1)*xi)*math.cosh(2*(j+1)*eta) for j,a in enumerate(_al)))
    return 500000+E, N
def inverse(E,N,zone):
    E-=500000
    xi=N/(K0*_A); eta=E/(K0*_A)
    xp=xi-sum(b*math.sin(2*(j+1)*xi)*math.cosh(2*(j+1)*eta) for j,b in enumerate(_be))
    ep=eta-sum(b*math.cos(2*(j+1)*xi)*math.sinh(2*(j+1)*eta) for j,b in enumerate(_be))
    chi=math.asin(math.sin(xp)/math.cosh(ep))
    # iterate geodetic lat
    phi=chi
    for _ in range(6):
        phi=math.asin(math.tanh(math.atanh(math.sin(chi))+_e*math.atanh(_e*math.sin(phi))))
    lam=math.atan2(math.sinh(ep),math.cos(xp))
    return math.degrees(phi), math.degrees(lam)+((zone-1)*6-180+3)
