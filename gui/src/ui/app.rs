//! The window: device list on the left, the chosen device on the right.

use super::edit::{DeviceEdit, Pending};
use super::theme::{self, ACCENT, BG, BORDER, CARD, CARD_ACTIVE, CARD_HOVER, SIDEBAR, TEXT, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::images::Images;
use crate::shell::{self, Tray, TrayAction};
use crate::worker::{self, Command, DaemonState, Device, Shared, Snapshot};
use egui::{pos2, vec2, Align, Color32, CornerRadius, Frame, Layout, Margin, Rect, RichText, Sense, Stroke, StrokeKind};
use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

thread_local! {
    /// The tray icon lives on the UI thread; kept here so Quit can take it
    /// down before the process exits rather than leave a ghost in the tray.
    static TRAY: RefCell<Option<Tray>> = const { RefCell::new(None) };
}

#[derive(Clone, PartialEq)]
pub enum View {
    Device(String),
    Settings,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Tab {
    Lighting,
    Performance,
    Power,
    Macros,
    Profiles,
    Info,
}

impl Tab {
    fn label(self) -> &'static str {
        match self {
            Tab::Lighting => "Lighting",
            Tab::Performance => "Performance",
            Tab::Power => "Power",
            Tab::Macros => "Macro keys",
            Tab::Profiles => "Profiles",
            Tab::Info => "About",
        }
    }
}

#[derive(serde::Serialize, serde::Deserialize, Clone)]
pub struct GuiSettings {
    pub close_to_tray: bool,
}

impl Default for GuiSettings {
    fn default() -> Self {
        GuiSettings { close_to_tray: true }
    }
}

impl GuiSettings {
    fn path() -> std::path::PathBuf {
        crate::rpc::config_dir().join("gui.json")
    }
    fn load() -> Self {
        std::fs::read_to_string(Self::path()).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
    }
    pub fn save(&self) {
        if let Some(dir) = Self::path().parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = std::fs::write(Self::path(), serde_json::to_string_pretty(self).unwrap_or_default());
    }
}

pub struct App {
    pub shared: Shared,
    pub tx: Sender<Command>,
    pub images: Images,
    pub view: Option<View>,
    pub tab: Tab,
    pub edits: HashMap<String, DeviceEdit>,
    pub pending: Vec<Pending>,
    pub settings: GuiSettings,
    pub autostart: bool,
    pub macro_edit: Option<super::macros::MacroEdit>,
    pub profile_name: String,
    tray_ok: bool,
    capture: Option<Capture>,
    /// eframe shows the window after its first frame whatever the viewport
    /// asked for, so a background start hides it again for a few frames.
    hide_frames: u8,
}

/// For checking the design: `OPENRAZER_GUI_CAPTURE=path.png` saves one
/// screenshot of the window once the daemon has answered, then exits.
/// `OPENRAZER_GUI_PAGE` picks what is on screen: `settings`, or
/// `<device index>:<tab>` such as `0:lighting`.
struct Capture {
    path: std::path::PathBuf,
    page: String,
    started: Instant,
    requested: bool,
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>, background: bool) -> App {
        theme::apply(&cc.egui_ctx);
        let shared: Shared = Default::default();
        let ctx = cc.egui_ctx.clone();
        let tx = worker::spawn(shared.clone(), move || ctx.request_repaint());
        // The control panel is how most people start the daemon; do it for them.
        let _ = tx.send(Command::StartDaemon);
        crate::daemon::refresh_autostart();

        let ctx = cc.egui_ctx.clone();
        let tray_tx = tx.clone();
        let tray = Tray::new(move |action| match action {
            TrayAction::Open => {
                shell::show();
                ctx.request_repaint();
            }
            TrayAction::RestartDaemon => {
                let _ = tray_tx.send(Command::RestartDaemon);
            }
            TrayAction::Quit => quit(),
        });
        let tray_ok = tray.is_some();
        TRAY.with(|slot| *slot.borrow_mut() = tray);
        if background && !tray_ok {
            // Nowhere to live but the window, so show it after all.
            shell::show();
        }

        App {
            shared,
            tx,
            images: Images::default(),
            view: None,
            tab: Tab::Lighting,
            edits: HashMap::new(),
            pending: Vec::new(),
            settings: GuiSettings::load(),
            autostart: crate::daemon::autostart_enabled(),
            macro_edit: None,
            profile_name: String::new(),
            tray_ok,
            hide_frames: if background && tray_ok { 4 } else { 0 },
            capture: std::env::var_os("OPENRAZER_GUI_CAPTURE").map(|path| Capture {
                path: path.into(),
                page: std::env::var("OPENRAZER_GUI_PAGE").unwrap_or_default(),
                started: Instant::now(),
                requested: false,
            }),
        }
    }

    pub fn send(&self, command: Command) {
        let _ = self.tx.send(command);
    }

    fn selected_device<'a>(&self, snap: &'a Snapshot) -> Option<&'a Device> {
        match &self.view {
            Some(View::Device(serial)) => snap.devices.iter().find(|d| &d.serial == serial),
            _ => None,
        }
    }

    /// Run whatever was waiting for the user to stop dragging.
    fn flush_pending(&mut self, ctx: &egui::Context) {
        let now = Instant::now();
        let (due, waiting): (Vec<_>, Vec<_>) = std::mem::take(&mut self.pending).into_iter().partition(|p| p.due <= now);
        self.pending = waiting;
        for pending in due {
            self.send(pending.command);
        }
        if let Some(next) = self.pending.iter().map(|p| p.due).min() {
            ctx.request_repaint_after(next.saturating_duration_since(now));
        }
    }

    /// Queue `command`, replacing an earlier one of the same kind.
    pub fn debounce(&mut self, key: String, command: Command, delay_ms: u64) {
        self.pending.retain(|p| p.key != key);
        self.pending.push(Pending { key, command, due: Instant::now() + Duration::from_millis(delay_ms) });
    }
}

fn quit() {
    TRAY.with(|slot| drop(slot.borrow_mut().take()));
    std::process::exit(0);
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if self.hide_frames > 0 {
            self.hide_frames -= 1;
            shell::hide();
            ctx.request_repaint();
        }
        if ctx.input(|i| i.viewport().close_requested()) && self.settings.close_to_tray && self.tray_ok {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            shell::hide();
        }
        let snap = self.shared.lock().unwrap().clone();
        self.sync(&snap);
        self.flush_pending(ctx);

        egui::SidePanel::left("devices")
            .exact_width(276.0)
            .resizable(false)
            .frame(Frame::new().fill(SIDEBAR).inner_margin(Margin { left: 14, right: 14, top: 18, bottom: 14 }))
            .show(ctx, |ui| self.sidebar(ui, &snap));

        egui::CentralPanel::default()
            .frame(Frame::new().fill(BG).inner_margin(Margin { left: 30, right: 30, top: 24, bottom: 18 }))
            .show(ctx, |ui| self.main(ui, &snap));

        self.toast(ctx, &snap);
        self.capture(ctx, &snap);
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        TRAY.with(|slot| drop(slot.borrow_mut().take()));
    }
}

impl App {
    /// Keep the view pointing at something that exists, and edits current.
    fn sync(&mut self, snap: &Snapshot) {
        let valid = match &self.view {
            Some(View::Device(serial)) => snap.devices.iter().any(|d| &d.serial == serial),
            Some(View::Settings) => true,
            None => false,
        };
        if !valid {
            self.view = snap.devices.first().map(|d| View::Device(d.serial.clone()));
        }
        for device in &snap.devices {
            let detail = snap.details.get(&device.serial);
            let edit = self.edits.entry(device.serial.clone()).or_default();
            if let Some(detail) = detail {
                edit.refresh(device, detail, snap);
            }
        }
    }

    fn capture(&mut self, ctx: &egui::Context, snap: &Snapshot) {
        let Some(capture) = self.capture.as_mut() else { return };
        let settled = match &snap.daemon {
            DaemonState::Running(info) => !snap.devices.is_empty() || info["state"] == "ready",
            _ => snap.start_error.is_some(),
        };
        let ready = settled && !snap.busy && capture.started.elapsed() > Duration::from_secs(4);
        if !capture.requested {
            if let Some((index, tab)) = capture.page.split_once(':') {
                if let Some(device) = index.parse::<usize>().ok().and_then(|i| snap.devices.get(i)) {
                    self.view = Some(View::Device(device.serial.clone()));
                }
                self.tab = match tab {
                    "performance" => Tab::Performance,
                    "power" => Tab::Power,
                    "macros" => Tab::Macros,
                    "profiles" => Tab::Profiles,
                    "info" => Tab::Info,
                    _ => Tab::Lighting,
                };
            } else if capture.page == "settings" {
                self.view = Some(View::Settings);
            }
            if ready {
                capture.requested = true;
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(Default::default()));
            }
            ctx.request_repaint_after(Duration::from_millis(200));
            return;
        }
        let shot = ctx.input(|i| {
            i.raw.events.iter().find_map(|e| match e {
                egui::Event::Screenshot { image, .. } => Some(image.clone()),
                _ => None,
            })
        });
        if let Some(image) = shot {
            let [w, h] = image.size;
            let bytes: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
            if let Some(buffer) = image::RgbaImage::from_raw(w as u32, h as u32, bytes) {
                let _ = buffer.save(&capture.path);
            }
            quit();
        }
        ctx.request_repaint();
    }

    // -- the sidebar --------------------------------------------------------
    fn sidebar(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        egui::TopBottomPanel::bottom("sidebar-footer")
            .frame(Frame::new().inner_margin(Margin { left: 0, right: 0, top: 10, bottom: 0 }))
            .show_separator_line(false)
            .show_inside(ui, |ui| self.sidebar_footer(ui, snap));

        ui.horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(vec2(30.0, 30.0), Sense::hover());
            let painter = ui.painter();
            painter.circle_filled(rect.center(), 14.0, Color32::from_rgb(0x12, 0x16, 0x1C));
            painter.circle_stroke(rect.center(), 10.5, Stroke::new(4.0_f32, ACCENT));
            painter.circle_filled(rect.center(), 4.5, Color32::from_rgb(0xB8, 0xFF, 0xA8));
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                ui.label(RichText::new("OpenRazer").font(theme::bold(18.0)));
                ui.label(RichText::new("for Windows").size(12.0).color(TEXT_DIM));
            });
        });
        ui.add_space(18.0);
        ui.horizontal(|ui| {
            widgets::section(ui, "Devices");
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let rescan = ui
                    .add(egui::Button::new(RichText::new(theme::icon::REFRESH).font(theme::icon_font(13.0)).color(TEXT_DIM)).frame(false))
                    .on_hover_text("Look for devices again, Bluetooth included");
                if rescan.clicked() {
                    self.send(Command::Rescan);
                }
                if snap.busy {
                    ui.add(egui::Spinner::new().size(12.0).color(TEXT_FAINT));
                }
            });
        });
        ui.add_space(4.0);

        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            if snap.devices.is_empty() {
                let text = match &snap.daemon {
                    DaemonState::Running(_) => "No Razer devices found",
                    DaemonState::Connecting => "Connecting…",
                    DaemonState::NotRunning(_) => "Waiting for the daemon",
                };
                ui.add_space(8.0);
                ui.label(widgets::dim(text).size(13.0));
            }
            for device in &snap.devices {
                let selected = self.view == Some(View::Device(device.serial.clone()));
                if self.device_row(ui, device, snap, selected).clicked() {
                    self.view = Some(View::Device(device.serial.clone()));
                    self.macro_edit = None;
                    // Re-read what may have changed on the device itself.
                    self.send(Command::LoadDevice(device.serial.clone()));
                }
            }
        });
    }

    fn device_row(&mut self, ui: &mut egui::Ui, device: &Device, snap: &Snapshot, selected: bool) -> egui::Response {
        let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 62.0), Sense::click());
        let fill = if selected { CARD_ACTIVE } else if response.hovered() { CARD_HOVER } else { Color32::TRANSPARENT };
        let painter = ui.painter_at(rect);
        painter.rect_filled(rect, CornerRadius::same(10), fill);
        if selected {
            painter.rect_filled(Rect::from_min_size(rect.min + vec2(0.0, 14.0), vec2(3.0, rect.height() - 28.0)), CornerRadius::same(2), ACCENT);
        }
        let thumb = Rect::from_min_size(rect.min + vec2(10.0, 9.0), vec2(44.0, 44.0));
        painter.rect_filled(thumb, CornerRadius::same(8), Color32::from_rgb(0x20, 0x25, 0x2F));
        let texture = device.image.as_ref().and_then(|url| self.images.get(ui.ctx(), url));
        match texture {
            Some(texture) => {
                let size = fit(texture.size_vec2(), thumb.shrink(3.0).size());
                painter.image(texture.id(), Rect::from_center_size(thumb.center(), size), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
            }
            None => widgets::device_glyph(&painter, thumb.shrink(8.0), &device.kind, TEXT_DIM),
        }
        let text_left = thumb.right() + 12.0;
        let name = ui.painter().layout(device.name.clone(), theme::bold(13.5), if selected { TEXT } else { Color32::from_rgb(0xD6, 0xD9, 0xDE) }, rect.right() - text_left - 8.0);
        let name_rows = name.rows.len().min(2) as f32;
        let top = rect.center().y - (name_rows * 17.0 + 16.0) / 2.0;
        painter.galley(pos2(text_left, top), name, TEXT);
        let mut sub = kind_label(&device.kind).to_string();
        if let Some(level) = snap.details.get(&device.serial).and_then(|d| d.battery) {
            sub.push_str(&format!("  ·  {level:.0}%"));
        }
        if snap.software.contains_key(&device.serial) {
            sub.push_str("  ·  live effect");
        }
        painter.text(pos2(text_left, top + name_rows * 17.0 + 2.0), egui::Align2::LEFT_TOP, sub, egui::FontId::proportional(12.0), TEXT_FAINT);
        response.on_hover_cursor(egui::CursorIcon::PointingHand)
    }

    fn sidebar_footer(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        let (dot, text) = match &snap.daemon {
            DaemonState::Running(info) => {
                let starting = info["state"] == "starting";
                (if starting { theme::WARN } else { ACCENT }, if starting { "Daemon starting…".to_string() } else { format!("Daemon {}", info["version"].as_str().unwrap_or("")) })
            }
            DaemonState::Connecting => (TEXT_FAINT, "Connecting…".to_string()),
            DaemonState::NotRunning(_) => (theme::DANGER, "Daemon not running".to_string()),
        };
        let selected = self.view == Some(View::Settings);
        let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 44.0), Sense::click());
        let painter = ui.painter();
        painter.rect_filled(rect, CornerRadius::same(10), if selected { CARD_ACTIVE } else if response.hovered() { CARD_HOVER } else { CARD });
        painter.rect_stroke(rect, CornerRadius::same(10), Stroke::new(1.0_f32, BORDER), StrokeKind::Inside);
        painter.circle_filled(pos2(rect.left() + 18.0, rect.center().y), 4.5, dot);
        painter.text(pos2(rect.left() + 32.0, rect.center().y), egui::Align2::LEFT_CENTER, text, egui::FontId::proportional(13.5), TEXT);
        painter.text(pos2(rect.right() - 16.0, rect.center().y), egui::Align2::RIGHT_CENTER, theme::icon::SETTINGS, theme::icon_font(15.0), TEXT_DIM);
        if response.on_hover_cursor(egui::CursorIcon::PointingHand).on_hover_text("Settings and the daemon").clicked() {
            self.view = Some(View::Settings);
        }
    }

    // -- the main area -----------------------------------------------------
    fn main(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        if self.view == Some(View::Settings) {
            egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| super::settings::page(self, ui, snap));
            return;
        }
        match &snap.daemon {
            DaemonState::Connecting => return empty_state(ui, "Connecting to the daemon…", "", None::<fn(&mut egui::Ui)>),
            DaemonState::NotRunning(reason) => {
                egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| super::setup::page(self, ui, snap, reason));
                return;
            }
            DaemonState::Running(_) => {}
        }
        let Some(device) = self.selected_device(snap).cloned() else {
            let tx = self.tx.clone();
            return empty_state(
                ui,
                "No Razer devices found",
                "Plug one in, switch a wireless one on, or close Razer Synapse if it is running — it holds devices to itself.",
                Some(move |ui: &mut egui::Ui| {
                    if widgets::icon_button(ui, theme::icon::REFRESH, "Look again").clicked() {
                        let _ = tx.send(Command::Rescan);
                    }
                }),
            );
        };
        self.header(ui, &device, snap);
        ui.add_space(6.0);
        let tabs = self.tabs_for(&device, snap);
        if !tabs.contains(&self.tab) {
            self.tab = tabs[0];
        }
        self.tab_bar(ui, &tabs);
        ui.add_space(14.0);
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            // set_max_width sets the width outright; it would widen a narrow window.
            ui.set_max_width(ui.available_width().min(980.0));
            match self.tab {
                Tab::Lighting => super::lighting::page(self, ui, &device, snap),
                Tab::Performance => super::device_pages::performance(self, ui, &device, snap),
                Tab::Power => super::device_pages::power(self, ui, &device, snap),
                Tab::Macros => super::macros::page(self, ui, &device, snap),
                Tab::Profiles => super::profiles::page(self, ui, &device, snap),
                Tab::Info => super::device_pages::info(self, ui, &device, snap),
            }
            ui.add_space(24.0);
        });
    }

    fn tabs_for(&self, device: &Device, snap: &Snapshot) -> Vec<Tab> {
        let detail = snap.details.get(&device.serial);
        let caps = detail.and_then(|d| d.caps.as_ref());
        let mut tabs = vec![];
        if caps.is_none_or(|c| c["zones"].as_object().is_some_and(|z| !z.is_empty())) {
            tabs.push(Tab::Lighting);
        }
        if let Some(caps) = caps {
            if caps["dpi"] == true || caps["poll_rate"] == true {
                tabs.push(Tab::Performance);
            }
            if caps["battery"] == true || caps["idle_time"] == true {
                tabs.push(Tab::Power);
            }
        }
        if let Some(macros) = detail.and_then(|d| d.macros.as_ref()) {
            let keys = macros["keys"].as_array().map_or(0, Vec::len);
            let bound = macros["bindings"].as_object().map_or(0, |b| b.len());
            if keys + bound > 0 {
                tabs.push(Tab::Macros);
            }
        }
        tabs.push(Tab::Profiles);
        tabs.push(Tab::Info);
        tabs
    }

    fn header(&mut self, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
        let detail = snap.details.get(&device.serial);
        ui.horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(vec2(84.0, 84.0), Sense::hover());
            let painter = ui.painter();
            painter.rect_filled(rect, CornerRadius::same(14), Color32::from_rgb(0x1E, 0x23, 0x2C));
            painter.rect_stroke(rect, CornerRadius::same(14), Stroke::new(1.0_f32, BORDER), StrokeKind::Inside);
            match device.image.as_ref().and_then(|url| self.images.get(ui.ctx(), url)) {
                Some(texture) => {
                    let size = fit(texture.size_vec2(), rect.shrink(8.0).size());
                    painter.image(texture.id(), Rect::from_center_size(rect.center(), size), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
                }
                None => widgets::device_glyph(painter, rect.shrink(16.0), &device.kind, TEXT_DIM),
            }
            ui.add_space(8.0);
            ui.vertical(|ui| {
                ui.add_space(6.0);
                ui.label(RichText::new(&device.name).font(theme::bold(24.0)));
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing.x = 6.0;
                    chip(ui, kind_label(&device.kind));
                    chip(ui, &format!("1532:{:04X}", device.pid));
                    if let Some(firmware) = &device.firmware {
                        chip(ui, &format!("Firmware {firmware}"));
                    }
                    if snap.software.contains_key(&device.serial) {
                        chip(ui, "Live effect running");
                    }
                });
            });
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if let Some(level) = detail.and_then(|d| d.battery) {
                    widgets::battery(ui, level, detail.and_then(|d| d.charging).unwrap_or(false));
                }
            });
        });
    }

    fn tab_bar(&mut self, ui: &mut egui::Ui, tabs: &[Tab]) {
        let (bar, _) = ui.allocate_exact_size(vec2(ui.available_width(), 0.0), Sense::hover());
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 24.0;
            for tab in tabs {
                let selected = *tab == self.tab;
                let text = RichText::new(tab.label()).size(15.0);
                let text = if selected { text.font(theme::bold(15.0)).color(TEXT) } else { text.color(TEXT_DIM) };
                let response = ui.add(egui::Label::new(text).sense(Sense::click()));
                if selected {
                    let r = response.rect;
                    ui.painter().rect_filled(Rect::from_min_size(pos2(r.left(), r.bottom() + 6.0), vec2(r.width(), 3.0)), CornerRadius::same(2), ACCENT);
                }
                if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    self.tab = *tab;
                    self.macro_edit = None;
                }
            }
        });
        let y = ui.cursor().top() + 6.0;
        ui.painter().line_segment([pos2(bar.left(), y), pos2(bar.left() + ui.available_width(), y)], Stroke::new(1.0_f32, BORDER));
        ui.add_space(6.0);
    }

    fn toast(&mut self, ctx: &egui::Context, snap: &Snapshot) {
        let Some(toast) = &snap.toast else { return };
        let age = toast.at.elapsed().as_secs_f32();
        let life = if toast.error { 7.0 } else { 3.5 };
        if age > life {
            return;
        }
        let fade = ((life - age) / 0.4).clamp(0.0, 1.0);
        egui::Area::new(egui::Id::new("toast"))
            .anchor(egui::Align2::RIGHT_BOTTOM, vec2(-24.0, -22.0))
            .order(egui::Order::Foreground)
            .show(ctx, |ui| {
                Frame::new()
                    .fill(if toast.error { Color32::from_rgb(0x3A, 0x1C, 0x1F) } else { Color32::from_rgb(0x1D, 0x2B, 0x1E) }.gamma_multiply(fade))
                    .stroke(Stroke::new(1.0_f32, if toast.error { theme::DANGER } else { ACCENT_BORDER }.gamma_multiply(fade)))
                    .corner_radius(CornerRadius::same(10))
                    .inner_margin(Margin::symmetric(16, 12))
                    .show(ui, |ui| {
                        ui.set_max_width(420.0);
                        ui.horizontal(|ui| {
                            let glyph = if toast.error { theme::icon::WARNING } else { theme::icon::CHECK };
                            widgets::icon(ui, glyph, 14.0, (if toast.error { theme::DANGER } else { ACCENT }).gamma_multiply(fade));
                            ui.label(RichText::new(&toast.text).color(TEXT.gamma_multiply(fade)));
                        });
                    });
            });
        ctx.request_repaint_after(Duration::from_millis(50));
    }
}

const ACCENT_BORDER: Color32 = Color32::from_rgb(0x2E, 0x6B, 0x25);

pub fn chip(ui: &mut egui::Ui, text: &str) {
    Frame::new()
        .fill(Color32::from_rgb(0x1E, 0x23, 0x2C))
        .corner_radius(CornerRadius::same(6))
        .inner_margin(Margin::symmetric(8, 3))
        .show(ui, |ui| {
            ui.label(RichText::new(text).size(12.0).color(TEXT_DIM));
        });
}

pub fn kind_label(kind: &str) -> &'static str {
    match kind {
        "keyboard" => "Keyboard",
        "mouse" => "Mouse",
        "headset" => "Headset",
        "mousemat" => "Mousepad",
        "keypad" => "Keypad",
        "core" => "eGPU enclosure",
        "accessory" => "Accessory",
        _ => "Device",
    }
}

fn fit(size: egui::Vec2, bounds: egui::Vec2) -> egui::Vec2 {
    let scale = (bounds.x / size.x).min(bounds.y / size.y);
    size * scale
}

pub fn empty_state(ui: &mut egui::Ui, title: &str, text: &str, action: Option<impl FnOnce(&mut egui::Ui)>) {
    ui.with_layout(Layout::top_down(Align::Center), |ui| {
        ui.add_space(ui.available_height() * 0.28);
        let (rect, _) = ui.allocate_exact_size(vec2(64.0, 64.0), Sense::hover());
        let painter = ui.painter();
        painter.circle_stroke(rect.center(), 28.0, Stroke::new(3.0_f32, Color32::from_rgb(0x2A, 0x30, 0x3B)));
        painter.circle_filled(rect.center(), 8.0, Color32::from_rgb(0x2A, 0x30, 0x3B));
        ui.add_space(10.0);
        ui.label(RichText::new(title).font(theme::bold(20.0)));
        if !text.is_empty() {
            ui.add(egui::Label::new(RichText::new(text).color(TEXT_DIM)).wrap());
        }
        ui.add_space(8.0);
        if let Some(action) = action {
            action(ui);
        }
    });
}
