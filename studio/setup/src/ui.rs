//! The installer's window.

use crate::ops::{self, Removal};
use crate::{Action, Args};
use egui::{vec2, Align, CornerRadius, Frame, Layout, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::install::{self, Installed, Options, APP_EXE};
use openrazer_studio_core::theme::{self, icon, ACCENT, BAR, BG, BORDER, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::{t, tf, widgets};
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
            .with_inner_size([700.0, 520.0])
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
        theme::backdrop(&ctx.layer_painter(egui::LayerId::background()), ctx.screen_rect());
        egui::TopBottomPanel::top("header").exact_height(84.0).show_separator_line(false).frame(Frame::new().inner_margin(Margin::symmetric(28, 0))).show(ctx, |ui| {
            ui.horizontal_centered(|ui| {
                let (rect, _) = ui.allocate_exact_size(vec2(40.0, 40.0), Sense::hover());
                theme::glow(ui.painter(), rect.center(), vec2(30.0, 30.0), ACCENT.gamma_multiply(0.35));
                ui.painter().circle_stroke(rect.center(), 14.0, Stroke::new(4.5_f32, ACCENT));
                ui.painter().circle_filled(rect.center(), 5.0, egui::Color32::from_rgb(0xB8, 0xFF, 0xA8));
                ui.add_space(6.0);
                ui.vertical(|ui| {
                    ui.add_space(14.0);
                    ui.label(RichText::new("OpenRazer Studio").font(theme::heavy(22.0)));
                    ui.label(RichText::new(tf("Setup · version {}", &[&ops::VERSION])).size(12.0).color(TEXT_DIM));
                });
            });
        });
        let _ = (BAR, BG);
        egui::CentralPanel::default().frame(Frame::new().inner_margin(Margin::symmetric(28, 16))).show(ctx, |ui| match self.page.clone() {
            Page::Welcome => self.welcome(ui),
            Page::Maintenance => self.maintenance(ui),
            Page::ConfirmRemoval => self.confirm_removal(ui),
            Page::Working(what) => self.working(ui, what),
            Page::Done { ok, message, launch } => self.done(ui, ok, &message, launch),
        });
    }
}

impl Setup {
    fn options(&mut self, ui: &mut egui::Ui) {
        widgets::toggle_row(ui, t("Start menu entry"), t("Plus one to repair or remove Studio."), &mut self.options.start_menu);
        widgets::toggle_row(ui, t("Desktop shortcut"), "", &mut self.options.desktop);
        widgets::toggle_row(ui, t("Start with Windows"), t("In the tray, keeping your devices set up."), &mut self.options.autostart);
        widgets::toggle_row(ui, t("Command line in PATH"), t("For openrazer-win in a terminal."), &mut self.options.path);
    }

    fn welcome(&mut self, ui: &mut egui::Ui) {
        if !ops::has_payload() {
            widgets::banner(ui, theme::DANGER, icon::WARNING, t("Nothing to install"), t("This copy of the installer was built without the program inside it. Download the installer from the releases page."), |ui| {
                widgets::link(ui, t("Releases"), install::RELEASES);
            });
            return;
        }
        ui.label(widgets::dim(t("Razer device control without Synapse: lighting, DPI, macros and profiles. Installed for you alone, so it never asks for administrator rights, and it can be repaired or removed cleanly at any time.")));
        ui.add_space(10.0);
        widgets::section(ui, t("Install to"));
        ui.horizontal(|ui| {
            ui.add(egui::TextEdit::singleline(&mut self.dir).desired_width(ui.available_width() - 110.0));
            if widgets::icon_button(ui, icon::FOLDER, t("Browse")).clicked() {
                if let Some(dir) = rfd_pick() {
                    self.dir = dir.join(install::APP_NAME).display().to_string();
                }
            }
        });
        ui.add_space(8.0);
        self.options(ui);
        ui.with_layout(Layout::bottom_up(Align::Max), |ui| {
            ui.horizontal(|ui| {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if widgets::primary_icon_button(ui, icon::DOWNLOAD, t("Install")).clicked() {
                        let ctx = ui.ctx().clone();
                        self.start(&ctx, "install");
                    }
                    ui.label(widgets::faint(t("GPL-2.0-or-later. Not affiliated with Razer Inc.")));
                });
            });
        });
    }

    fn maintenance(&mut self, ui: &mut egui::Ui) {
        let Some(installed) = self.installed.clone() else {
            self.page = Page::Welcome;
            return;
        };
        ui.label(RichText::new(tf("Version {} is installed in {}", &[&installed.version, &installed.dir.display()])).color(TEXT_DIM));
        ui.add_space(12.0);
        let newer = !installed.version.is_empty() && installed.version != ops::VERSION;
        let choice = |ui: &mut egui::Ui, glyph: &str, title: &str, text: &str, danger: bool| -> bool {
            let response = Frame::new()
                .fill(theme::SURFACE)
                .stroke(Stroke::new(1.0_f32, BORDER))
                .corner_radius(CornerRadius::same(theme::RADIUS))
                .inner_margin(Margin::symmetric(16, 12))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        widgets::icon(ui, glyph, 20.0, if danger { theme::DANGER } else { ACCENT });
                        ui.add_space(6.0);
                        ui.vertical(|ui| {
                            ui.spacing_mut().item_spacing.y = 2.0;
                            ui.label(RichText::new(title).font(theme::bold(15.0)));
                            ui.label(RichText::new(text).size(12.5).color(TEXT_DIM));
                        });
                    });
                })
                .response
                .interact(Sense::click());
            response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked()
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
            ui.add_space(6.0);
            if choice(ui, icon::WRENCH, t("Repair"), t("Puts back missing or damaged files, the Start menu entry and starting with Windows, and sets aside settings files that no longer read. Profiles and macros are kept."), false) {
                let ctx = ui.ctx().clone();
                self.start(&ctx, "repair");
            }
            ui.add_space(6.0);
        }
        if choice(ui, icon::DELETE, t("Uninstall"), t("Removes the program, its shortcuts and its entry in Windows. You choose whether your settings go too."), true) {
            self.page = Page::ConfirmRemoval;
        }
    }

    fn confirm_removal(&mut self, ui: &mut egui::Ui) {
        widgets::heading(ui, t("Remove OpenRazer Studio"));
        ui.label(widgets::dim(tf("The program in {} and its shortcuts go. Your devices keep whatever lighting they show until they lose power.", &[&self.dir])));
        ui.add_space(10.0);
        widgets::toggle_row(ui, t("Also remove Studio's settings"), t("The macro library, links between apps and profiles, and window settings."), &mut self.removal.settings);
        widgets::toggle_row(ui, t("Also remove everything the daemon keeps"), t("Device settings, profiles, macro key bindings and logs, shared with the classic control panel and the command line."), &mut self.removal.all_data);
        if self.removal.all_data {
            self.removal.settings = true;
        }
        ui.with_layout(Layout::bottom_up(Align::Max), |ui| {
            ui.horizontal(|ui| {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if widgets::danger_button(ui, t("Uninstall")).clicked() {
                        let ctx = ui.ctx().clone();
                        self.start(&ctx, "uninstall");
                    }
                    if ui.button(t("Back")).clicked() {
                        self.page = if self.installed.is_some() { Page::Maintenance } else { Page::Welcome };
                    }
                });
            });
        });
    }

    fn working(&mut self, ui: &mut egui::Ui, what: &str) {
        let shared = self.shared.lock().unwrap();
        widgets::heading(ui, match what {
            "uninstall" => t("Removing"),
            "repair" => t("Repairing"),
            _ => t("Installing"),
        });
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            ui.add(egui::Spinner::new().color(ACCENT));
            ui.label(&shared.step);
        });
        ui.add(egui::ProgressBar::new(shared.fraction).desired_height(6.0).fill(ACCENT));
        ui.add_space(6.0);
        for note in &shared.notes {
            ui.label(widgets::faint(format!("· {note}")));
        }
        ui.ctx().request_repaint_after(std::time::Duration::from_millis(100));
    }

    fn done(&mut self, ui: &mut egui::Ui, ok: bool, message: &str, launch: bool) {
        ui.horizontal(|ui| {
            widgets::icon(ui, if ok { icon::CHECK } else { icon::WARNING }, 22.0, if ok { ACCENT } else { theme::DANGER });
            ui.label(RichText::new(message).font(theme::bold(16.0)));
        });
        let notes = self.shared.lock().unwrap().notes.clone();
        if !notes.is_empty() {
            ui.add_space(6.0);
            widgets::section(ui, t("What was done"));
            for note in notes {
                ui.label(widgets::dim(format!("· {note}")));
            }
        } else if ok && message == t("OpenRazer Studio has been repaired.") {
            ui.label(widgets::dim(t("Nothing needed fixing: every file was as it should be.")));
        }
        if !ok {
            ui.label(widgets::faint(tf("The details are also in {}", &[&std::env::temp_dir().join("openrazer-studio-setup.log").display()])));
        }
        ui.with_layout(Layout::bottom_up(Align::Max), |ui| {
            ui.horizontal(|ui| {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.button(t("Close")).clicked() {
                        ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                    if launch && widgets::primary_button(ui, t("Open OpenRazer Studio")).clicked() {
                        let _ = std::process::Command::new(PathBuf::from(self.dir.trim()).join(APP_EXE)).spawn();
                        ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                    if !ok && ui.button(t("Try again")).clicked() {
                        self.page = if self.installed.is_some() { Page::Maintenance } else { Page::Welcome };
                    }
                });
            });
        });
        let _ = TEXT_FAINT;
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
