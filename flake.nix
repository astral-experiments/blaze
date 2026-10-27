{
  description = "blaze: cargo build, test and run from a daemon that keeps your workspace compiled, with live edits";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { nixpkgs, rust-overlay, flake-utils, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ] (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ rust-overlay.overlays.default ];
        };
        # The one toolchain ra-jit is pinned to: the dated nightly in harness/toolchain
        # (`nightly-YYYY-MM-DD`, moved by harness/toolchain_bump.py, which also moves the
        # rust-overlay input to one that has it). rust-src is the sysroot source rust-analyzer
        # reads `core` from.
        pin = pkgs.lib.removePrefix "nightly-" (pkgs.lib.fileContents ./harness/toolchain);
        rust = pkgs.rust-bin.nightly.${pin}.default.override {
          extensions = [ "rust-src" ];
        };
        # The same nightly with rustc_codegen_cranelift for bench.py --setups.
        nightly = pkgs.rust-bin.nightly.${pin}.default.override {
          extensions = [ "rust-src" "rustc-codegen-cranelift-preview" ];
        };
        # The dev shells and CI (.github/actions/toolchain) use this LLVM for the `llvm` feature.
        # LLVM_SYS_201_PREFIX names the output with llvm-config, the headers and the libraries;
        # crates/hir-ty-codegen/build.rs links what it names and refuses any other.
        llvm = pkgs.llvmPackages_20.llvm;
        llvmEnv = { LLVM_SYS_201_PREFIX = "${llvm.dev}"; };
        # What harness/bench.py runs in locally: the
        # pinned toolchain, LLVM 20 for `--features llvm`, and the C side the projects' build
        # scripts use. Nothing here wraps rustc (no sccache) or replaces the default linker:
        # mold and wild are on PATH for the setups that ask for them with `-fuse-ld`. Every
        # library directory here costs every link a `realpath` in Nix's linker wrapper, which
        # cargo pays and ra-jit does not: what only one project needs goes in a shell of its own.
        bench = llvmEnv // {
          packages = [
            rust
            pkgs.python3
            pkgs.git
            pkgs.pkg-config
            pkgs.openssl
            llvm.dev
            pkgs.libffi
            pkgs.libxml2
            pkgs.zlib
            pkgs.ncurses
          ] ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
            # bevy's audio, input and windowing crates link these.
            pkgs.alsa-lib
            pkgs.udev
            pkgs.wayland
            pkgs.libxkbcommon
            pkgs.mold
            pkgs.wild
          ] ++ [
            # dx, for harness/hotpatch.py's subsecond (Dioxus hot-patching).
            pkgs.dioxus-cli
          ];
          RA_JIT_NIGHTLY = "${nightly}";
          # A C compiler as a distribution ships it: the wrapper's _FORTIFY_SOURCE warns at the
          # -O0 of a debug build, which fails configure probes (jemalloc's, in fd).
          hardeningDisable = [ "all" ];
        };
      in {
        packages.llvm = llvm;
        # `nix develop`: the pinned LLVM (LLVM_SYS_201_PREFIX, its tools on PATH) and the C
        # toolchain it was built with, so `cargo build --features llvm` links against it. Rust is
        # rustup at the pinned nightly: plain `cargo` is harness/toolchain's and `cargo +<toolchain>` works.
        devShells.default = pkgs.mkShell (llvmEnv // {
          packages = [ pkgs.rustup pkgs.python3 llvm llvm.dev ];
          RUSTUP_TOOLCHAIN = pkgs.lib.fileContents ./harness/toolchain;
          # As in the bench shell: the wrapper's _FORTIFY_SOURCE fails configure probes at -O0 in
          # the C code of the projects the harnesses build.
          hardeningDisable = [ "all" ];
        } // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
          # rustup's rustc links x86-64 Linux with its own rust-lld, past the wrapper of the
          # shell's linker that writes the store's libraries into the binary's RUNPATH: without
          # this the binary finds neither libLLVM nor the store's glibc and libgcc_s when it runs.
          CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS = "-Clinker-features=-lld";
        });
        devShells.bench = pkgs.mkShell bench;
        # What harness/bench.py and runbench.py measure bun in (harness/bunprep.py):
        # bun's own pinned nightly in place of ra-jit's (rust-src, which ra-jit reads `core` from), and
        # what bun's own build of its C and C++ needs, as its flake.nix has it: LLVM 23 as the
        # C and C++ compiler, cmake, ninja, nasm (x86), go, a bun to run its build scripts, and on Linux lld.
        # ra-jit itself is built in the bench shell.
        devShells.bench-bun = (pkgs.mkShell.override { stdenv = pkgs.llvmPackages_23.stdenv; }) {
          packages = [
            (pkgs.rust-bin.nightly."2026-09-15".default.override { extensions = [ "rust-src" ]; })
            pkgs.python3
            pkgs.git
            pkgs.curl
            pkgs.unzip
            pkgs.xz
            pkgs.pkg-config
            pkgs.cmake
            pkgs.ninja
            pkgs.nasm
            pkgs.go
            pkgs.bun
            pkgs.perl
            pkgs.ruby
            pkgs.libtool
            pkgs.llvmPackages_23.clang
            pkgs.llvmPackages_23.llvm
            pkgs.llvmPackages_23.lld
          ];
          hardeningDisable = [ "all" ];
          shellHook = ''
            export CC=${pkgs.llvmPackages_23.clang}/bin/clang CXX=${pkgs.llvmPackages_23.clang}/bin/clang++
            export AR=${pkgs.llvmPackages_23.llvm}/bin/llvm-ar RANLIB=${pkgs.llvmPackages_23.llvm}/bin/llvm-ranlib
          '' + pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux ''
            export NIX_CFLAGS_LINK="''${NIX_CFLAGS_LINK:+$NIX_CFLAGS_LINK }-fuse-ld=lld"
          '';
        };
        # The bench shell and what zed's build needs besides (its own nix/build.nix lists the
        # same): `nix develop .#bench-zed`.
        devShells.bench-zed = pkgs.mkShell (bench // {
          packages = bench.packages ++ [ pkgs.cmake pkgs.protobuf ] ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
            pkgs.curl
            pkgs.fontconfig
            pkgs.freetype
            pkgs.libgit2
            pkgs.sqlite
            pkgs.zstd
            pkgs.glib
            pkgs.libva
            pkgs.vulkan-loader
            pkgs.libglvnd
            pkgs.libx11
            pkgs.libxcb
            pkgs.libdrm
            pkgs.libgbm
            pkgs.libxcomposite
            pkgs.libxdamage
            pkgs.libxext
            pkgs.libxfixes
            pkgs.libxrandr
          ];
          PROTOC = "${pkgs.protobuf}/bin/protoc";
        } // pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          # zed's build script looks for glib's headers under /usr/include.
          CXXFLAGS = "-I${pkgs.glib.dev}/include/glib-2.0 -I${pkgs.glib.out}/lib/glib-2.0/include";
          ZSTD_SYS_USE_PKG_CONFIG = "1";
        } // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
          # The WebRTC zed links statically, the build its build script would download into
          # every cold target directory.
          LK_CUSTOM_WEBRTC = pkgs.fetchzip {
            url = "https://github.com/zed-industries/livekit-rust-sdks/releases/download/webrtc-0001d84-4/webrtc-linux-x64-release.zip";
            sha256 = "0hlv1p6fi1lgfdyq8q49gghbqbqnq50icrw7i25g2qwqmpf2jyi7";
          };
        });
      });
}
