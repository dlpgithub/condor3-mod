//! Background writer: output files are handed to one thread that writes them (in order) while the next tile is computed.
//! Slow disks then cost max(compute, write) instead of compute + write. Jobs run in submission order; `flush` waits for all of them
//! (main calls it at the end of every stage, so later stages always read complete files). At most MAX_PENDING bytes are queued.
use std::collections::HashMap;
use std::sync::mpsc::{sync_channel, SyncSender};
use std::sync::{Arc, Condvar, Mutex, OnceLock};

type Job = Box<dyn FnOnce() + Send>;
struct W { tx: Mutex<Option<SyncSender<(Job, usize)>>>, pending: Mutex<(usize, usize)>, cv: Condvar }   // (bytes, jobs)
static WR: OnceLock<W> = OnceLock::new();
const MAX_PENDING: usize = 1 << 30;

fn w() -> &'static W {
    WR.get_or_init(|| {
        let (tx, rx) = sync_channel::<(Job, usize)>(4096);
        std::thread::spawn(move || {
            for (job, n) in rx {
                crate::prof("bg.writing", job);
                let w = WR.get().unwrap();
                let mut p = w.pending.lock().unwrap(); p.0 -= n; p.1 -= 1; w.cv.notify_all();
            }
        });
        W { tx: Mutex::new(Some(tx)), pending: Mutex::new((0, 0)), cv: Condvar::new() }
    })
}

/// run a job on the writer thread; `bytes` = memory it holds (for the queue limit)
pub fn run(bytes: usize, job: impl FnOnce() + Send + 'static) {
    let w = w();
    crate::prof("bg.queue-full", || {
        let mut p = w.pending.lock().unwrap();
        while p.1 > 0 && p.0 + bytes > MAX_PENDING { p = w.cv.wait(p).unwrap(); }
        p.0 += bytes; p.1 += 1;
    });
    w.tx.lock().unwrap().as_ref().unwrap().send((Box::new(job), bytes)).expect("writer thread is gone");
}

/// files queued but not yet written; `read` / `exists` see them, so later stages need not wait for the disk
static PEND: Mutex<Option<HashMap<String, Arc<Vec<u8>>>>> = Mutex::new(None);

/// write a file (via .tmp + rename) in the background
pub fn write(path: String, data: Vec<u8>) {
    let (n, a) = (data.len(), Arc::new(data));
    PEND.lock().unwrap().get_or_insert_with(HashMap::new).insert(path.clone(), a.clone());
    run(n, move || {
        crate::fmt::write_atomic(&path, &a);
        let mut m = PEND.lock().unwrap();
        if let Some(m) = m.as_mut() { if m.get(&path).is_some_and(|x| Arc::ptr_eq(x, &a)) { m.remove(&path); } }
    });
}
/// contents of a file, also if it is still queued
pub fn read(path: &str) -> Option<Vec<u8>> {
    if let Some(a) = PEND.lock().unwrap().as_ref().and_then(|m| m.get(path).cloned()) { return Some((*a).clone()); }
    std::fs::read(path).ok()
}
/// size of a file, also if it is still queued
pub fn len(path: &str) -> Option<usize> {
    if let Some(n) = PEND.lock().unwrap().as_ref().and_then(|m| m.get(path).map(|a| a.len())) { return Some(n); }
    std::fs::metadata(path).ok().map(|m| m.len() as usize)
}
pub fn exists(path: &str) -> bool { PEND.lock().unwrap().as_ref().is_some_and(|m| m.contains_key(path)) || std::path::Path::new(path).exists() }

/// CPU jobs on the rayon pool (e.g. compressing an atlas page, then writing it), at most MAX_CPU queued
static CPU: (Mutex<usize>, Condvar) = (Mutex::new(0), Condvar::new());
const MAX_CPU: usize = 12;
pub fn cpu(job: impl FnOnce() + Send + 'static) {
    { let mut n = CPU.0.lock().unwrap(); while *n >= MAX_CPU { n = CPU.1.wait(n).unwrap(); } *n += 1; }
    rayon::spawn(move || { job(); let mut n = CPU.0.lock().unwrap(); *n -= 1; CPU.1.notify_all(); });
}

/// wait for the background CPU jobs only
pub fn wait_cpu() { crate::prof("bg.wait-cpu", || { let mut n = CPU.0.lock().unwrap(); while *n > 0 { n = CPU.1.wait(n).unwrap(); } }); }

/// wait until all background CPU jobs are done and everything queued so far is written
pub fn flush() {
    crate::prof("bg.flush-cpu", || { let mut n = CPU.0.lock().unwrap(); while *n > 0 { n = CPU.1.wait(n).unwrap(); } });
    let Some(w) = WR.get() else { return };
    crate::prof("bg.flush-write", || { let mut p = w.pending.lock().unwrap(); while p.1 > 0 { p = w.cv.wait(p).unwrap(); } });
}
