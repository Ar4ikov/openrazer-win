//! The window: a rail of sections on the left, a bar of devices along the
//! top, the page in between, and the daemon's state along the bottom.

use super::edit::{DeviceEdit, Pending};
use crate::appwatch::Rules;
use crate::images::Images;
use crate::settings::Settings;
use crate::shell::{self, Tray, TrayAction};
use crate::worker::{self, Command, DaemonState, Device, Shared, Snapshot};
use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, Frame, Layout, Margin, Rect, RichText, Sense, Stroke, StrokeKind};
use openrazer_studio_core::theme::{self, icon, ACCENT, BAR, BG, BORDER, RAIL, SURFACE_HI, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::{install, t, tf, widgets};
use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

thread_local! {
    /// The tray icon lives on the UI thread; kept here so Quit can take it
    /// down before the process exits rather than leave a ghost in the tray.
    static TRAY: RefCell<Option<Tray>> = const { RefCell::new(None) };
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Page {
    Home,
    Devices,
    Lighting,
    Macros,
    Integrations,
    Settings,
}

impl Page {
    fn title(self) -> &'static str {
        match self {
            Page::Home => t("Home"),
            Page::Devices => t("Devices"),
            Page::Lighting => t("Lighting"),
            Page::Macros => t("Macros"),
            Page::Integrations => t("Integrations"),
            Page::Settings => t("Settings"),
        }
    }
    fn glyph(self) -> &'static str {
        match self {
            Page::Home => icon::HOME,
            Page::Devices => icon::DEVICES,
            Page::Lighting => icon::LIGHTING,
            Page::Macros => icon::MACROS,
            Page::Integrations => icon::INTEGRATIONS,
            Page::Settings => icon::SETTINGS,
        }
    }
    /// Whether the page is about one device, chosen in the bar along the top.
    fn per_device(self) -> bool {
        matches!(self, Page::Devices | Page::Lighting)
    }
}

pub struct App {
    pub shared: Shared,
    pub tx: Sender<Command>,
    pub images: Images,
    pub page: Page,
    /// The device the per-device pages show.
    pub selected: Option<String>,
    pub edits: HashMap<String, DeviceEdit>,
    pending: Vec<Pending>,
    pub settings: Settings,
    /// A copy for the thread that switches profiles by app.
    rules: Rules,
    serials: Arc<Mutex<Vec<String>>>,
    pub autostart: bool,
    pub device_ui: super::device::DeviceUi,
    pub lighting: super::lighting::LightingUi,
    pub macros: super::macros::MacrosUi,
    pub arts: super::canvas::Arts,
    pub update: crate::updates::Found,
    pub installed: Option<install::Installed>,
    pub started: Instant,
    tray_ok: bool,
    capture: Option<Capture>,
    /// eframe shows the window after its first frame whatever the viewport
    /// asked for, so a background start hides it again for a few frames.
    hide_frames: u8,
}

/// For checking the design: `OPENRAZER_STUDIO_CAPTURE=path.png` saves one
/// screenshot of the window once the daemon has answered, then exits.
/// `OPENRAZER_STUDIO_PAGE` picks the page: `home`, `devices:0:performance`,
/// `lighting:0:studio`, `macros`, `integrations`, `settings`.
struct Capture {
    path: std::path::PathBuf,
    page: String,
    started: Instant,
    requested: bool,
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>, background: bool) -> App {
        let settings = Settings::load();
        openrazer_studio_core::i18n::set(settings.language);
        theme::apply(&cc.egui_ctx);
        let shared: Shared = Default::default();
        let ctx = cc.egui_ctx.clone();
        let tx = worker::spawn(shared.clone(), move || ctx.request_repaint());
        // Studio is how most people start the daemon; do it for them.
        let _ = tx.send(Command::StartDaemon);
        crate::daemon::refresh_autostart();
        openrazer_studio_core::process::listen_for_quit(quit);

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

        let rules: Rules = Arc::new(Mutex::new(settings.clone()));
        let serials = Arc::new(Mutex::new(Vec::new()));
        crate::appwatch::spawn(rules.clone(), tx.clone(), serials.clone());
        let update: crate::updates::Found = Default::default();
        let capture = std::env::var_os("OPENRAZER_STUDIO_CAPTURE").map(|path| Capture {
            path: path.into(),
            page: std::env::var("OPENRAZER_STUDIO_PAGE").unwrap_or_default(),
            started: Instant::now(),
            requested: false,
        });
        if settings.check_updates && capture.is_none() {
            crate::updates::check(update.clone(), cc.egui_ctx.clone());
        }

        App {
            shared,
            tx,
            images: Images::default(),
            page: Page::Home,
            selected: None,
            edits: HashMap::new(),
            pending: Vec::new(),
            settings,
            rules,
            serials,
            autostart: crate::daemon::autostart_enabled(),
            device_ui: Default::default(),
            lighting: Default::default(),
            macros: Default::default(),
            arts: Default::default(),
            update,
            installed: install::installed(),
            started: Instant::now(),
            tray_ok,
            hide_frames: if background && tray_ok { 4 } else { 0 },
            capture,
        }
    }

    pub fn send(&self, command: Command) {
        let _ = self.tx.send(command);
    }

    pub fn toast(&self, text: impl Into<String>, error: bool) {
        self.shared.lock().unwrap().toast = Some(worker::Toast { text: text.into(), error, at: Instant::now() });
    }

    /// Save the settings, and hand the app-switching thread its copy.
    pub fn save_settings(&mut self) {
        self.settings.save();
        *self.rules.lock().unwrap() = self.settings.clone();
    }

    /// Seconds since the window opened, for animations.
    pub fn time(&self) -> f32 {
        self.started.elapsed().as_secs_f32()
    }

    pub fn device<'a>(&self, snap: &'a Snapshot) -> Option<&'a Device> {
        let serial = self.selected.as_ref()?;
        snap.devices.iter().find(|d| &d.serial == serial)
    }

    /// Open `page` about the device `serial`.
    pub fn open(&mut self, page: Page, serial: &str) {
        self.page = page;
        if self.selected.as_deref() != Some(serial) {
            self.selected = Some(serial.to_string());
            // Re-read what may have changed on the device itself.
            self.send(Command::LoadDevice(serial.to_string()));
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

    /// Whether a command queued under `key` is still waiting to go.
    pub fn is_pending(&self, key: &str) -> bool {
        self.pending.iter().any(|p| p.key == key)
    }

    /// The picture of `device`: of the colour its owner said it is, if it comes in several.
    pub fn picture(&self, device: &Device) -> Option<String> {
        let chosen = self.settings.colours.get(&device.serial);
        device.images.iter().find(|(colour, _)| Some(colour) == chosen).map(|(_, url)| url.clone()).or_else(|| device.image.clone())
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
        if ctx.input(|i| i.viewport().close_requested()) && self.settings.close_to_tray && self.tray_ok && self.capture.is_none() {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            shell::hide();
        }
        let snap = self.shared.lock().unwrap().clone();
        self.sync(&snap);
        self.flush_pending(ctx);

        // One backdrop behind everything; the panels over it are see-through.
        theme::backdrop(&ctx.layer_painter(egui::LayerId::background()), ctx.screen_rect());
        egui::SidePanel::left("rail")
            .exact_width(88.0)
            .resizable(false)
            .show_separator_line(false)
            .frame(Frame::new().fill(RAIL.gamma_multiply(0.92)).inner_margin(Margin::symmetric(0, 16)))
            .show(ctx, |ui| self.rail(ui, &snap));
        egui::TopBottomPanel::bottom("status").exact_height(30.0).show_separator_line(false).frame(Frame::new().inner_margin(Margin::symmetric(30, 0))).show(ctx, |ui| self.status(ui, &snap));
        egui::TopBottomPanel::top("bar").exact_height(78.0).show_separator_line(false).frame(Frame::new().inner_margin(Margin { left: 34, right: 30, top: 0, bottom: 0 })).show(ctx, |ui| self.bar(ui, &snap));
        egui::CentralPanel::default().frame(Frame::new().inner_margin(Margin { left: 34, right: 30, top: 10, bottom: 6 })).show(ctx, |ui| self.page(ui, &snap));
        let _ = (BG, BAR);

        self.toast_area(ctx, &snap);
        self.capture(ctx, &snap);
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        TRAY.with(|slot| drop(slot.borrow_mut().take()));
    }
}

impl App {
    /// Keep the selection pointing at something that exists, and edits current.
    fn sync(&mut self, snap: &Snapshot) {
        if self.selected.as_ref().is_none_or(|s| !snap.devices.iter().any(|d| &d.serial == s)) {
            self.selected = snap.devices.first().map(|d| d.serial.clone());
        }
        for device in &snap.devices {
            let detail = snap.details.get(&device.serial);
            let edit = self.edits.entry(device.serial.clone()).or_default();
            if let Some(detail) = detail {
                edit.refresh(device, detail, snap);
            }
        }
        *self.serials.lock().unwrap() = snap.devices.iter().map(|d| d.serial.clone()).collect();
    }

    // -- the rail ----------------------------------------------------------------
    fn rail(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        ui.vertical_centered(|ui| {
            let (rect, _) = ui.allocate_exact_size(vec2(40.0, 40.0), Sense::hover());
            let painter = ui.painter();
            theme::glow(painter, rect.center(), vec2(30.0, 30.0), ACCENT.gamma_multiply(0.35));
            painter.circle_stroke(rect.center(), 14.0, Stroke::new(4.5_f32, ACCENT));
            painter.circle_filled(rect.center(), 5.5, Color32::from_rgb(0xC8, 0xFF, 0xB8));
            ui.add_space(22.0);
            for page in [Page::Home, Page::Devices, Page::Lighting, Page::Macros, Page::Integrations] {
                self.rail_item(ui, page);
            }
        });
        ui.with_layout(Layout::bottom_up(Align::Center), |ui| {
            let (colour, hint) = match &snap.daemon {
                DaemonState::Running(info) if info["state"] == "starting" => (theme::WARN, t("Daemon starting…")),
                DaemonState::Running(_) => (ACCENT, t("Daemon running")),
                DaemonState::Connecting => (TEXT_FAINT, t("Connecting…")),
                DaemonState::NotRunning(_) => (theme::DANGER, t("Daemon not running")),
            };
            let (rect, response) = ui.allocate_exact_size(vec2(20.0, 20.0), Sense::hover());
            ui.painter().circle_filled(rect.center(), 4.5, colour);
            response.on_hover_text(hint);
            ui.add_space(6.0);
            self.rail_item(ui, Page::Settings);
        });
    }

    fn rail_item(&mut self, ui: &mut egui::Ui, page: Page) {
        let selected = self.page == page;
        let (slot, response) = ui.allocate_exact_size(vec2(88.0, 64.0), Sense::click());
        let rect = Rect::from_center_size(slot.center(), vec2(68.0, 58.0));
        let painter = ui.painter();
        let hover = ui.ctx().animate_bool_responsive(response.id, response.hovered() || selected);
        if selected {
            theme::glow(painter, rect.center() - vec2(0.0, 8.0), vec2(34.0, 26.0), ACCENT.gamma_multiply(0.22));
            painter.rect_filled(rect, CornerRadius::same(16), theme::ACCENT_DEEP.gamma_multiply(0.9));
            painter.rect_stroke(rect, CornerRadius::same(16), Stroke::new(1.0_f32, ACCENT.gamma_multiply(0.35)), StrokeKind::Inside);
        } else if hover > 0.0 {
            painter.rect_filled(rect, CornerRadius::same(16), SURFACE_HI.gamma_multiply(hover));
        }
        let colour = if selected { theme::ACCENT_HI } else if response.hovered() { TEXT } else { TEXT_DIM };
        painter.text(rect.center() - vec2(0.0, 8.0), Align2::CENTER_CENTER, page.glyph(), theme::icon_font(20.0), colour);
        painter.text(rect.center() + vec2(0.0, 15.0), Align2::CENTER_CENTER, page.title(), theme::bold(10.5), if selected { TEXT } else { TEXT_FAINT });
        if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
            self.page = page;
        }
    }

    // -- the bar along the top ----------------------------------------------------------
    fn bar(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        ui.horizontal_centered(|ui| {
            ui.label(RichText::new(self.page.title()).font(theme::heavy(28.0)));
            ui.add_space(26.0);
            if self.page.per_device() && !snap.devices.is_empty() {
                ui.spacing_mut().item_spacing.x = 8.0;
                for device in &snap.devices {
                    let selected = self.selected.as_deref() == Some(device.serial.as_str());
                    if self.device_chip(ui, device, selected).clicked() {
                        let page = self.page;
                        self.open(page, &device.serial.clone());
                    }
                }
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if snap.busy {
                    ui.add(egui::Spinner::new().size(14.0).color(TEXT_FAINT));
                }
                let update = self.update.lock().unwrap().clone();
                if let Some(update) = update {
                    let response = ui.add(egui::Button::new(RichText::new(tf("Update {} is out", &[&update.version])).font(theme::bold(12.0)).color(theme::ON_ACCENT)).fill(ACCENT));
                    if response.on_hover_text(t("Open the release page")).clicked() {
                        ui.ctx().open_url(egui::OpenUrl::new_tab(update.url));
                    }
                }
                if self.page.per_device() {
                    if let Some(device) = self.device(snap).cloned() {
                        super::device::profile_switcher(self, ui, &device, snap);
                    }
                }
            });
        });
    }

    fn device_chip(&mut self, ui: &mut egui::Ui, device: &Device, selected: bool) -> egui::Response {
        let name = short_name(&device.name);
        let galley = ui.painter().layout_no_wrap(name, theme::bold(12.5), TEXT);
        let (rect, response) = ui.allocate_exact_size(vec2(galley.size().x + 58.0, 42.0), Sense::click());
        let painter = ui.painter_at(rect.expand(12.0));
        if selected {
            theme::glow(&painter, rect.center(), vec2(rect.width() * 0.6, 26.0), ACCENT.gamma_multiply(0.14));
            painter.rect_filled(rect, CornerRadius::same(255), SURFACE_HI);
            painter.rect_stroke(rect, CornerRadius::same(255), Stroke::new(1.0_f32, ACCENT.gamma_multiply(0.6)), StrokeKind::Inside);
        } else {
            painter.rect_filled(rect, CornerRadius::same(255), if response.hovered() { SURFACE_HI } else { theme::SURFACE.gamma_multiply(0.7) });
            painter.rect_stroke(rect, CornerRadius::same(255), Stroke::new(1.0_f32, BORDER), StrokeKind::Inside);
        }
        let thumb = Rect::from_min_size(rect.min + vec2(10.0, 5.0), vec2(32.0, 32.0));
        match self.picture(device).and_then(|url| self.images.get(ui.ctx(), &url)) {
            Some(texture) => {
                let size = fit(texture.size_vec2(), thumb.size());
                painter.image(texture.id(), Rect::from_center_size(thumb.center(), size), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
            }
            None => super::widgets::device_glyph(&painter, thumb.shrink(4.0), &device.kind, TEXT_DIM),
        }
        painter.galley(pos2(thumb.right() + 6.0, rect.center().y - galley.size().y / 2.0), galley, if selected { TEXT } else { TEXT_DIM });
        response.on_hover_cursor(egui::CursorIcon::PointingHand).on_hover_text(&device.name)
    }

    // -- the status line ----------------------------------------------------------------
    fn status(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        ui.horizontal_centered(|ui| {
            ui.spacing_mut().item_spacing.x = 14.0;
            let text = match &snap.daemon {
                DaemonState::Running(info) => tf("Daemon {} · {} device(s)", &[&info["version"].as_str().unwrap_or("?"), &snap.devices.len()]),
                DaemonState::Connecting => t("Connecting to the daemon…").to_string(),
                DaemonState::NotRunning(_) => t("Daemon not running").to_string(),
            };
            ui.label(RichText::new(text).size(11.5).color(TEXT_FAINT));
            if !snap.synapse.is_empty() {
                ui.label(RichText::new(t("Razer Synapse is running")).size(11.5).color(theme::WARN));
            }
            if let Some(effects) = Some(snap.software.len()).filter(|n| *n > 0) {
                ui.label(RichText::new(tf("{} live effect(s)", &[&effects])).size(11.5).color(TEXT_FAINT));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.label(RichText::new(format!("OpenRazer Studio {}", env!("CARGO_PKG_VERSION"))).size(11.5).color(TEXT_FAINT));
            });
        });
    }

    // -- the page -----------------------------------------------------------------------
    fn page(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        let needs_daemon = matches!(self.page, Page::Home | Page::Devices | Page::Lighting | Page::Macros);
        if needs_daemon {
            match &snap.daemon {
                DaemonState::Connecting => return empty_state(ui, t("Connecting to the daemon…"), "", None::<fn(&mut egui::Ui)>),
                DaemonState::NotRunning(reason) => {
                    let reason = reason.clone();
                    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| super::setup::page(self, ui, snap, &reason));
                    return;
                }
                DaemonState::Running(_) => {}
            }
        }
        match self.page {
            Page::Home => super::home::page(self, ui, snap),
            Page::Devices => super::device::page(self, ui, snap),
            Page::Lighting => super::lighting::page(self, ui, snap),
            Page::Macros => super::macros::page(self, ui, snap),
            Page::Integrations => super::integrations::page(self, ui, snap),
            Page::Settings => super::settings::page(self, ui, snap),
        }
    }

    fn toast_area(&mut self, ctx: &egui::Context, snap: &Snapshot) {
        let Some(toast) = &snap.toast else { return };
        let age = toast.at.elapsed().as_secs_f32();
        let life = if toast.error { 7.0 } else { 3.5 };
        if age > life {
            return;
        }
        let fade = ((life - age) / 0.4).clamp(0.0, 1.0);
        let colour = if toast.error { theme::DANGER } else { ACCENT };
        egui::Area::new(egui::Id::new("toast")).anchor(Align2::RIGHT_BOTTOM, vec2(-24.0, -40.0)).order(egui::Order::Foreground).show(ctx, |ui| {
            Frame::new()
                .fill(theme::SURFACE_HI.gamma_multiply(fade))
                .stroke(Stroke::new(1.0_f32, colour.gamma_multiply(0.6 * fade)))
                .corner_radius(CornerRadius::same(theme::RADIUS))
                .inner_margin(Margin::symmetric(16, 11))
                .show(ui, |ui| {
                    ui.set_max_width(440.0);
                    ui.horizontal(|ui| {
                        let glyph = if toast.error { icon::WARNING } else { icon::CHECK };
                        widgets::icon(ui, glyph, 14.0, colour.gamma_multiply(fade));
                        ui.add(egui::Label::new(RichText::new(&toast.text).color(TEXT.gamma_multiply(fade))).wrap());
                    });
                });
        });
        ctx.request_repaint_after(Duration::from_millis(50));
    }

    fn capture(&mut self, ctx: &egui::Context, snap: &Snapshot) {
        let Some(capture) = self.capture.as_mut() else { return };
        let settled = match &snap.daemon {
            DaemonState::Running(info) => !snap.devices.is_empty() || info["state"] == "ready",
            _ => snap.start_error.is_some(),
        };
        let ready = settled && !snap.busy && capture.started.elapsed() > Duration::from_secs(6);
        if !capture.requested {
            let parts: Vec<&str> = capture.page.split(':').collect();
            let page = match parts.first().copied().unwrap_or("home") {
                "devices" => Page::Devices,
                "lighting" => Page::Lighting,
                "macros" => Page::Macros,
                "integrations" => Page::Integrations,
                "settings" => Page::Settings,
                _ => Page::Home,
            };
            self.page = page;
            if let Some(device) = parts.get(1).and_then(|i| i.parse::<usize>().ok()).and_then(|i| snap.devices.get(i)) {
                self.selected = Some(device.serial.clone());
            }
            if let Some(sub) = parts.get(2) {
                match page {
                    Page::Devices => self.device_ui.tab = super::device::Tab::from_name(sub),
                    Page::Lighting => self.lighting.studio = *sub == "studio",
                    _ => {}
                }
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
}

/// "Razer BlackWidow V3" → "BlackWidow V3": every name starts with Razer.
pub fn short_name(name: &str) -> String {
    name.strip_prefix("Razer ").unwrap_or(name).to_string()
}

pub fn kind_label(kind: &str) -> &'static str {
    match kind {
        "keyboard" => t("Keyboard"),
        "mouse" => t("Mouse"),
        "headset" => t("Headset"),
        "mousemat" => t("Mousepad"),
        "keypad" => t("Keypad"),
        "core" => t("eGPU enclosure"),
        "accessory" => t("Accessory"),
        _ => t("Device"),
    }
}

pub fn fit(size: egui::Vec2, bounds: egui::Vec2) -> egui::Vec2 {
    let scale = (bounds.x / size.x).min(bounds.y / size.y);
    size * scale
}

pub fn empty_state(ui: &mut egui::Ui, title: &str, text: &str, action: Option<impl FnOnce(&mut egui::Ui)>) {
    ui.with_layout(Layout::top_down(Align::Center), |ui| {
        ui.add_space(ui.available_height() * 0.26);
        let (rect, _) = ui.allocate_exact_size(vec2(64.0, 64.0), Sense::hover());
        let painter = ui.painter();
        painter.circle_stroke(rect.center(), 28.0, Stroke::new(3.0_f32, BORDER));
        painter.circle_filled(rect.center(), 8.0, BORDER);
        ui.add_space(10.0);
        ui.label(RichText::new(title.to_uppercase()).font(theme::heavy(18.0)).extra_letter_spacing(1.0));
        if !text.is_empty() {
            ui.set_max_width(520.0);
            ui.add(egui::Label::new(RichText::new(text).color(TEXT_DIM)).wrap());
        }
        ui.add_space(8.0);
        if let Some(action) = action {
            action(ui);
        }
    });
}

/// A device picture, fitted into `rect`; a drawing of its kind until the picture arrives.
pub fn draw_picture(app: &mut App, ui: &egui::Ui, device: &Device, rect: Rect) {
    let painter = ui.painter_at(rect);
    match app.picture(device).and_then(|url| app.images.get(ui.ctx(), &url)) {
        Some(texture) => {
            let size = fit(texture.size_vec2(), rect.size());
            painter.image(texture.id(), Rect::from_center_size(rect.center(), size), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
        }
        None => super::widgets::device_glyph(&painter, rect.shrink(rect.width() * 0.2), &device.kind, TEXT_FAINT),
    }
}

#[allow(dead_code)]
pub fn outline(ui: &egui::Ui, rect: Rect, colour: Color32) {
    ui.painter().rect_stroke(rect, CornerRadius::same(theme::RADIUS), Stroke::new(1.0_f32, colour), StrokeKind::Inside);
}
