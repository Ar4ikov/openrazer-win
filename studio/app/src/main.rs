//! OpenRazer Studio: Razer device control on Windows.
//!
//! A client of the openrazer-win daemon, which owns the devices. Started with
//! `--background` (as it is at sign-in) it lives in the tray and makes sure
//! the daemon is running; otherwise it opens its window.

#![windows_subsystem = "windows"]

mod appwatch;
mod art;
mod daemon;
mod images;
mod layout;
mod outline;
mod preview;
mod pysetup;
mod report;
mod settings;
mod shell;
mod ui;
mod updates;
mod worker;

use std::sync::Arc;

fn main() -> eframe::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--version" || a == "-V") {
        attach_console();
        println!("openrazer-studio {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.iter().any(|a| a == "--self-test") {
        // For the release build's smoke test: everything short of a window.
        let icon = shell::icon_rgba(64);
        let drawn = art::Art::build_with("keyboard", &serde_json::json!({"matrix_dimensions": [6, 22], "custom_frame": true, "zones": {"backlight": {}}}), None, None);
        let ok = icon.len() == 64 * 64 * 4 && icon[(32 * 64 + 32) * 4 + 3] == 255 && drawn.shapes.len() > 100;
        std::process::exit(if ok { 0 } else { 1 });
    }
    let background = args.iter().any(|a| a == "--background" || a == "--tray");

    if !shell::claim_single_instance() {
        if !background {
            shell::show();
        }
        return Ok(());
    }

    let icon = egui::IconData { rgba: shell::icon_rgba(64), width: 64, height: 64 };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(shell::WINDOW_TITLE)
            .with_inner_size([1320.0, 860.0])
            .with_min_inner_size([1000.0, 660.0])
            .with_visible(!background)
            .with_icon(Arc::new(icon)),
        ..Default::default()
    };
    eframe::run_native(shell::WINDOW_TITLE, options, Box::new(move |cc| Ok(Box::new(ui::App::new(cc, background)))))
}

/// Print to the console that started us, if any: this is a windowed program.
fn attach_console() {
    #[cfg(windows)]
    unsafe {
        windows_sys::Win32::System::Console::AttachConsole(windows_sys::Win32::System::Console::ATTACH_PARENT_PROCESS);
    }
}
