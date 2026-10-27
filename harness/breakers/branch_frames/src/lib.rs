//! ty's recursive type relation checker kept the temporaries of every match arm in each
//! frame. The two arguments of one call need distinct storage. Different arms can reuse it.

use std::hint::black_box;

#[inline(never)]
fn pair(left: &[u8; 1024], right: &[u8; 1024]) -> u32 {
    3 * u32::from(black_box(left)[13]) + u32::from(black_box(right)[17])
}

#[inline(never)]
pub fn nested(depth: u8) -> u32 {
    if depth == 0 {
        return 0;
    }
    let value = match depth % 4 {
        0 => pair(&[depth; 1024], &[depth + 1; 1024]),
        1 => pair(&[depth + 1; 1024], &[depth + 2; 1024]),
        2 => pair(&[depth + 2; 1024], &[depth + 3; 1024]),
        _ => pair(&[depth + 3; 1024], &[depth + 4; 1024]),
    };
    value + black_box(nested(depth - 1))
}

#[test]
fn mutually_exclusive_arms_fit_on_one_stack() {
    let expected: u32 = (1..=24).map(|depth| 4 * (depth + depth % 4) + 1).sum();
    let worker = std::thread::Builder::new().stack_size(128 * 1024);
    assert_eq!(worker.spawn(|| nested(24)).unwrap().join().unwrap(), expected);
}
