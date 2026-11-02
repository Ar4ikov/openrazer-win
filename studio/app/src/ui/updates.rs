//! Updates: whether a newer Studio is out, what is in it, and getting it.

use super::app::{scroll, App};
use crate::notes::{self, Block, Span};
use crate::updates::{self, newer, Phase, Progress, Release, Status, CURRENT};
use egui::text::{LayoutJob, TextFormat};
use egui::{pos2, vec2, Align, Color32, CornerRadius, Frame, Layout, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::i18n::{self, Lang};
use openrazer_studio_core::install::{self, REPOSITORY};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, SURFACE, SURFACE_HI, TEXT, TEXT_DIM, TEXT_FAINT, WELL};
use openrazer_studio_core::widgets::{self, Button, IconButton};
use openrazer_studio_core::{t, tf};
use std::collections::HashMap;
use std::sync::Arc;

/// Notes read once, by version and language: they are drawn every frame.
pub type NotesCache = HashMap<(String, bool), Arc<Vec<Block>>>;

/// `23520256` as Explorer would put it: `22.4`.
fn megabytes(bytes: u64) -> String {
    let text = format!("{:.1}", bytes as f64 / 1_048_576.0);
    if i18n::current() == Lang::Ru {
        text.replace('.', ",")
    } else {
        text
    }
}

/// `2026-11-03` as a reader would write it.
fn date(published: &str) -> String {
    const EN: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const RU: [&str; 12] = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
    let parts: Vec<&str> = published.split('-').collect();
    let [year, month, day] = parts.as_slice() else { return published.to_string() };
    let (Ok(month), Ok(day)) = (month.parse::<usize>(), day.parse::<u32>()) else { return published.to_string() };
    let Some(index) = month.checked_sub(1).filter(|i| *i < 12) else { return published.to_string() };
    format!("{day} {} {year}", if i18n::current() == Lang::Ru { RU[index] } else { EN[index] })
}

fn ago(checked: u64) -> String {
    if checked == 0 {
        return t("not yet").to_string();
    }
    match updates::now().saturating_sub(checked) {
        0..=59 => t("just now").to_string(),
        s @ 60..=3599 => tf("{} min ago", &[&(s / 60)]),
        s @ 3600..=86_399 => tf("{} h ago", &[&(s / 3600)]),
        s => tf("{} d ago", &[&(s / 86_400)]),
    }
}

fn time_left(seconds: u64) -> String {
    if seconds < 60 {
        tf("{} s left", &[&seconds.max(1)])
    } else {
        tf("{} min left", &[&(seconds / 60 + 1)])
    }
}

pub fn page(app: &mut App, ui: &mut egui::Ui) {
    let status = app.updates.status();
    let busy = matches!(status.phase, Phase::Checking | Phase::Downloading(_) | Phase::Installing);
    widgets::page_header(ui, t("Updates"), t("Studio and the daemon are updated together, when you say so."), |ui| {
        if ui.add(Button::secondary(t("Check now")).icon(icon::REFRESH).enabled(!busy)).clicked() {
            app.updates.check();
        }
        if status.phase == Phase::Checking {
            ui.add(egui::Spinner::new().size(16.0).color(TEXT_FAINT));
        }
    });
    scroll(ui, "updates", |ui| {
        if let Some(error) = &status.error {
            widgets::banner(ui, theme::WARN, icon::WARNING, t("That did not work"), error, |ui| {
                if let Some(release) = status.newest() {
                    widgets::link_button(ui, t("Open the release page"), &release.page);
                }
            });
        }
        hero(app, ui, &status);
        widgets::columns(ui, 0.66, super::device::TWO_COLUMNS, |ui, column| {
            if column == 0 {
                what_is_new(app, ui, &status);
            } else {
                about(app, ui, &status);
            }
        });
    });
}

/// The state of things, and the one thing to do about it: a sunk tile with an
/// icon, a title and a line, and the buttons at the right end. `below` runs
/// across the whole card, for the progress of a download.
#[allow(clippy::too_many_arguments)]
fn card(ui: &mut egui::Ui, glyph: &str, colour: Color32, title: &str, text: &str, actions_width: f32, actions: impl FnOnce(&mut egui::Ui), below: impl FnOnce(&mut egui::Ui)) {
    let edge = if colour == TEXT_FAINT { BORDER } else { colour.gamma_multiply(0.4) };
    Frame::new().fill(SURFACE).stroke(Stroke::new(1.0_f32, edge)).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::same(18)).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.spacing_mut().item_spacing.y = 12.0;
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 16.0;
            let (tile, _) = ui.allocate_exact_size(vec2(52.0, 52.0), Sense::hover());
            ui.painter().rect_filled(tile, CornerRadius::same(theme::RADIUS), WELL);
            if colour != TEXT_FAINT {
                theme::glow(ui.painter(), tile.center(), vec2(30.0, 30.0), colour.gamma_multiply(0.22));
            }
            ui.painter().text(tile.center(), egui::Align2::CENTER_CENTER, glyph, theme::icon_font(21.0), colour);
            let width = (ui.available_width() - actions_width - 16.0).max(180.0);
            ui.allocate_ui_with_layout(vec2(width, 52.0), Layout::top_down(Align::Min), |ui| {
                ui.set_width(width);
                ui.spacing_mut().item_spacing.y = 3.0;
                ui.add(egui::Label::new(RichText::new(title).font(theme::bold(18.0)).color(TEXT)).wrap());
                if !text.is_empty() {
                    ui.add(egui::Label::new(RichText::new(text).size(13.0).color(TEXT_DIM)).wrap());
                }
            });
            ui.allocate_ui_with_layout(vec2(ui.available_width(), 52.0), Layout::right_to_left(Align::Center), |ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                actions(ui);
            });
        });
        below(ui);
    });
}

fn hero(app: &mut App, ui: &mut egui::Ui, status: &Status) {
    let Some(release) = status.newest() else {
        let line = tf("Version {} · checked {}", &[&CURRENT, &ago(status.checked)]);
        return card(ui, icon::CHECK, TEXT_FAINT, t("You have the latest version"), &line, 0.0, |_| {}, |_| {});
    };
    let facts = {
        let mut facts = vec![tf("You have {}", &[&CURRENT]), date(&release.published)];
        facts.extend(release.installer.as_ref().map(|i| tf("{} MB", &[&megabytes(i.size)])));
        facts.join(" · ")
    };
    let Some(dir) = status.install_dir.clone() else {
        // Nothing here to replace: a copy run from a build folder.
        let text = format!("{facts}\n{}", t("This copy of Studio is not the installed one, so it cannot replace itself."));
        return card(ui, icon::DOWNLOAD, ACCENT, &tf("Version {} is out", &[&release.version]), &text, 220.0, |ui| {
            widgets::link_button(ui, t("Open the release page"), &release.page);
        }, |_| {});
    };
    match &status.phase {
        Phase::Installing => card(ui, icon::DOWNLOAD, ACCENT, &tf("Installing {}", &[&release.version]), t("The installer is starting and will close Studio. It comes back by itself."), 60.0, |ui| {
            ui.add(egui::Spinner::new().size(20.0).color(ACCENT));
        }, |_| {}),
        Phase::Downloading(progress) => {
            let mut cancelled = false;
            card(ui, icon::DOWNLOAD, ACCENT, &tf("Downloading {}", &[&release.version]), t("You can carry on using Studio. Nothing is installed until you say so."), 130.0, |ui| {
                cancelled = ui.add(Button::secondary(t("Cancel")).min_width(100.0)).clicked();
            }, |ui| download_bar(ui, progress));
            if cancelled {
                app.updates.cancel();
            }
        }
        Phase::Ready => {
            let mut install = false;
            card(ui, icon::CHECK, ACCENT, &tf("Version {} is ready to install", &[&release.version]), t("Studio closes for a few seconds and opens again by itself. Settings, profiles and macros stay where they are."), 190.0, |ui| {
                install = ui.add(Button::primary(t("Install now")).icon(icon::DOWNLOAD).min_width(150.0)).clicked();
            }, |_| {});
            if install {
                app.updates.install(dir);
            }
        }
        _ => {
            let skipped = status.skipped == release.version;
            let (mut download, mut skip) = (false, false);
            let text = if skipped { format!("{facts}\n{}", t("You chose to skip this version, so it is not mentioned elsewhere.")) } else { facts };
            card(ui, icon::SYNC, ACCENT, &tf("Version {} is out", &[&release.version]), &text, 330.0, |ui| {
                download = ui.add(Button::primary(t("Download")).icon(icon::DOWNLOAD).min_width(130.0)).clicked();
                skip = !skipped && ui.add(Button::secondary(t("Skip this version"))).clicked();
            }, |_| {});
            if download {
                app.updates.download();
            }
            if skip {
                app.updates.skip(&release.version);
            }
        }
    }
}

fn download_bar(ui: &mut egui::Ui, progress: &Progress) {
    let fraction = if progress.total > 0 { progress.done as f32 / progress.total as f32 } else { 0.0 };
    ui.add(egui::ProgressBar::new(fraction).desired_height(8.0).fill(ACCENT));
    ui.spacing_mut().interact_size.y = 14.0;
    ui.horizontal(|ui| {
        ui.label(RichText::new(tf("{} of {} MB", &[&megabytes(progress.done), &megabytes(progress.total)])).size(12.5).color(TEXT_DIM));
        if progress.per_second > 1.0 {
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let left = progress.total.saturating_sub(progress.done) as f64 / progress.per_second;
                ui.label(RichText::new(format!("{} · {}", tf("{} MB/s", &[&megabytes(progress.per_second as u64)]), time_left(left as u64))).size(12.5).color(TEXT_DIM));
            });
        }
    });
    ui.ctx().request_repaint_after(std::time::Duration::from_millis(200));
}

/// Every version from the one here up, newest first.
fn what_is_new(app: &mut App, ui: &mut egui::Ui, status: &Status) {
    let shown: Vec<&Release> = status.releases.iter().filter(|r| !newer(CURRENT, &r.version)).collect();
    if shown.is_empty() {
        widgets::plain_panel(ui, |ui| widgets::empty_state(ui, t("No notes here yet"), t("The notes of each release appear here after the first check."), |_| {}));
        return;
    }
    let russian = i18n::current() == Lang::Ru;
    for release in shown {
        let blocks = app.notes.entry((release.version.clone(), russian)).or_insert_with(|| Arc::new(notes::parse(notes::for_language(&release.notes, i18n::current())))).clone();
        let is_new = newer(&release.version, CURRENT);
        widgets::panel_with(ui, &tf("Version {}", &[&release.version]), |ui| {
            if ui.add(IconButton::new(icon::OPEN, t("Open the release page"))).clicked() {
                ui.ctx().open_url(egui::OpenUrl::new_tab(&release.page));
            }
            widgets::chip(ui, &date(&release.published));
            if is_new {
                widgets::badge(ui, t("New"), ACCENT);
            } else {
                widgets::badge(ui, t("Installed"), TEXT_DIM);
            }
        }, |ui| {
            if blocks.is_empty() {
                widgets::help(ui, t("This release came without notes."));
            }
            draw_notes(ui, &blocks);
        });
    }
}

/// The colour of a changelog heading: what kind of change it is, in either language.
fn heading_colour(heading: &str) -> Color32 {
    let heading = heading.to_lowercase();
    let starts = |words: &[&str]| words.iter().any(|w| heading.starts_with(w));
    if starts(&["add", "new", "добав", "нов"]) {
        ACCENT
    } else if starts(&["fix", "исправ"]) {
        theme::WARN
    } else if starts(&["remov", "deprecat", "удал", "убран"]) {
        theme::DANGER
    } else if starts(&["chang", "измен"]) {
        theme::INFO
    } else {
        TEXT_DIM
    }
}

fn draw_notes(ui: &mut egui::Ui, blocks: &[Block]) {
    ui.spacing_mut().item_spacing.y = 7.0;
    for (i, block) in blocks.iter().enumerate() {
        match block {
            Block::Heading(text) => {
                if i > 0 {
                    ui.add_space(8.0);
                }
                widgets::badge(ui, text, heading_colour(text));
            }
            Block::Text(spans) => rich(ui, spans),
            Block::Code(text) => {
                Frame::new().fill(WELL).corner_radius(CornerRadius::same(theme::RADIUS_SM)).inner_margin(Margin::symmetric(10, 8)).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.label(RichText::new(text).font(egui::FontId::monospace(12.5)).color(TEXT_DIM));
                });
            }
            Block::Item { level, spans } => {
                ui.horizontal_top(|ui| {
                    ui.spacing_mut().item_spacing.x = 0.0;
                    ui.add_space(*level as f32 * 18.0);
                    let (mark, _) = ui.allocate_exact_size(vec2(16.0, 19.0), Sense::hover());
                    let dot = pos2(mark.left() + 3.0, mark.center().y);
                    if *level == 0 {
                        ui.painter().circle_filled(dot, 2.2, TEXT_FAINT);
                    } else {
                        ui.painter().circle_stroke(dot, 2.0, Stroke::new(1.0_f32, TEXT_FAINT));
                    }
                    ui.vertical(|ui| rich(ui, spans));
                });
            }
        }
    }
}

/// A paragraph of styled text, its links clickable.
fn rich(ui: &mut egui::Ui, spans: &[Span]) {
    let mut job = LayoutJob::default();
    job.wrap.max_width = ui.available_width();
    let mut links = Vec::new();
    let mut at = 0;
    for span in spans {
        let colour = if span.link.is_some() {
            ACCENT
        } else if span.bold {
            TEXT
        } else {
            TEXT_DIM
        };
        let format = TextFormat {
            font_id: if span.code { egui::FontId::monospace(12.0) } else if span.bold { theme::bold(13.5) } else { theme::body(13.5) },
            line_height: Some(19.0),
            color: colour,
            background: if span.code { SURFACE_HI } else { Color32::TRANSPARENT },
            italics: span.italic,
            underline: if span.link.is_some() { Stroke::new(1.0_f32, ACCENT.gamma_multiply(0.5)) } else { Stroke::NONE },
            ..Default::default()
        };
        job.append(&span.text, 0.0, format);
        let length = span.text.chars().count();
        if let Some(link) = &span.link {
            links.push((at..at + length, link));
        }
        at += length;
    }
    let galley = ui.fonts(|f| f.layout_job(job));
    let (rect, response) = ui.allocate_exact_size(galley.size(), if links.is_empty() { Sense::hover() } else { Sense::click() });
    if ui.is_rect_visible(rect) {
        ui.painter().galley(rect.min, galley.clone(), TEXT);
    }
    if let Some(pointer) = response.hover_pos() {
        let index = galley.cursor_from_pos(pointer - rect.min).ccursor.index;
        if let Some((_, link)) = links.iter().find(|(range, _)| range.contains(&index)) {
            ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
            if response.clicked() {
                ui.ctx().open_url(egui::OpenUrl::new_tab(link.as_str()));
            }
        }
    }
}

fn about(app: &mut App, ui: &mut egui::Ui, status: &Status) {
    widgets::panel(ui, t("How it works"), |ui| {
        widgets::fact(ui, t("Version"), CURRENT);
        widgets::fact(ui, t("Last check"), &ago(status.checked));
        widgets::divider(ui);
        let mut enabled = app.settings.check_updates;
        if widgets::toggle_row(ui, t("Look for updates"), t("About once a day, asks GitHub whether a newer release is out. Nothing is sent, and nothing is downloaded until you say so."), &mut enabled) {
            app.settings.check_updates = enabled;
            app.save_settings();
        }
        widgets::divider(ui);
        widgets::help(ui, t("Before it runs, the installer is checked against the checksum published with the release. It keeps your settings, profiles and macros, and never asks for administrator rights."));
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = vec2(8.0, 8.0);
            widgets::link_button(ui, t("All releases"), &format!("{REPOSITORY}/releases"));
            widgets::link_button(ui, t("Changelog"), &format!("{REPOSITORY}/blob/main/{}", if i18n::current() == Lang::Ru { "CHANGELOG.ru.md" } else { "CHANGELOG.md" }));
        });
        if status.install_dir.is_none() && app.installed.is_none() {
            widgets::link_button(ui, t("Get the installer"), install::RELEASES);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_read_as_written() {
        i18n::set(Lang::En);
        assert_eq!(date("2026-11-03"), "3 Nov 2026");
        assert_eq!(date("not a date"), "not a date");
        assert_eq!(date("2026-13-03"), "2026-13-03");
    }

    #[test]
    fn a_heading_is_coloured_by_kind_in_both_languages() {
        assert_eq!(heading_colour("Added"), heading_colour("Добавлено"));
        assert_eq!(heading_colour("Fixed"), heading_colour("Исправлено"));
        assert_eq!(heading_colour("Changed"), heading_colour("Изменено"));
        assert_eq!(heading_colour("Removed"), heading_colour("Удалено"));
    }
}
