#![feature(unboxed_closures)]

/// Adds the final array element to the first argument of a Rust-call function.
#[inline(never)]
pub extern "rust-call" fn add((a, b): (&usize, [usize; 5])) -> usize {
    *a + b[4]
}

#[test]
fn direct_and_reified() {
    let args = (&9, [1, 2, 3, 4, 5]);
    assert_eq!(add(args), 14);
    let pointer = std::hint::black_box(add as extern "rust-call" fn((&usize, [usize; 5])) -> usize);
    assert_eq!(pointer(args), 14);
}
