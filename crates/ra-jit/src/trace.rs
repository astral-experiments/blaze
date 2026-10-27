//! Where the time of a build goes: spans of the client, the daemon and the worker on one
//! clock, always recorded. The worker sends its own with its answer, the daemon adds its
//! own, and the client puts them together. `RA_JIT_STATS` prints a line and a table that
//! adds up to the client's wall time; `RA_JIT_TRACE=<path>` writes them as a Chrome trace
//! (ui.perfetto.dev opens it).
//!
//! A span with a `level` is a step of the build: one level is inside the one above it
//! (client 0, daemon 1, the daemon's question to the worker 2, the worker 3), and the table
//! gives each moment to the deepest step under way. One without is detail for the trace:
//! a rust-analyzer query or a step of code generation, from [`install`].
//! A direct worker with `BLAZE_TRACE=<path>` writes its first build before it starts
//! watching. `BLAZE_TRACE_CONST=crate::module::NAME` adds requests for that module
//! constant and executions of its Salsa query, including ones that take under 200 µs.

use std::{
    cell::Cell,
    fmt::Write as _,
    path::{Path, PathBuf},
    sync::{
        Mutex, OnceLock,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::{Instant, SystemTime, UNIX_EPOCH},
};

use serde_derive::{Deserialize, Serialize};

/// One span: what it was, the process and thread, when it began (microseconds since the
/// Unix epoch) and how long it took.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub(crate) struct Span {
    #[serde(rename = "n")]
    pub(crate) name: String,
    #[serde(rename = "p")]
    pub(crate) pid: u32,
    #[serde(rename = "t")]
    pub(crate) tid: u64,
    #[serde(rename = "h", default, skip_serializing_if = "String::is_empty")]
    pub(crate) thread: String,
    #[serde(rename = "s")]
    pub(crate) start_us: u64,
    #[serde(rename = "d")]
    pub(crate) dur_us: u64,
    #[serde(rename = "l", default, skip_serializing_if = "Option::is_none")]
    pub(crate) level: Option<u8>,
}

pub(crate) const CLIENT: u8 = 0;
pub(crate) const DAEMON: u8 = 1;
pub(crate) const ASKED: u8 = 2;
pub(crate) const WORKER: u8 = 3;

/// Microseconds since the Unix epoch of `at`: the wall clock now, less how long ago it was.
pub(crate) fn epoch_us(at: Instant) -> u64 {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    now.saturating_sub(at.elapsed()).as_micros() as u64
}

/// When this process began, as near as it can tell: the first call, which `main` makes.
pub(crate) fn began() -> Instant {
    static BEGAN: OnceLock<Instant> = OnceLock::new();
    *BEGAN.get_or_init(Instant::now)
}

thread_local! {
    static TID: Cell<u64> = const { Cell::new(0) };
}

/// A number for this thread, small and the same for as long as it lives.
fn tid() -> u64 {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    TID.with(|it| {
        if it.get() == 0 {
            it.set(NEXT.fetch_add(1, Ordering::Relaxed));
        }
        it.get()
    })
}

/// The span from `start` to `end` on this thread.
pub(crate) fn span(
    name: impl Into<String>,
    level: Option<u8>,
    start: Instant,
    end: Instant,
) -> Span {
    let thread = std::thread::current().name().unwrap_or_default().to_owned();
    Span {
        name: name.into(),
        pid: std::process::id(),
        tid: tid(),
        thread,
        start_us: epoch_us(start),
        dur_us: end.saturating_duration_since(start).as_micros() as u64,
        level,
    }
}

/// The spans this process noted for the build it is in: the client's steps.
static NOTED: Mutex<Vec<Span>> = Mutex::new(Vec::new());

fn noted() -> std::sync::MutexGuard<'static, Vec<Span>> {
    NOTED.lock().unwrap_or_else(|it| it.into_inner())
}

/// Notes a step from `start` to now.
pub(crate) fn note(name: &str, level: u8, start: Instant) {
    noted().push(span(name, Some(level), start, Instant::now()));
}

pub(crate) fn note_all(spans: impl IntoIterator<Item = Span>) {
    noted().extend(spans);
}

/// What the answer line of a worker has after the spans it sends.
const SPANS: &str = " spans=";

/// The answer `line` with `spans` after it, for the daemon to hand on.
pub(crate) fn with_spans(line: String, spans: &[Span]) -> String {
    match serde_json::to_string(spans) {
        Ok(json) => format!("{line}{SPANS}{json}"),
        Err(_) => line,
    }
}

/// A worker's answer and the spans it sent.
pub(crate) fn split(line: &str) -> (&str, Vec<Span>) {
    match line.find(SPANS) {
        Some(at) => {
            let spans = serde_json::from_str(&line[at + SPANS.len()..]).unwrap_or_default();
            (&line[..at], spans)
        }
        None => (line, Vec::new()),
    }
}

/// The answer without its spans, for a person or an editor.
pub(crate) fn without_spans(line: &str) -> &str {
    line.find(SPANS).map_or(line, |at| &line[..at])
}

/// When the worker last read a request.
static ARRIVED: Mutex<Option<Instant>> = Mutex::new(None);

pub(crate) fn arrived() {
    *ARRIVED.lock().unwrap_or_else(|it| it.into_inner()) = Some(Instant::now());
}

pub(crate) fn last_arrival() -> Option<Instant> {
    *ARRIVED.lock().unwrap_or_else(|it| it.into_inner())
}

/// Detail spans shorter than this are not kept.
const SHORTEST_US: u64 = 200;
/// Nor more than this many between two builds.
const MOST: usize = 4000;

static DETAIL: Mutex<Vec<Span>> = Mutex::new(Vec::new());
static DROPPED: AtomicUsize = AtomicUsize::new(0);

struct Cold {
    path: PathBuf,
    selected: Option<String>,
    active: AtomicBool,
    batch: AtomicUsize,
    events: Mutex<ColdEvents>,
}

#[derive(Default)]
struct ColdEvents {
    // Keep parallel scopes, requests, and executions independently of the ordinary detail cap.
    spans: [Vec<Span>; 3],
    dropped: [usize; 3],
}

enum ColdEvent {
    Parallel,
    Request,
    Execution,
}

static COLD: OnceLock<Cold> = OnceLock::new();

/// Enables first-build tracing for a worker that was started directly.
pub(crate) fn start_cold() {
    let Some(path) = std::env::var_os("RA_JIT_TRACE").filter(|it| !it.is_empty()) else { return };
    let selected = std::env::var("RA_JIT_TRACE_CONST").ok().filter(|it| !it.is_empty());
    let has_filter = selected.is_some();
    _ = COLD.set(Cold {
        path: path.into(),
        selected,
        active: AtomicBool::new(true),
        batch: AtomicUsize::new(1),
        events: Mutex::default(),
    });
    if has_filter {
        hir_ty::consteval::trace_consts_with(selected_const);
    }
}

fn cold() -> Option<&'static Cold> {
    COLD.get().filter(|it| it.active.load(Ordering::Relaxed))
}

impl Cold {
    fn record(&self, kind: ColdEvent, start: Instant, name: impl FnOnce() -> String) {
        let end = Instant::now();
        let mut events = self.events.lock().unwrap_or_else(|it| it.into_inner());
        let index = kind as usize;
        if events.spans[index].len() < MOST {
            events.spans[index].push(span(name(), None, start, end));
        } else {
            events.dropped[index] += 1;
        }
    }
}

/// The number of a parallel dispatch and its item count, shared by all of its lanes.
pub(crate) fn cold_batch(count: usize) -> Option<(usize, usize)> {
    Some((cold()?.batch.fetch_add(1, Ordering::Relaxed), count))
}

/// A first-build phase. Each batch gets one interval per participating thread.
pub(crate) fn cold_span(name: &'static str, batch: Option<(usize, usize)>) -> Option<impl Drop> {
    struct Phase(&'static Cold, &'static str, usize, usize, Instant);
    impl Drop for Phase {
        fn drop(&mut self) {
            let Self(cold, name, id, count, start) = *self;
            cold.record(ColdEvent::Parallel, start, || {
                format!("parallel {id} {name} ({count} items)")
            });
        }
    }
    let (id, count) = batch?;
    Some(Phase(cold()?, name, id, count, Instant::now()))
}

/// Times the linker subprocess itself, including each failed attempt in a retry.
pub(crate) fn link_output(
    command: &mut std::process::Command,
    name: &str,
) -> std::io::Result<std::process::Output> {
    let start = cold().map(|_| Instant::now());
    let output = command.output();
    if let Some(start) = start {
        let end = Instant::now();
        let name = match &output {
            Ok(it) if it.status.success() => name.to_owned(),
            Ok(it) => format!("{name} ({})", it.status),
            Err(e) => format!("{name} ({e})"),
        };
        noted().push(span(name, Some(1), start, end));
    }
    output
}

/// Matches the declared crate and every named module. Associated and block constants
/// have different owners, even when their leaf names are identical.
fn selected_const(
    db: &dyn hir_ty::db::HirDatabase,
    def: hir_def::ConstId,
    executing: bool,
) -> Option<fn(Instant)> {
    use hir_def::{HasModule, ItemContainerId, Lookup, signatures::ConstSignature};
    let filter = cold()?.selected.as_deref()?;
    let mut path = filter.rsplit("::");
    if ConstSignature::of(db, def).name.as_ref()?.as_str() != path.next()? {
        return None;
    }
    let ItemContainerId::ModuleId(mut module) = def.lookup(db).container else { return None };
    while let Some(parent) = module.containing_module(db) {
        // A block module has no name. A constant inside a block cannot match a module item.
        if module.name(db)?.as_str() != path.next()? {
            return None;
        }
        module = parent;
    }
    let krate = def.krate(db).extra_data(db).display_name.as_ref()?.crate_name();
    if krate.as_str() != path.next()? || path.next().is_some() {
        return None;
    }
    Some(if executing {
        |start| record_const(ColdEvent::Execution, "eval", start)
    } else {
        |start| record_const(ColdEvent::Request, "request", start)
    })
}

fn record_const(kind: ColdEvent, name: &str, start: Instant) {
    if let Some(cold) = cold()
        && let Some(selected) = &cold.selected
    {
        cold.record(kind, start, || format!("const {name} {selected}"));
    }
}

/// Writes once, before a direct worker starts watching or when its initial build returns.
pub(crate) fn finish_cold(outcome: &str) {
    let Some(cold) = COLD.get() else { return };
    if !cold.active.swap(false, Ordering::Relaxed) {
        return;
    }
    let root = span("worker startup", Some(CLIENT), began(), Instant::now());
    let (start, end) = (root.start_us, root.start_us + root.dur_us);
    let mut spans = vec![root];
    spans.append(&mut *noted());
    let detail_dropped = DROPPED.load(Ordering::Relaxed);
    spans.extend(take_detail(began()));
    let events = std::mem::take(&mut *cold.events.lock().unwrap_or_else(|it| it.into_inner()));
    spans.extend(events.spans.into_iter().flatten());
    let [parallel, requests, executions] = events.dropped;
    let info = serde_json::json!({"outcome":outcome, "constFilter":cold.selected, "dropped":{
        "detail":detail_dropped, "parallel":parallel, "constRequests":requests, "constExecutions":executions}});
    if let Err(why) = write_trace(&cold.path, &spans, start, end, Some(info)) {
        eprintln!("ra-jit: the trace is not written to {}: {why:#}", cold.path.display());
    }
}

/// The detail spans that ended since `since`, taken; older ones are dropped.
pub(crate) fn take_detail(since: Instant) -> Vec<Span> {
    let since = epoch_us(since);
    let mut taken = std::mem::take(&mut *DETAIL.lock().unwrap_or_else(|it| it.into_inner()));
    DROPPED.store(0, Ordering::Relaxed);
    taken.retain(|it| it.start_us + it.dur_us >= since);
    taken
}

/// Records the spans of rust-analyzer's queries (the `*_query` spans of the queries that
/// ran) and of code generation (target `ra_jit`), each as long as [`SHORTEST_US`] or longer.
/// Every other callsite is told it is never wanted, so it costs what a disabled one does.
pub(crate) fn install() {
    _ = tracing::subscriber::set_global_default(Recorder);
}

/// The callsites of the spans [`Recorder`] keeps, by the number in their span ids.
static CALLSITES: Mutex<Vec<&'static tracing::Metadata<'static>>> = Mutex::new(Vec::new());

struct Recorder;

/// A span id holds its callsite in the low byte and its start, in nanoseconds since the
/// process began, above it: nothing is kept per open span.
const SITE_BITS: u32 = 8;

impl Recorder {
    fn wanted(meta: &tracing::Metadata<'_>) -> bool {
        meta.is_span()
            && *meta.level() <= tracing::Level::INFO
            && (meta.target().starts_with("ra_jit") || meta.name().ends_with("_query"))
    }

    fn site(meta: &'static tracing::Metadata<'static>) -> Option<u64> {
        let mut sites = CALLSITES.lock().unwrap_or_else(|it| it.into_inner());
        let found = sites.iter().position(|it| std::ptr::eq(*it, meta));
        let at = match found {
            Some(at) => at,
            None if sites.len() < (1 << SITE_BITS) - 1 => {
                sites.push(meta);
                sites.len() - 1
            }
            None => return None,
        };
        Some(at as u64 + 1)
    }
}

impl tracing::Subscriber for Recorder {
    fn register_callsite(
        &self,
        meta: &'static tracing::Metadata<'static>,
    ) -> tracing::subscriber::Interest {
        if Self::wanted(meta) && Self::site(meta).is_some() {
            tracing::subscriber::Interest::always()
        } else {
            tracing::subscriber::Interest::never()
        }
    }

    fn max_level_hint(&self) -> Option<tracing::level_filters::LevelFilter> {
        Some(tracing::level_filters::LevelFilter::INFO)
    }

    fn enabled(&self, meta: &tracing::Metadata<'_>) -> bool {
        Self::wanted(meta)
    }

    fn new_span(&self, span: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        let site = Self::site(span.metadata()).unwrap_or(0);
        let nanos = began().elapsed().as_nanos() as u64;
        tracing::span::Id::from_u64((nanos << SITE_BITS | site).max(1))
    }

    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}

    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}

    fn event(&self, _: &tracing::Event<'_>) {}

    fn enter(&self, _: &tracing::span::Id) {}

    fn exit(&self, _: &tracing::span::Id) {}

    fn try_close(&self, id: tracing::span::Id) -> bool {
        let id = id.into_u64();
        let site = (id & ((1 << SITE_BITS) - 1)) as usize;
        let start = began() + std::time::Duration::from_nanos(id >> SITE_BITS);
        let end = Instant::now();
        if site == 0 || end.saturating_duration_since(start).as_micros() < SHORTEST_US as u128 {
            return true;
        }
        let Some(meta) =
            CALLSITES.lock().unwrap_or_else(|it| it.into_inner()).get(site - 1).copied()
        else {
            return true;
        };
        let mut detail = DETAIL.lock().unwrap_or_else(|it| it.into_inner());
        if detail.len() < MOST {
            detail.push(span(meta.name(), None, start, end));
        } else {
            DROPPED.fetch_add(1, Ordering::Relaxed);
        }
        true
    }
}

/// Where the time from `start_us` to `end_us` went: each moment is the deepest step's under
/// way then, and a moment no step has is unaccounted. In the order the steps first had
/// time; the rows add up to the whole.
pub(crate) fn breakdown(spans: &[Span], start_us: u64, end_us: u64) -> Vec<(String, u64)> {
    let steps: Vec<&Span> = spans.iter().filter(|it| it.level.is_some()).collect();
    let mut cuts: Vec<u64> = vec![start_us, end_us];
    for it in &steps {
        cuts.push(it.start_us.clamp(start_us, end_us));
        cuts.push((it.start_us + it.dur_us).clamp(start_us, end_us));
    }
    cuts.sort_unstable();
    cuts.dedup();
    let mut rows: Vec<(String, u64)> = Vec::new();
    for pair in cuts.windows(2) {
        let (from, to) = (pair[0], pair[1]);
        let covers = |it: &&&Span| it.start_us <= from && it.start_us + it.dur_us >= to;
        // The deepest level that is inside one of each level above it; at a level, the step
        // that began last.
        let mut owner: Option<&Span> = None;
        for level in 0..=u8::MAX {
            let here = steps.iter().filter(|it| it.level == Some(level)).filter(covers);
            match here.max_by_key(|it| it.start_us) {
                Some(found) => owner = Some(found),
                None => break,
            }
        }
        let name = owner.map_or("unaccounted", |it| &it.name);
        match rows.iter_mut().find(|it| it.0 == name) {
            Some(row) => row.1 += to - from,
            None => rows.push((name.to_owned(), to - from)),
        }
    }
    rows
}

/// What the client says at the end of a build the daemon made, with all the spans of it:
/// a line and a table with `RA_JIT_STATS`, a Chrome trace with `RA_JIT_TRACE`.
pub(crate) fn finish() {
    let stats = std::env::var_os("RA_JIT_STATS").is_some();
    let trace = std::env::var_os("RA_JIT_TRACE").filter(|it| !it.is_empty());
    if !stats && trace.is_none() {
        return;
    }
    let spans = std::mem::take(&mut *noted());
    let (start, end) = (epoch_us(began()), epoch_us(Instant::now()));
    if stats {
        eprint!("{}", table(&spans, start, end));
    }
    if let Some(trace) = trace {
        let written = write_trace(Path::new(&trace), &spans, start, end, None);
        if let Err(why) = written {
            eprintln!(
                "ra-jit: the trace is not written to {}: {why:#}",
                Path::new(&trace).display()
            );
        }
    }
}

/// `ra-jit time: 402.1 ms: check 285.5 (71%), ...` and the table of [`breakdown`].
pub(crate) fn table(spans: &[Span], start: u64, end: u64) -> String {
    let rows = breakdown(spans, start, end);
    let total = (end - start).max(1);
    let ms = |us: u64| us as f64 / 1000.0;
    let pct = |us: u64| us as f64 * 100.0 / total as f64;
    let mut top: Vec<&(String, u64)> = rows.iter().collect();
    top.sort_by_key(|it| std::cmp::Reverse(it.1));
    let top: Vec<String> = top
        .iter()
        .take(6)
        .map(|(name, us)| format!("{name} {:.1} ({:.0}%)", ms(*us), pct(*us)))
        .collect();
    let mut out = format!("ra-jit time: {:.1} ms: {}\n", ms(total), top.join(", "));
    out.push_str("ra-jit where the time went:\n");
    for (name, us) in &rows {
        let _ = writeln!(out, "ra-jit   {name:<28} {:>9.1} ms {:>5.1}%", ms(*us), pct(*us));
    }
    let _ = writeln!(out, "ra-jit   {:<28} {:>9.1} ms {:>5.1}%", "total", ms(total), 100.0);
    out
}

/// Writes `spans` as a Chrome trace: to `path` when it ends in `.json`, else to a file of
/// its own in the directory `path`.
fn write_trace(
    path: &Path,
    spans: &[Span],
    start: u64,
    end: u64,
    cold: Option<serde_json::Value>,
) -> anyhow::Result<()> {
    let path = if path.extension().is_some_and(|it| it == "json") {
        path.to_owned()
    } else {
        std::fs::create_dir_all(path)?;
        let now = epoch_us(Instant::now());
        path.join(format!("ra-jit-{now}-{}.json", std::process::id()))
    };
    std::fs::write(&path, chrome(spans, start, end, cold))?;
    eprintln!("ra-jit: trace written to {}", path.display());
    Ok(())
}

/// The Chrome trace event format: complete events, with names for the processes and
/// threads. Key `raJit` has the client's wall time and [`breakdown`] of it, for a harness.
fn chrome(spans: &[Span], start: u64, end: u64, cold: Option<serde_json::Value>) -> String {
    use serde_json::json;
    let mut events = Vec::new();
    let mut named = rustc_hash::FxHashSet::default();
    for it in spans {
        let process = match it.level {
            Some(_) if cold.is_some() => Some("worker"),
            Some(CLIENT) => Some("client"),
            Some(DAEMON | ASKED) => Some("daemon"),
            Some(_) => Some("worker"),
            None => None,
        };
        if let Some(process) = process
            && named.insert((it.pid, 0))
        {
            events.push(json!({"ph": "M", "name": "process_name", "pid": it.pid, "tid": 0,
                "args": {"name": format!("ra-jit {process} ({})", it.pid)}}));
        }
        if named.insert((it.pid, it.tid)) {
            let thread =
                if it.thread.is_empty() { format!("thread {}", it.tid) } else { it.thread.clone() };
            events.push(json!({"ph": "M", "name": "thread_name", "pid": it.pid, "tid": it.tid,
                "args": {"name": thread}}));
        }
        let cat = if it.level.is_some() { "step" } else { "detail" };
        events.push(json!({"ph": "X", "name": it.name, "cat": cat, "pid": it.pid, "tid": it.tid,
            "ts": it.start_us, "dur": it.dur_us}));
    }
    let rows = breakdown(spans, start, end);
    let breakdown: Vec<_> = rows.iter().map(|(name, us)| json!([name, us])).collect();
    let mut ours = json!({"start_us": start, "total_us": end - start, "breakdown": breakdown});
    if let Some(cold) = cold {
        ours["startup"] = cold;
    }
    json!({"traceEvents": events, "displayTimeUnit": "ms", "raJit": ours}).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(name: &str, level: u8, start_us: u64, dur_us: u64) -> Span {
        Span {
            name: name.to_owned(),
            pid: 1,
            tid: 1,
            thread: String::new(),
            start_us,
            dur_us,
            level: Some(level),
        }
    }

    #[test]
    fn the_rows_add_up_to_the_whole() {
        let spans = [
            at("client start", CLIENT, 0, 10),
            at("client↔daemon", CLIENT, 10, 80),
            at("daemon", DAEMON, 15, 70),
            at("daemon↔worker", ASKED, 20, 60),
            at("check", WORKER, 25, 40),
            // A worker step outside of any question of the daemon's is not the build's.
            at("stray", WORKER, 92, 5),
            at("client write state", CLIENT, 95, 3),
        ];
        let rows = breakdown(&spans, 0, 100);
        let want = [
            ("client start", 10),
            ("client↔daemon", 10),
            ("daemon", 10),
            ("daemon↔worker", 20),
            ("check", 40),
            ("unaccounted", 7),
            ("client write state", 3),
        ];
        assert_eq!(rows, want.map(|(n, us)| (n.to_owned(), us)));
        assert_eq!(rows.iter().map(|it| it.1).sum::<u64>(), 100);
        let joined = with_spans("ok patch=x ms=3".to_owned(), &spans);
        let (line, back) = split(&joined);
        assert_eq!((line, back.as_slice()), ("ok patch=x ms=3", &spans[..]));
    }

    #[test]
    fn a_direct_trace_labels_the_worker_and_keeps_overlapping_detail_out_of_its_total() {
        use serde_json::json;
        let mut query = at("const eval lib::TABLE", CLIENT, 20, 60);
        query.level = None;
        let spans = [at("worker startup", CLIENT, 0, 100), query, at("link", 1, 80, 20)];
        let trace: serde_json::Value =
            serde_json::from_str(&chrome(&spans, 0, 100, Some(json!({"outcome":"watch ready"}))))
                .unwrap();
        assert_eq!(trace["raJit"]["breakdown"], json!([["worker startup", 80], ["link", 20]]));
        assert_eq!(trace["raJit"]["startup"]["outcome"], "watch ready");
        assert_eq!(trace["traceEvents"][0]["args"]["name"], "ra-jit worker (1)");
    }

    #[test]
    fn a_full_request_buffer_still_records_actual_const_evaluations() {
        let cold = Cold {
            path: PathBuf::new(),
            selected: None,
            active: AtomicBool::new(true),
            batch: AtomicUsize::new(1),
            events: Mutex::default(),
        };
        let req = ColdEvent::Request as usize;
        cold.events.lock().unwrap().spans[req].resize(MOST, at("const request", CLIENT, 0, 10));
        cold.record(ColdEvent::Request, Instant::now(), || {
            panic!("a dropped request needs no name")
        });
        cold.record(ColdEvent::Execution, Instant::now(), || "const eval".into());
        let kept = cold.events.into_inner().unwrap();
        assert_eq!(kept.dropped[req], 1);
        assert_eq!(kept.spans[ColdEvent::Execution as usize][0].name, "const eval");
    }
}
