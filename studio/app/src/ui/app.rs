//! The window: a navigation column on the left -- the app, Home, each
//! device, the tools, Settings and the daemon's state -- and the page beside it.

use super::device::Tab;
use super::edit::{DeviceEdit, Pending};
use crate::appwatch::Rules;
use crate::images::Images;
use crate::settings::Settings;
use crate::shell::{self, Tray, TrayAction};
use crate::worker::{self, Command, DaemonState, Device, Shared, Snapshot};
use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, Frame, Layout, Margin, Rect, RichText, Sense, Stroke, StrokeKind};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER_HI, NAV, SURFACE, SURFACE_HI, TEXT, TEXT_DIM, TEXT_FAINT};
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

/// The width of the navigation column.
const NAV_WIDTH: f32 = 224.0;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Page {
    Home,
    /// One device, the one in `App::selected`.
    Device,
    Macros,
    Integrations,
    Settings,
}

impl Page {
    pub fn title(self) -> &'static str {
        match self {
            Page::Home => t("Home"),
            Page::Device => t("Devices"),
            Page::Macros => t("Macros"),
            Page::Integrations => t("Integrations"),
            Page::Settings => t("Settings"),
        }
    }
    fn glyph(self) -> &'static str {
        match self {
            Page::Home => icon::HOME,
            Page::Device => icon::DEVICES,
            Page::Macros => icon::MACROS,
            Page::Integrations => icon::INTEGRATIONS,
            Page::Settings => icon::SETTINGS,
        }
    }
}

pub struct App {
    pub shared: Shared,
    pub tx: Sender<Command>,
    pub images: Images,
    pub page: Page,
    /// The device the device page shows.
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
    /// The library macro the screenshot hook opened, so it is opened once.
    macros_opened: Option<usize>,
    /// eframe shows the window after its first frame whatever the viewport
    /// asked for, so a background start hides it again for a few frames.
    hide_frames: u8,
}

/// For checking the design: `OPENRAZER_STUDIO_CAPTURE=path.png` saves one
/// screenshot of the window once the daemon has answered, then exits.
/// `OPENRAZER_STUDIO_PAGE` picks the page: `home`, `devices:0:performance`,
/// `lighting:0:studio`, `macros` (`macros:0` with the first library macro
/// open), `integrations`, `settings`.
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
        #[cfg(debug_assertions)]
        if std::env::var_os("OPENRAZER_STUDIO_DEBUG").is_some() {
            cc.egui_ctx.style_mut(|s| {
                s.debug.show_expand_width = true;
                s.debug.show_expand_height = false;
            });
        }
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
            macros_opened: None,
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

    /// Open the page of the device `serial`, on `tab` if given.
    pub fn open_device(&mut self, serial: &str, tab: Option<Tab>) {
        self.page = Page::Device;
        if let Some(tab) = tab {
            self.device_ui.tab = tab;
        }
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

        egui::SidePanel::left("nav")
            .exact_width(NAV_WIDTH)
            .resizable(false)
            .show_separator_line(true)
            .frame(Frame::new().fill(NAV).inner_margin(Margin { left: 12, right: 12, top: 18, bottom: 14 }))
            .show(ctx, |ui| self.nav(ui, &snap));
        egui::CentralPanel::default().frame(Frame::new().fill(theme::BG).inner_margin(Margin { left: 32, right: 28, top: 20, bottom: 0 })).show(ctx, |ui| {
            ui.set_max_width(ui.available_width().min(1480.0));
            self.page(ui, &snap)
        });

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

    // -- the navigation column ------------------------------------------------------------
    fn nav(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        ui.spacing_mut().item_spacing.y = 2.0;
        // Who we are.
        ui.horizontal(|ui| {
            ui.add_space(4.0);
            let (rect, _) = ui.allocate_exact_size(vec2(30.0, 30.0), Sense::hover());
            theme::logo(ui.painter(), rect.center(), 30.0);
            ui.add_space(4.0);
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                ui.label(RichText::new("OpenRazer").font(theme::bold(15.5)).color(TEXT));
                ui.label(RichText::new("STUDIO").font(theme::bold(10.0)).color(ACCENT).extra_letter_spacing(2.6));
            });
        });
        ui.add_space(20.0);
        if nav_item(ui, Page::Home.glyph(), Page::Home.title(), self.page == Page::Home) {
            self.page = Page::Home;
        }

        // The devices, one item each.
        ui.add_space(14.0);
        nav_section(ui, t("Devices"), &snap.devices.len().to_string());
        // What the bottom of the column needs; the devices scroll in the rest.
        let tools = 2.0 * 42.0 + 40.0;
        let bottom = 42.0 + 56.0 + if self.update.lock().unwrap().is_some() { 44.0 } else { 0.0 } + if snap.synapse.is_empty() { 0.0 } else { 20.0 };
        let room = (ui.available_height() - tools - bottom - 24.0).max(52.0);
        egui::ScrollArea::vertical().id_salt("nav-devices").max_height(room).auto_shrink([false, true]).show(ui, |ui| {
            ui.spacing_mut().item_spacing.y = 2.0;
            if snap.devices.is_empty() {
                ui.horizontal(|ui| {
                    ui.add_space(12.0);
                    ui.label(widgets::faint(match snap.daemon {
                        DaemonState::Running(_) => t("None found"),
                        _ => "—",
                    }));
                });
            }
            for device in &snap.devices {
                let selected = self.page == Page::Device && self.selected.as_deref() == Some(device.serial.as_str());
                if self.nav_device(ui, device, snap, selected) {
                    self.open_device(&device.serial.clone(), None);
                }
            }
        });

        ui.add_space(14.0);
        nav_section(ui, t("Tools"), "");
        for page in [Page::Macros, Page::Integrations] {
            if nav_item(ui, page.glyph(), page.title(), self.page == page) {
                self.page = page;
            }
        }

        ui.with_layout(Layout::bottom_up(Align::Min), |ui| {
            ui.spacing_mut().item_spacing.y = 2.0;
            self.daemon_status(ui, snap);
            ui.add_space(8.0);
            if nav_item(ui, Page::Settings.glyph(), Page::Settings.title(), self.page == Page::Settings) {
                self.page = Page::Settings;
            }
            let update = self.update.lock().unwrap().clone();
            if let Some(update) = update {
                let response = ui.add(widgets::Button::primary(&tf("Update {} is out", &[&update.version])).icon(icon::DOWNLOAD).width(ui.available_width()));
                if response.on_hover_text(t("Open the release page")).clicked() {
                    ui.ctx().open_url(egui::OpenUrl::new_tab(update.url));
                }
                ui.add_space(6.0);
            }
        });
    }

    fn nav_device(&mut self, ui: &mut egui::Ui, device: &Device, snap: &Snapshot, selected: bool) -> bool {
        let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 48.0), Sense::click());
        paint_nav_item(ui, rect, selected, response.hovered());
        let thumb = Rect::from_min_size(pos2(rect.left() + 10.0, rect.center().y - 15.0), vec2(38.0, 30.0));
        draw_picture(self, ui, device, thumb);
        let painter = ui.painter_at(rect);
        let x = thumb.right() + 10.0;
        let width = rect.right() - x - 8.0;
        let name = widgets::galley_fit(ui, &title(device, snap), theme::body(13.5), TEXT, width);
        painter.galley(pos2(x, rect.center().y - name.size().y + 1.0), name, if selected || response.hovered() { TEXT } else { TEXT_DIM });
        let detail = snap.details.get(&device.serial);
        let mut line = kind_label(&device.kind).to_string();
        if let Some(level) = detail.and_then(|d| d.battery) {
            line = format!("{line} · {level:.0}%");
        }
        let sub = widgets::galley_fit(ui, &line, theme::body(11.5), TEXT_FAINT, width);
        painter.galley(pos2(x, rect.center().y + 1.0), sub, TEXT_FAINT);
        response.on_hover_cursor(egui::CursorIcon::PointingHand).on_hover_text(&device.name).clicked()
    }

    fn daemon_status(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        let (colour, text) = match &snap.daemon {
            DaemonState::Running(info) if info["state"] == "starting" => (theme::WARN, t("Daemon starting…").to_string()),
            DaemonState::Running(info) => (ACCENT, tf("Daemon {}", &[&info["version"].as_str().unwrap_or("?")])),
            DaemonState::Connecting => (TEXT_FAINT, t("Connecting…").to_string()),
            DaemonState::NotRunning(_) => (theme::DANGER, t("Daemon not running").to_string()),
        };
        let width = ui.available_width();
        ui.allocate_ui_with_layout(vec2(width, 52.0), Layout::top_down(Align::Min), |ui| {
            ui.set_min_size(vec2(width, 52.0));
            ui.spacing_mut().item_spacing.y = 2.0;
            ui.horizontal(|ui| {
                ui.add_space(12.0);
                widgets::status_dot(ui, colour, &text);
                if snap.busy {
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.add_space(8.0);
                        ui.add(egui::Spinner::new().size(12.0).color(TEXT_FAINT));
                    });
                }
            });
            if !snap.synapse.is_empty() {
                ui.horizontal(|ui| {
                    ui.add_space(12.0);
                    widgets::status_dot(ui, theme::WARN, t("Razer Synapse is running"));
                });
            }
            ui.horizontal(|ui| {
                ui.add_space(28.0);
                ui.label(RichText::new(format!("Studio {}", env!("CARGO_PKG_VERSION"))).size(11.5).color(TEXT_FAINT));
            });
        });
        ui.add_space(4.0);
        let (line, _) = ui.allocate_exact_size(vec2(width, 1.0), Sense::hover());
        ui.painter().hline(line.x_range(), line.center().y, Stroke::new(1.0_f32, theme::BORDER));
    }

    // -- the page -----------------------------------------------------------------------
    fn page(&mut self, ui: &mut egui::Ui, snap: &Snapshot) {
        let needs_daemon = matches!(self.page, Page::Home | Page::Device | Page::Macros);
        if needs_daemon {
            match &snap.daemon {
                DaemonState::Connecting => {
                    widgets::page_header(ui, self.page.title(), "", |_| {});
                    widgets::empty_state(ui, t("Connecting to the daemon…"), "", |ui| {
                        ui.add(egui::Spinner::new().size(18.0).color(ACCENT));
                    });
                    return;
                }
                DaemonState::NotRunning(reason) => {
                    let reason = reason.clone();
                    super::setup::page(self, ui, snap, &reason);
                    return;
                }
                DaemonState::Running(_) => {}
            }
        }
        match self.page {
            Page::Home => super::home::page(self, ui, snap),
            Page::Device => super::device::page(self, ui, snap),
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
        egui::Area::new(egui::Id::new("toast")).anchor(Align2::RIGHT_BOTTOM, vec2(-24.0, -24.0)).order(egui::Order::Foreground).show(ctx, |ui| {
            let response = Frame::new()
                .fill(SURFACE_HI.gamma_multiply(fade))
                .stroke(Stroke::new(1.0_f32, BORDER_HI.gamma_multiply(fade)))
                .corner_radius(CornerRadius::same(theme::RADIUS))
                .shadow(theme::shadow())
                .inner_margin(Margin { left: 18, right: 16, top: 12, bottom: 12 })
                .show(ui, |ui| {
                    ui.set_max_width(420.0);
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        let glyph = if toast.error { icon::WARNING } else { icon::CHECK };
                        widgets::icon(ui, glyph, 14.0, colour.gamma_multiply(fade));
                        ui.add(egui::Label::new(RichText::new(&toast.text).color(TEXT.gamma_multiply(fade))).wrap());
                    });
                })
                .response;
            let bar = Rect::from_min_size(response.rect.min + vec2(0.0, 8.0), vec2(3.0, response.rect.height() - 16.0));
            ui.painter().rect_filled(bar, CornerRadius::same(2), colour.gamma_multiply(fade));
        });
        ctx.request_repaint_after(Duration::from_millis(50));
    }

    fn capture(&mut self, ctx: &egui::Context, snap: &Snapshot) {
        let Some(capture) = self.capture.as_ref() else { return };
        let (spec, started, requested, path) = (capture.page.clone(), capture.started, capture.requested, capture.path.clone());
        let settled = match &snap.daemon {
            DaemonState::Running(info) => !snap.devices.is_empty() || info["state"] == "ready",
            _ => snap.start_error.is_some(),
        };
        let ready = settled && !snap.busy && started.elapsed() > Duration::from_secs(6);
        if !requested {
            let parts: Vec<&str> = spec.split(':').collect();
            let page = match parts.first().copied().unwrap_or("home") {
                "devices" | "lighting" => Page::Device,
                "macros" => Page::Macros,
                "integrations" => Page::Integrations,
                "settings" => Page::Settings,
                _ => Page::Home,
            };
            self.page = page;
            if let Some(device) = parts.get(1).and_then(|i| i.parse::<usize>().ok()).and_then(|i| snap.devices.get(i)) {
                self.selected = Some(device.serial.clone());
            }
            if let (Page::Macros, Some(index)) = (page, parts.get(1).and_then(|i| i.parse::<usize>().ok())) {
                if self.macros_opened.is_none() {
                    super::macros::open(self, index);
                    self.macros_opened = Some(index);
                }
            }
            if parts.first() == Some(&"lighting") {
                self.device_ui.tab = Tab::Lighting;
                self.lighting.studio = parts.get(2) == Some(&"studio");
            } else if let (Page::Device, Some(sub)) = (page, parts.get(2)) {
                self.device_ui.tab = Tab::from_name(sub);
            }
            if ready {
                if let Some(capture) = self.capture.as_mut() {
                    capture.requested = true;
                }
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
                let _ = buffer.save(&path);
            }
            quit();
        }
        ctx.request_repaint();
    }
}

/// The fill and green edge of a navigation item.
fn paint_nav_item(ui: &egui::Ui, rect: Rect, selected: bool, hovered: bool) {
    let painter = ui.painter();
    if selected {
        painter.rect_filled(rect, CornerRadius::same(theme::RADIUS_SM), SURFACE_HI);
        painter.rect_filled(Rect::from_min_size(rect.min + vec2(0.0, 8.0), vec2(3.0, rect.height() - 16.0)), CornerRadius::same(2), ACCENT);
    } else if hovered {
        painter.rect_filled(rect, CornerRadius::same(theme::RADIUS_SM), SURFACE);
    }
}

/// An icon and a name in the navigation column. True when clicked.
fn nav_item(ui: &mut egui::Ui, glyph: &str, label: &str, selected: bool) -> bool {
    let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 40.0), Sense::click());
    let hovered = response.hovered();
    paint_nav_item(ui, rect, selected, hovered);
    let painter = ui.painter();
    painter.text(pos2(rect.left() + 22.0, rect.center().y), Align2::CENTER_CENTER, glyph, theme::icon_font(16.0), if selected { ACCENT } else if hovered { TEXT } else { TEXT_DIM });
    let text = widgets::galley_fit(ui, label, theme::body(14.0), TEXT, rect.width() - 52.0);
    painter.galley_with_override_text_color(pos2(rect.left() + 44.0, rect.center().y - text.size().y / 2.0), text, if selected || hovered { TEXT } else { TEXT_DIM });
    response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked()
}

/// A small heading in the navigation column, with a count on the right.
fn nav_section(ui: &mut egui::Ui, title: &str, count: &str) {
    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), 24.0), Sense::hover());
    let text = widgets::galley(ui, &title.to_uppercase(), theme::bold(10.5), TEXT_FAINT, 1.4);
    ui.painter().galley(pos2(rect.left() + 12.0, rect.center().y - text.size().y / 2.0), text, TEXT_FAINT);
    if !count.is_empty() {
        ui.painter().text(pos2(rect.right() - 10.0, rect.center().y), Align2::RIGHT_CENTER, count, theme::bold(10.5), TEXT_FAINT);
    }
}

/// "Razer BlackWidow V3" → "BlackWidow V3": every name starts with Razer.
pub fn short_name(name: &str) -> String {
    name.strip_prefix("Razer ").unwrap_or(name).to_string()
}

/// The name to show for `device`: short, and with the end of its serial when
/// another device here has the same name -- two headsets of one model would
/// otherwise look like one device listed twice.
pub fn title(device: &Device, snap: &Snapshot) -> String {
    let name = short_name(&device.name);
    if snap.devices.iter().filter(|d| d.name == device.name).count() < 2 {
        return name;
    }
    let tail: Vec<char> = device.serial.chars().rev().take(4).collect();
    format!("{name} · {}", tail.into_iter().rev().collect::<String>())
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

/// A scrolling page body, with room at the bottom.
pub fn scroll(ui: &mut egui::Ui, id: &str, add: impl FnOnce(&mut egui::Ui)) {
    egui::ScrollArea::vertical().id_salt(id).auto_shrink([false, false]).show(ui, |ui| {
        ui.spacing_mut().item_spacing.y = theme::GAP;
        add(ui);
        ui.add_space(12.0);
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
        None => super::widgets::device_glyph(&painter, rect.shrink(rect.width().min(rect.height()) * 0.15), &device.kind, TEXT_FAINT),
    }
}

/// A frame in the colour of the canvas: where a device is drawn.
pub fn stage_frame() -> Frame {
    Frame::new().fill(theme::WELL).stroke(Stroke::new(1.0_f32, theme::BORDER)).corner_radius(CornerRadius::same(theme::RADIUS_SM))
}

#[allow(dead_code)]
pub fn outline(ui: &egui::Ui, rect: Rect, colour: Color32) {
    ui.painter().rect_stroke(rect, CornerRadius::same(theme::RADIUS), Stroke::new(1.0_f32, colour), StrokeKind::Inside);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn device(serial: &str, name: &str) -> Device {
        Device { serial: serial.into(), name: name.into(), ..Default::default() }
    }

    #[test]
    fn two_devices_of_one_model_are_told_apart_by_their_serials() {
        let snap = Snapshot {
            devices: vec![device("BLE445ECD583460", "Razer Kraken Kitty V2 BT"), device("BLE445ECD583560", "Razer Kraken Kitty V2 BT"), device("PM1", "Razer Viper")],
            ..Default::default()
        };
        assert_eq!(title(&snap.devices[0], &snap), "Kraken Kitty V2 BT · 3460");
        assert_eq!(title(&snap.devices[1], &snap), "Kraken Kitty V2 BT · 3560");
        assert_eq!(title(&snap.devices[2], &snap), "Viper");
    }
}
