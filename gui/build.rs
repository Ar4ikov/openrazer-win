//! Embed the icon and version information in the executable.

fn main() {
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
