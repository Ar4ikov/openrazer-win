//! Embed the icon and version information in the executable, and -- for a
//! release -- the daemon itself.
//!
//! With `OPENRAZER_EMBED_DAEMON` pointing at a frozen `openrazer-win.exe`, the
//! control panel carries the daemon inside it and needs nothing else on the
//! machine. Without it (an ordinary `cargo build`) the panel looks for the
//! daemon beside it, on PATH, or as a Python module.

fn main() {
    println!("cargo::rustc-check-cfg=cfg(embedded_daemon)");
    println!("cargo::rerun-if-env-changed=OPENRAZER_EMBED_DAEMON");
    if let Some(daemon) = std::env::var_os("OPENRAZER_EMBED_DAEMON") {
        let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").expect("cargo sets OUT_DIR"));
        std::fs::copy(&daemon, out.join("openrazer-win.exe")).expect("OPENRAZER_EMBED_DAEMON names a readable file");
        println!("cargo::rerun-if-changed={}", std::path::Path::new(&daemon).display());
        println!("cargo::rustc-cfg=embedded_daemon");
    }

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let mut resource = winresource::WindowsResource::new();
    resource.set_icon("assets/openrazer.ico");
    resource.set("FileDescription", "OpenRazer for Windows");
    resource.set("ProductName", "OpenRazer for Windows");
    resource.set("LegalCopyright", "GPL-2.0-or-later");
    if let Err(error) = resource.compile() {
        println!("cargo:warning=could not embed the icon: {error}");
    }
}
