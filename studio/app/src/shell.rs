//! The window's life outside egui: one instance, the tray icon, show and hide.
//!
//! Showing and hiding go straight through Win32 rather than egui viewport
//! commands. A hidden window gets no paint messages, so egui never runs a
//! frame for it and a "show yourself" command would wait forever; ShowWindow
//! does not care.

use openrazer_studio_core::t;

pub const WINDOW_TITLE: &str = "OpenRazer Studio";

#[cfg(windows)]
mod win {
    use super::WINDOW_TITLE;
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS, HWND};
    use windows_sys::Win32::System::Threading::CreateMutexW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        FindWindowW, IsIconic, SetForegroundWindow, ShowWindow, SW_HIDE, SW_RESTORE, SW_SHOW,
    };

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn window() -> Option<HWND> {
        let title = wide(WINDOW_TITLE);
        let hwnd = unsafe { FindWindowW(std::ptr::null(), title.as_ptr()) };
        if hwnd.is_null() {
            None
        } else {
            Some(hwnd)
        }
    }

    /// True if this is the only control panel in the session.
    pub fn claim_single_instance() -> bool {
        let name = wide("Local\\openrazer-studio");
        unsafe {
            let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
            // Deliberately never closed: held for the life of the process.
            !handle.is_null() && GetLastError() != ERROR_ALREADY_EXISTS
        }
    }

    pub fn show() {
        if let Some(hwnd) = window() {
            unsafe {
                if IsIconic(hwnd) != 0 {
                    ShowWindow(hwnd, SW_RESTORE);
                } else {
                    ShowWindow(hwnd, SW_SHOW);
                }
                SetForegroundWindow(hwnd);
            }
        }
    }

    pub fn hide() {
        if let Some(hwnd) = window() {
            unsafe {
                ShowWindow(hwnd, SW_HIDE);
            }
        }
    }
}

#[cfg(not(windows))]
mod win {
    pub fn claim_single_instance() -> bool {
        true
    }
    pub fn show() {}
    pub fn hide() {}
}

pub use win::{claim_single_instance, hide, show};

/// The program's icon: a green ring with a bright core, drawn rather than
/// shipped, so the tray, the window and the executable all agree.
pub fn icon_rgba(size: u32) -> Vec<u8> {
    let mut pixels = vec![0u8; (size * size * 4) as usize];
    let s = size as f32;
    let c = (s - 1.0) / 2.0;
    let mix = |from: [f32; 3], to: [f32; 3], t: f32| [0, 1, 2].map(|i| from[i] + (to[i] - from[i]) * t);
    for y in 0..size {
        for x in 0..size {
            let d = ((x as f32 - c).powi(2) + (y as f32 - c).powi(2)).sqrt();
            // Antialiased "inside a circle of this radius".
            let inside = |radius: f32| (radius - d + 0.5).clamp(0.0, 1.0);
            let mut colour = [18.0, 22.0, 28.0];
            colour = mix(colour, [68.0, 214.0, 44.0], inside(s * 0.44) * (1.0 - inside(s * 0.31)));
            colour = mix(colour, [184.0, 255.0, 168.0], inside(s * 0.15));
            let i = ((y * size + x) * 4) as usize;
            pixels[i] = colour[0] as u8;
            pixels[i + 1] = colour[1] as u8;
            pixels[i + 2] = colour[2] as u8;
            pixels[i + 3] = (inside(s * 0.48) * 255.0) as u8;
        }
    }
    pixels
}

pub enum TrayAction {
    Open,
    RestartDaemon,
    Quit,
}

/// The tray icon and its menu.  Must be built on the UI thread and kept alive.
pub struct Tray {
    _icon: tray_icon::TrayIcon,
}

impl Tray {
    pub fn new(on_action: impl Fn(TrayAction) + Send + Sync + 'static) -> Option<Tray> {
        use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
        use tray_icon::{Icon, MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

        let open = MenuItem::new(t("Open OpenRazer Studio"), true, None);
        let restart = MenuItem::new(t("Restart the daemon"), true, None);
        let quit = MenuItem::new(t("Quit"), true, None);
        let menu = Menu::new();
        menu.append_items(&[&open, &restart, &PredefinedMenuItem::separator(), &quit]).ok()?;
        let icon = Icon::from_rgba(icon_rgba(32), 32, 32).ok()?;
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu))
            .with_tooltip(WINDOW_TITLE)
            .with_icon(icon)
            .with_menu_on_left_click(false)
            .build()
            .ok()?;

        let on_action = std::sync::Arc::new(on_action);
        let (open_id, restart_id, quit_id) = (open.id().clone(), restart.id().clone(), quit.id().clone());
        let menu_action = on_action.clone();
        MenuEvent::set_event_handler(Some(move |event: MenuEvent| {
            if event.id == open_id {
                menu_action(TrayAction::Open);
            } else if event.id == restart_id {
                menu_action(TrayAction::RestartDaemon);
            } else if event.id == quit_id {
                menu_action(TrayAction::Quit);
            }
        }));
        TrayIconEvent::set_event_handler(Some(move |event: TrayIconEvent| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                on_action(TrayAction::Open);
            }
        }));
        Some(Tray { _icon: tray })
    }
}
