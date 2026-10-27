// Native LazyLock callbacks and rebuilt callers use the same immutable static, edition 2024.
mod c0 {
    use super::*;
    static RULE: &str = "assignment";
    static REGISTRY: std::sync::LazyLock<&'static &'static str> = std::sync::LazyLock::new(|| &RULE);
    pub fn run() {
        feed(u64::from(std::ptr::eq(*REGISTRY, &RULE)));
        feed(1);
    }
}
//// then
mod c0 {
    use super::*;
    static RULE: &str = "assignment";
    static REGISTRY: std::sync::LazyLock<&'static &'static str> = std::sync::LazyLock::new(|| &RULE);
    pub fn run() {
        feed(u64::from(std::ptr::eq(*REGISTRY, &RULE)));
        feed(2);
    }
}
