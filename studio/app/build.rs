//! Embed the icon, version information and manifest in the executable.

fn main() {
    // `OPENRAZER_STUDIO_CHROMA` names a folder holding the stand-in for Razer's
    // Chroma SDK DLL, built from `../chroma` as RzChromaSDK64.dll and, 32-bit,
    // RzChromaSDK.dll; a release build sets it, and Studio puts them beside
    // the games it is asked to. Without it Studio says it has none to give.
    println!("cargo::rustc-check-cfg=cfg(chroma)");
    println!("cargo::rerun-if-env-changed=OPENRAZER_STUDIO_CHROMA");
    if let Some(dir) = std::env::var_os("OPENRAZER_STUDIO_CHROMA") {
        let dir = std::path::PathBuf::from(dir);
        let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").expect("cargo sets OUT_DIR"));
        for name in ["RzChromaSDK64.dll", "RzChromaSDK.dll"] {
            let from = dir.join(name);
            std::fs::copy(&from, out.join(name)).unwrap_or_else(|e| panic!("OPENRAZER_STUDIO_CHROMA has no readable {}: {e}", from.display()));
            println!("cargo::rerun-if-changed={}", from.display());
        }
        println!("cargo::rustc-cfg=chroma");
    }

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let mut resource = winresource::WindowsResource::new();
    resource.set_icon("assets/openrazer.ico");
    resource.set("FileDescription", "OpenRazer Studio");
    resource.set("ProductName", "OpenRazer Studio");
    resource.set("LegalCopyright", "GPL-2.0-or-later");
    resource.set_manifest(
        r#"<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
  <security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security>
</trustInfo>
</assembly>"#,
    );
    if let Err(error) = resource.compile() {
        println!("cargo:warning=could not embed the icon: {error}");
    }
}
