//! `RA_JIT_TESTS=affected`: `cargo ra-jit test` runs, of each program of tests the daemon
//! built, the tests whose end an edit could have changed since they last passed, and says how
//! many it left. The daemon writes beside the program a hash of each test's code with all it
//! reaches ([`plan::REACH`]). A test runs again when its hash is not the one it passed with,
//! when it reaches what its code does not decide (files, the environment, processes, the
//! network, code ra-jit can't compile), and when it has not passed. Unset, or `all`, runs
//! every test, as cargo does.

use std::{collections::BTreeMap, path::PathBuf};

use sha2::{Digest as _, Sha256};

use super::{
    cli::Invocation,
    plan,
    state::{Kind, Program, State},
};

pub(crate) const VAR: &str = "RA_JIT_TESTS";

/// The file beside a program of tests with the tests that passed, one a line: what
/// [`plan::REACH`] said of the test then, a tab and its name.
const PASSED: &str = "tests-passed";

/// The most bytes of names a program of tests is given to pick its tests by; past it, it runs
/// them all.
const NAMES_AT_MOST: usize = 64 << 10;

/// Whether this command runs only the tests an edit reached.
fn on() -> bool {
    std::env::var(VAR).is_ok_and(|it| it == "affected")
}

/// Has the daemon write [`plan::REACH`] at its builds of the programs of tests of `state`
/// while only the tests an edit reached are run, and stop when every test is.
pub(crate) fn ask(state: &State) {
    let on = on();
    for program in state.programs.iter().filter(|it| it.test) {
        let Some(dir) = state.plan_dir(program) else { continue };
        let wanted = dir.join(plan::REACH_WANTED);
        match on {
            true if wanted.is_file() => {}
            true => _ = std::fs::write(&wanted, ""),
            false => {
                _ = std::fs::remove_file(&wanted);
                _ = std::fs::remove_file(dir.join(plan::REACH));
            }
        }
    }
}

/// Forgets what the daemon wrote of the programs of `state`, which cargo built since.
pub(crate) fn forget(state: &State) {
    for program in state.programs.iter().filter(|it| it.test) {
        if let Some(dir) = state.plan_dir(program) {
            _ = std::fs::remove_file(dir.join(plan::REACH));
        }
    }
}

/// What of a program of tests this command runs.
pub(crate) enum Pick {
    /// What the command asks, every test unless it names some: nothing is known of what an
    /// edit reached, or the command picks the tests itself.
    Asked,
    /// The tests `names` names, of the `total` the program has, or all of them when `None`;
    /// `always` are those of them run every time.
    Some { names: Option<Vec<String>>, total: usize, always: Vec<Always>, passed: Passed },
}

/// A test that is run every time, and why.
pub(crate) struct Always {
    pub(crate) name: String,
    pub(crate) why: String,
    /// The calls that take the test to what it reaches, from the test on, joined by ` -> `.
    pub(crate) path: Option<String>,
}

/// What is remembered of a program of tests once it passed.
pub(crate) struct Passed {
    file: PathBuf,
    /// Each test that passed before and was not run, and each that ran, by name.
    tests: BTreeMap<String, String>,
}

impl Passed {
    /// Forgets that the tests `failed` passed, or, when the program did not say which failed,
    /// all the tests `ran` (all of them when `None`): each runs until it passes again, even
    /// as its code goes back to what passed before. The others that ran passed.
    pub(crate) fn failed(mut self, ran: Option<&[String]>, failed: Option<&[String]>) {
        match (failed, ran) {
            (Some(failed), _) => failed.iter().for_each(|it| _ = self.tests.remove(it)),
            (None, Some(ran)) => ran.iter().for_each(|it| _ = self.tests.remove(it)),
            (None, None) => self.tests.clear(),
        }
        self.passed();
    }

    /// Remembers that the tests that ran passed.
    pub(crate) fn passed(self) {
        let mut text = String::new();
        for (name, identity) in &self.tests {
            text.push_str(&format!("{identity}\t{name}\n"));
        }
        let writing = self.file.with_extension("writing");
        if std::fs::write(&writing, text)
            .and_then(|()| std::fs::rename(&writing, &self.file))
            .is_err()
        {
            _ = std::fs::remove_file(&self.file);
        }
    }
}

/// Which tests of `test` this command runs.
pub(crate) fn pick(asked: &Invocation, state: &State, test: &Program) -> Pick {
    if !on() || !asked.positional.is_empty() || !plain(&asked.trailing) {
        return Pick::Asked;
    }
    let Some(dir) = state.plan_dir(test) else { return Pick::Asked };
    let Ok(reach) = std::fs::read_to_string(dir.join(plan::REACH)) else { return Pick::Asked };
    let file = dir.join(PASSED);
    let before = std::fs::read_to_string(&file).unwrap_or_default();
    let before: BTreeMap<&str, &str> = before
        .lines()
        .filter_map(|it| it.split_once('\t'))
        .map(|(identity, name)| (name, identity))
        .collect();
    let programs = programs_run_by(state, test);
    let (mut names, mut tests, mut always, mut total) =
        (Vec::new(), BTreeMap::new(), Vec::new(), 0);
    for line in reach.lines() {
        let mut parts = line.split('\t');
        let (Some(hash), Some(name)) = (parts.next(), parts.next()) else { continue };
        if hash == "*" {
            return Pick::Asked;
        }
        total += 1;
        // Older plans prefix the hash of a test with external inputs with a tilde.
        if hash == "-" || hash.starts_with('~') {
            names.push(name.to_owned());
            let why = parts.next().unwrap_or_default().to_owned();
            let path = parts.next().map(str::to_owned);
            always.push(Always { name: name.to_owned(), why, path });
            continue;
        }
        let identity = format!("{hash}{programs}");
        if before.get(name) != Some(&identity.as_str()) {
            names.push(name.to_owned());
        }
        tests.insert(name.to_owned(), identity);
    }
    let long = names.iter().map(|it| it.len() + 1).sum::<usize>() > NAMES_AT_MOST;
    let names = (names.len() != total && !long).then_some(names);
    Pick::Some { names, total, always, passed: Passed { file, tests } }
}

/// Whether what follows `--` leaves the tests to the program to pick and says nothing of how
/// a test ends: what only shows the run otherwise. One that runs the ignored tests, or lists
/// them, runs as asked.
fn plain(trailing: &[String]) -> bool {
    let mut args = trailing.iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-q" | "--quiet" | "--nocapture" | "--no-capture" | "--show-output" => {}
            "--test-threads" | "--color" if args.next().is_some() => {}
            _ if arg.starts_with("--test-threads=") || arg.starts_with("--color=") => {}
            _ => return false,
        }
    }
    true
}

/// What stands for the programs an integration test of `test`'s package runs, which cargo
/// gives it as `CARGO_BIN_EXE_<name>`: their sizes and times, which a build changes. Empty
/// for a unit test.
fn programs_run_by(state: &State, test: &Program) -> String {
    if !matches!(test.kind, Kind::Test | Kind::Bench) {
        return String::new();
    }
    let mut hash = Sha256::new();
    let runs = |it: &&Program| it.package == test.package && it.kind == Kind::Bin && !it.test;
    for program in state.programs.iter().filter(runs) {
        hash.update(program.executable.as_os_str().as_encoded_bytes());
        let meta = std::fs::metadata(&program.executable).ok();
        let at = meta.as_ref().and_then(|it| it.modified().ok());
        let at = at.and_then(|it| it.duration_since(std::time::UNIX_EPOCH).ok());
        hash.update(meta.map_or(0, |it| it.len()).to_le_bytes());
        hash.update(at.map_or(0, |it| it.as_nanos()).to_le_bytes());
    }
    let hex: String = hash.finalize().iter().take(8).map(|it| format!("{it:02x}")).collect();
    format!("-{hex}")
}

#[cfg(test)]
mod tests {
    use super::plain;

    fn of(args: &[&str]) -> bool {
        plain(&args.iter().map(|it| (*it).to_owned()).collect::<Vec<_>>())
    }

    #[test]
    fn what_only_shows_the_run_is_plain() {
        assert!(of(&[]));
        assert!(of(&["--nocapture", "-q", "--test-threads", "4", "--color=never"]));
        assert!(!of(&["--ignored"]));
        assert!(!of(&["--include-ignored"]));
        assert!(!of(&["--list"]));
        assert!(!of(&["--test-threads"]));
        assert!(!of(&["some_test"]));
    }
}
