//! Embed the icon, version information and manifest in the executable.

fn main() {
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
