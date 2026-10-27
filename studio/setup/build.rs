//! Put what the installer installs inside it.
//!
//! `OPENRAZER_STUDIO_PAYLOAD` names a folder holding `openrazer-studio.exe`
//! and `openrazer-win.exe`; a release build sets it. Without it the installer
//! still builds -- for tests and clippy -- and says it has nothing to install.

fn main() {
    println!("cargo::rustc-check-cfg=cfg(payload)");
    println!("cargo::rerun-if-env-changed=OPENRAZER_STUDIO_PAYLOAD");
    if let Some(dir) = std::env::var_os("OPENRAZER_STUDIO_PAYLOAD") {
        let dir = std::path::PathBuf::from(dir);
        let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").expect("cargo sets OUT_DIR"));
        for name in ["openrazer-studio.exe", "openrazer-win.exe"] {
            let from = dir.join(name);
            std::fs::copy(&from, out.join(name)).unwrap_or_else(|e| panic!("OPENRAZER_STUDIO_PAYLOAD has no readable {}: {e}", from.display()));
            println!("cargo::rerun-if-changed={}", from.display());
        }
        println!("cargo::rustc-cfg=payload");
    }

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let mut resource = winresource::WindowsResource::new();
    resource.set_icon("assets/openrazer.ico");
    resource.set("FileDescription", "OpenRazer Studio Setup");
    resource.set("ProductName", "OpenRazer Studio");
    resource.set("LegalCopyright", "GPL-2.0-or-later");
    // Without this Windows guesses from the word "setup" in the name that it
    // needs administrator rights, and asks for them. It does not: it installs
    // for the current user only.
    resource.set_manifest(
        r#"<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
  <security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security>
</trustInfo>
<compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
  <application><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application>
</compatibility>
</assembly>"#,
    );
    if let Err(error) = resource.compile() {
        println!("cargo:warning=could not embed the icon and manifest: {error}");
    }
}
