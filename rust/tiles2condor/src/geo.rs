//! WGS84 geodesy: ECEF -> geodetic, UTM forward / inverse (Krueger series). Same formulas as tools/tiles2condor.py.
pub const WGS_A: f64 = 6378137.0;
const WGS_F: f64 = 1.0 / 298.257223563;
pub const WGS_E2: f64 = WGS_F * (2.0 - WGS_F);
const K0: f64 = 0.9996;

struct K { a: f64, al: [f64; 3], be: [f64; 3], e: f64 }
fn k() -> K {
    let n = WGS_F / (2.0 - WGS_F);
    K {
        a: WGS_A / (1.0 + n) * (1.0 + n * n / 4.0 + n.powi(4) / 64.0),
        al: [n / 2.0 - 2.0 * n * n / 3.0 + 5.0 * n.powi(3) / 16.0, 13.0 * n * n / 48.0 - 3.0 * n.powi(3) / 5.0, 61.0 * n.powi(3) / 240.0],
        be: [n / 2.0 - 2.0 * n * n / 3.0 + 37.0 * n.powi(3) / 96.0, n * n / 48.0 + n.powi(3) / 15.0, 17.0 * n.powi(3) / 480.0],
        e: WGS_E2.sqrt(),
    }
}

/// (latitude deg, longitude deg, ellipsoidal height m)
pub fn ecef_to_lla(p: [f64; 3]) -> (f64, f64, f64) {
    let (x, y, z) = (p[0], p[1], p[2]);
    let lon = y.atan2(x);
    let r = x.hypot(y);
    let mut lat = z.atan2(r * (1.0 - WGS_E2));
    let mut h = 0.0;
    for _ in 0..5 {
        let n = WGS_A / (1.0 - WGS_E2 * lat.sin().powi(2)).sqrt();
        h = r / lat.cos() - n;
        lat = z.atan2(r * (1.0 - WGS_E2 * n / (n + h)));
    }
    (lat.to_degrees(), lon.to_degrees(), h)
}

pub fn utm_forward(lat: f64, lon: f64, zone: i32, south: bool) -> (f64, f64) {
    let c = k();
    let lam = (lon - ((zone - 1) as f64 * 6.0 - 180.0 + 3.0)).to_radians();
    let phi = lat.to_radians();
    let t = (phi.sin().atanh() - c.e * (c.e * phi.sin()).atanh()).sinh();
    let xi = t.atan2(lam.cos());
    let eta = (lam.sin() / (1.0 + t * t).sqrt()).atanh();
    let mut e = eta;
    let mut n = xi;
    for j in 0..3 {
        let f = 2.0 * (j as f64 + 1.0);
        e += c.al[j] * (f * xi).cos() * (f * eta).sinh();
        n += c.al[j] * (f * xi).sin() * (f * eta).cosh();
    }
    (500000.0 + K0 * c.a * e, K0 * c.a * n + if south { 1e7 } else { 0.0 })
}

pub fn utm_inverse(e: f64, n: f64, zone: i32, south: bool) -> (f64, f64) {
    let c = k();
    let xi = (n - if south { 1e7 } else { 0.0 }) / (K0 * c.a);
    let eta = (e - 500000.0) / (K0 * c.a);
    let mut xp = xi;
    let mut ep = eta;
    for j in 0..3 {
        let f = 2.0 * (j as f64 + 1.0);
        xp -= c.be[j] * (f * xi).sin() * (f * eta).cosh();
        ep -= c.be[j] * (f * xi).cos() * (f * eta).sinh();
    }
    let chi = (xp.sin() / ep.cosh()).asin();
    let mut phi = chi;
    for _ in 0..6 {
        phi = (chi.sin().atanh() + c.e * (c.e * phi.sin()).atanh()).tanh().asin();
    }
    (phi.to_degrees(), ep.sinh().atan2(xp.cos()).to_degrees() + ((zone - 1) as f64 * 6.0 - 180.0 + 3.0))
}
