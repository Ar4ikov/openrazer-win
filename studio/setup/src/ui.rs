//! The installer's window.

use crate::ops::{self, Removal};
use crate::{Action, Args};
use egui::{vec2, Align, CornerRadius, Frame, Layout, Margin, RichText, Sense};
use openrazer_studio_core::install::{self, Installed, Options, APP_EXE};
use openrazer_studio_core::theme::{self, icon, ACCENT, NAV, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::widgets::{self, Button};
use openrazer_studio_core::{t, tf};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

#[derive(Clone, PartialEq)]
enum Page {
    Welcome,
    Maintenance,
    ConfirmRemoval,
    Working(&'static str),
    Done { ok: bool, message: String, launch: bool },
}

#[derive(Default)]
struct Shared {
    step: String,
    fraction: f32,
    notes: Vec<String>,
    finished: Option<Result<(), String>>,
}

struct Reporter(Arc<Mutex<Shared>>, egui::Context);

impl ops::Progress for Reporter {
    fn step(&self, text: String, fraction: f32) {
        let mut shared = self.0.lock().unwrap();
        shared.step = text;
        shared.fraction = fraction;
        self.1.request_repaint();
    }
    fn note(&self, text: String) {
        self.0.lock().unwrap().notes.push(text);
        self.1.request_repaint();
    }
}

struct Setup {
    page: Page,
    installed: Option<Installed>,
    dir: String,
    options: Options,
    removal: Removal,
    shared: Arc<Mutex<Shared>>,
    from_temp: bool,
    /// For checking the design: `OPENRAZER_SETUP_CAPTURE=path.png` saves a
    /// screenshot of the first page and exits.
    capture: Option<(PathBuf, u32)>,
}

pub fn run(args: Args) -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(t("OpenRazer Studio Setup"))
            .with_inner_size([720.0, 560.0])
            .with_resizable(false)
            .with_maximize_button(false)
            .with_icon(Arc::new(egui::IconData { rgba: icon_rgba(64), width: 64, height: 64 })),
        ..Default::default()
    };
    eframe::run_native("OpenRazer Studio Setup", options, Box::new(move |cc| {
        theme::apply(&cc.egui_ctx);
        let installed = args.dir.clone().filter(|_| args.from_temp).map(|dir| Installed { version: install::Manifest::read(&dir).map(|m| m.version).unwrap_or_default(), dir }).or_else(install::installed);
        let dir = installed.as_ref().map(|i| i.dir.clone()).or(args.dir.clone()).unwrap_or_else(openrazer_studio_core::paths::default_install_dir);
        let saved = installed.as_ref().and_then(|i| install::Manifest::read(&i.dir)).map(|m| m.options);
        let mut setup = Setup {
            page: if installed.is_some() { Page::Maintenance } else { Page::Welcome },
            installed,
            dir: dir.display().to_string(),
            options: if args.options_given { args.options.clone() } else { saved.unwrap_or_default() },
            removal: args.removal,
            shared: Default::default(),
            from_temp: args.from_temp,
            capture: std::env::var_os("OPENRAZER_SETUP_CAPTURE").map(|p| (PathBuf::from(p), 0)),
        };
        match args.action {
            Some(Action::Uninstall) if setup.installed.is_some() => setup.page = Page::ConfirmRemoval,
            Some(Action::Repair) if setup.installed.is_some() => setup.start(&cc.egui_ctx, "repair"),
            Some(Action::Install) => setup.page = Page::Welcome,
            _ => {}
        }
        Ok(Box::new(setup))
    }))
}

/// The same drawn icon as the app's.
fn icon_rgba(size: u32) -> Vec<u8> {
    let mut pixels = vec![0u8; (size * size * 4) as usize];
    let s = size as f32;
    let c = (s - 1.0) / 2.0;
    let mix = |from: [f32; 3], to: [f32; 3], t: f32| [0, 1, 2].map(|i| from[i] + (to[i] - from[i]) * t);
    for y in 0..size {
        for x in 0..size {
            let d = ((x as f32 - c).powi(2) + (y as f32 - c).powi(2)).sqrt();
            let inside = |radius: f32| (radius - d + 0.5).clamp(0.0, 1.0);
            let mut colour = [18.0, 22.0, 28.0];
            colour = mix(colour, [68.0, 214.0, 44.0], inside(s * 0.44) * (1.0 - inside(s * 0.31)));
            colour = mix(colour, [184.0, 255.0, 168.0], inside(s * 0.15));
            let i = ((y * size + x) * 4) as usize;
            pixels[i..i + 3].copy_from_slice(&colour.map(|v| v as u8));
            pixels[i + 3] = (inside(s * 0.48) * 255.0) as u8;
        }
    }
    pixels
}

impl Setup {
    fn start(&mut self, ctx: &egui::Context, what: &'static str) {
        *self.shared.lock().unwrap() = Shared::default();
        self.page = Page::Working(what);
        let reporter = Reporter(self.shared.clone(), ctx.clone());
        let dir = PathBuf::from(self.dir.trim());
        let options = self.options.clone();
        let removal = self.removal;
        let from_temp = self.from_temp;
        std::thread::spawn(move || {
            let result = match what {
                "uninstall" => ops::uninstall(&dir, removal, &reporter),
                "repair" => ops::install(&dir, &options, true, &reporter),
                _ => ops::install(&dir, &options, false, &reporter),
            };
            if what == "uninstall" && from_temp {
                if let Ok(me) = std::env::current_exe() {
                    ops::delete_after_exit(&me);
                }
            }
            reporter.0.lock().unwrap().finished = Some(result);
            reporter.1.request_repaint();
        });
    }
}

impl eframe::App for Setup {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if let Page::Working(what) = self.page {
            let finished = self.shared.lock().unwrap().finished.take();
            if let Some(result) = finished {
                let (ok, message) = match result {
                    Ok(()) => (
                        true,
                        match what {
                            "uninstall" => t("OpenRazer Studio has been removed.").to_string(),
                            "repair" => t("OpenRazer Studio has been repaired.").to_string(),
                            _ => t("OpenRazer Studio is installed.").to_string(),
                        },
                    ),
                    Err(error) => (false, error),
                };
                self.page = Page::Done { ok, message, launch: ok && what != "uninstall" };
                self.installed = install::installed();
            }
        }

        if let Some((path, frames)) = self.capture.as_mut() {
            *frames += 1;
            if *frames == 20 {
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(Default::default()));
            }
            let shot = ctx.input(|i| i.raw.events.iter().find_map(|e| if let egui::Event::Screenshot { image, .. } = e { Some(image.clone()) } else { None }));
            if let Some(image) = shot {
                let bytes: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
                if let Some(buffer) = image::RgbaImage::from_raw(image.size[0] as u32, image.size[1] as u32, bytes) {
                    let _ = buffer.save(&*path);
                }
                std::process::exit(0);
            }
            ctx.request_repaint();
        }

        // Who we are, along the top.
        egui::TopBottomPanel::top("header").exact_height(76.0).show_separator_line(true).frame(Frame::new().fill(NAV).inner_margin(Margin::symmetric(24, 0))).show(ctx, |ui| {
            ui.horizontal_centered(|ui| {
                ui.spacing_mut().item_spacing.x = 12.0;
                let (rect, _) = ui.allocate_exact_size(vec2(36.0, 36.0), Sense::hover());
                theme::logo(ui.painter(), rect.center(), 36.0);
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 0.0;
                    ui.add_space(16.0);
                    ui.label(RichText::new("OpenRazer Studio").font(theme::bold(18.0)).color(TEXT));
                    ui.label(RichText::new(tf("Setup · version {}", &[&ops::VERSION])).size(12.5).color(TEXT_DIM));
                });
            });
        });
        // What to do next, along the bottom, the main action on the right.
        egui::TopBottomPanel::bottom("footer").exact_height(64.0).show_separator_line(true).frame(Frame::new().fill(NAV).inner_margin(Margin::symmetric(24, 0))).show(ctx, |ui| {
            ui.horizontal_centered(|ui| self.footer(ui));
        });
        egui::CentralPanel::default().frame(Frame::new().fill(theme::BG).inner_margin(Margin { left: 24, right: 24, top: 20, bottom: 0 })).show(ctx, |ui| {
            egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
                ui.spacing_mut().item_spacing.y = 12.0;
                match self.page.clone() {
                    Page::Welcome => self.welcome(ui),
                    Page::Maintenance => self.maintenance(ui),
                    Page::ConfirmRemoval => self.confirm_removal(ui),
                    Page::Working(what) => self.working(ui, what),
                    Page::Done { ok, message, .. } => self.done(ui, ok, &message),
                }
                ui.add_space(12.0);
            });
        });
    }
}

impl Setup {
    fn footer(&mut self, ui: &mut egui::Ui) {
        ui.label(RichText::new(t("GPL-2.0-or-later. Not affiliated with Razer Inc.")).size(12.0).color(TEXT_FAINT));
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            match self.page.clone() {
                Page::Welcome => {
                    let ready = ops::has_payload() && !self.dir.trim().is_empty();
                    if ui.add(Button::primary(t("Install")).icon(icon::DOWNLOAD).min_width(130.0).enabled(ready)).clicked() {
                        let ctx = ui.ctx().clone();
                        self.start(&ctx, "install");
                    }
                    if self.installed.is_some() && ui.add(Button::secondary(t("Back")).icon(icon::BACK).min_width(100.0)).clicked() {
                        self.page = Page::Maintenance;
                    }
                }
                Page::Maintenance => {
                    if ui.add(Button::secondary(t("Close")).min_width(100.0)).clicked() {
                        ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                }
                Page::ConfirmRemoval => {
                    if ui.add(Button::danger(t("Uninstall")).icon(icon::DELETE).min_width(130.0)).clicked() {
                        let ctx = ui.ctx().clone();
                        self.start(&ctx, "uninstall");
                    }
                    if ui.add(Button::secondary(t("Back")).icon(icon::BACK).min_width(100.0)).clicked() {
                        self.page = if self.installed.is_some() { Page::Maintenance } else { Page::Welcome };
                    }
                }
                Page::Working(_) => {
                    ui.add(Button::secondary(t("Close")).min_width(100.0).enabled(false));
                }
                Page::Done { ok, launch, .. } => {
                    if launch && ui.add(Button::primary(t("Open OpenRazer Studio")).icon(icon::OPEN)).clicked() {
                        let _ = std::process::Command::new(PathBuf::from(self.dir.trim()).join(APP_EXE)).spawn();
                        ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                    if ui.add(Button::secondary(t("Close")).min_width(100.0)).clicked() {
                        ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                    if !ok && ui.add(Button::secondary(t("Try again")).icon(icon::REFRESH)).clicked() {
                        self.page = if self.installed.is_some() { Page::Maintenance } else { Page::Welcome };
                    }
                }
            }
        });
    }

    fn welcome(&mut self, ui: &mut egui::Ui) {
        if !ops::has_payload() {
            widgets::banner(ui, theme::DANGER, icon::WARNING, t("Nothing to install"), t("This copy of the installer was built without the program inside it. Download the installer from the releases page."), |ui| {
                widgets::link_button(ui, t("Releases"), install::RELEASES);
            });
        }
        widgets::help(ui, t("Razer device control without Synapse: lighting, DPI, macros and profiles. Installed for you alone, so it never asks for administrator rights, and it can be repaired or removed cleanly at any time."));
        ui.add_enabled_ui(ops::has_payload(), |ui| {
            widgets::panel(ui, t("Install to"), |ui| {
                ui.horizontal(|ui| {
                    widgets::text_field(ui, &mut self.dir, "", ui.available_width() - 120.0);
                    if ui.add(Button::secondary(t("Browse")).icon(icon::FOLDER).width(112.0)).clicked() {
                        if let Some(dir) = rfd_pick() {
                            self.dir = dir.join(install::APP_NAME).display().to_string();
                        }
                    }
                });
            });
            widgets::panel(ui, t("Options"), |ui| self.options(ui));
        });
    }

    fn options(&mut self, ui: &mut egui::Ui) {
        widgets::toggle_row(ui, t("Start menu entry"), t("Plus one to repair or remove Studio."), &mut self.options.start_menu);
        widgets::divider(ui);
        widgets::toggle_row(ui, t("Desktop shortcut"), "", &mut self.options.desktop);
        widgets::divider(ui);
        widgets::toggle_row(ui, t("Start with Windows"), t("In the tray, keeping your devices set up."), &mut self.options.autostart);
        widgets::divider(ui);
        widgets::toggle_row(ui, t("Command line in PATH"), t("For openrazer-win in a terminal."), &mut self.options.path);
    }

    fn maintenance(&mut self, ui: &mut egui::Ui) {
        let Some(installed) = self.installed.clone() else {
            self.page = Page::Welcome;
            return;
        };
        widgets::help(ui, &tf("Version {} is installed in {}", &[&installed.version, &installed.dir.display()]));
        let newer = !installed.version.is_empty() && installed.version != ops::VERSION;
        let choice = |ui: &mut egui::Ui, glyph: &str, title: &str, text: &str, danger: bool| -> bool {
            let (response, _) = widgets::list_row(ui, false, 68.0, |ui| {
                ui.spacing_mut().item_spacing.x = 14.0;
                let (rect, _) = ui.allocate_exact_size(vec2(40.0, 40.0), Sense::hover());
                ui.painter().rect_filled(rect, CornerRadius::same(theme::RADIUS_SM), theme::WELL);
                ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER, glyph, theme::icon_font(17.0), if danger { theme::DANGER } else { ACCENT });
                let room = ui.available_width() - 30.0;
                ui.allocate_ui_with_layout(vec2(room, 56.0), Layout::top_down(Align::Min), |ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    ui.add_space(8.0);
                    ui.label(RichText::new(title).font(theme::bold(15.0)).color(TEXT));
                    ui.add(egui::Label::new(RichText::new(text).size(12.5).color(TEXT_DIM)).truncate());
                });
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    widgets::icon(ui, icon::CHEVRON_RIGHT, 12.0, TEXT_FAINT);
                });
            });
            response.clicked()
        };
        if ops::has_payload() {
            let (title, text) = if newer {
                (tf("Update to {}", &[&ops::VERSION]), t("Replaces the program and keeps every setting, profile and macro.").to_string())
            } else {
                (t("Reinstall").to_string(), t("Writes every file again, with the options below.").to_string())
            };
            if choice(ui, icon::DOWNLOAD, &title, &text, false) {
                self.page = Page::Welcome;
            }
            if choice(ui, icon::WRENCH, t("Repair"), t("Puts back missing or damaged files, the Start menu entry and starting with Windows, and sets aside settings files that no longer read. Profiles and macros are kept."), false) {
                let ctx = ui.ctx().clone();
                self.start(&ctx, "repair");
            }
        }
        if choice(ui, icon::DELETE, t("Uninstall"), t("Removes the program, its shortcuts and its entry in Windows. You choose whether your settings go too."), true) {
            self.page = Page::ConfirmRemoval;
        }
    }

    fn confirm_removal(&mut self, ui: &mut egui::Ui) {
        ui.label(RichText::new(t("Remove OpenRazer Studio")).font(theme::bold(17.0)).color(TEXT));
        widgets::help(ui, &tf("The program in {} and its shortcuts go. Your devices keep whatever lighting they show until they lose power.", &[&self.dir]));
        widgets::panel(ui, t("Also remove"), |ui| {
            widgets::toggle_row(ui, t("Also remove Studio's settings"), t("The macro library, links between apps and profiles, and window settings."), &mut self.removal.settings);
            widgets::divider(ui);
            widgets::toggle_row(ui, t("Also remove everything the daemon keeps"), t("Device settings, profiles, macro key bindings and logs, shared with the classic control panel and the command line."), &mut self.removal.all_data);
        });
        if self.removal.all_data {
            self.removal.settings = true;
        }
    }

    fn working(&mut self, ui: &mut egui::Ui, what: &str) {
        let shared = self.shared.lock().unwrap();
        let title = match what {
            "uninstall" => t("Removing"),
            "repair" => t("Repairing"),
            _ => t("Installing"),
        };
        widgets::panel(ui, title, |ui| {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().size(16.0).color(ACCENT));
                ui.label(RichText::new(&shared.step).color(TEXT));
            });
            ui.add(egui::ProgressBar::new(shared.fraction).desired_height(8.0).fill(ACCENT));
            for note in &shared.notes {
                ui.label(widgets::faint(format!("· {note}")));
            }
        });
        ui.ctx().request_repaint_after(std::time::Duration::from_millis(100));
    }

    fn done(&mut self, ui: &mut egui::Ui, ok: bool, message: &str) {
        let colour = if ok { ACCENT } else { theme::DANGER };
        widgets::banner(ui, colour, if ok { icon::CHECK } else { icon::WARNING }, message, "", |ui| {
            if !ok {
                ui.label(widgets::faint(tf("The details are also in {}", &[&std::env::temp_dir().join("openrazer-studio-setup.log").display()])));
            }
        });
        let notes = self.shared.lock().unwrap().notes.clone();
        if !notes.is_empty() {
            widgets::panel(ui, t("What was done"), |ui| {
                for note in notes {
                    ui.label(widgets::dim(format!("· {note}")));
                }
            });
        } else if ok && message == t("OpenRazer Studio has been repaired.") {
            widgets::help(ui, t("Nothing needed fixing: every file was as it should be."));
        }
    }
}

fn rfd_pick() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        // A folder picker without pulling in a dialog crate: PowerShell's own.
        let script = "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }";
        let mut command = std::process::Command::new("powershell.exe");
        command.args(["-NoProfile", "-STA", "-Command", script]);
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
        let output = command.output().ok()?;
        let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
        (!path.is_empty()).then(|| PathBuf::from(path))
    }
    #[cfg(not(windows))]
    None
}
