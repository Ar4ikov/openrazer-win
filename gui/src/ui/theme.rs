//! Colours, type and the look of every widget.

use egui::{Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Margin, Stroke, TextStyle};
use std::sync::Arc;

pub const BG: Color32 = Color32::from_rgb(0x0E, 0x10, 0x14);
pub const SIDEBAR: Color32 = Color32::from_rgb(0x13, 0x16, 0x1C);
pub const CARD: Color32 = Color32::from_rgb(0x19, 0x1D, 0x25);
pub const CARD_HOVER: Color32 = Color32::from_rgb(0x21, 0x26, 0x31);
pub const CARD_ACTIVE: Color32 = Color32::from_rgb(0x1C, 0x2A, 0x1E);
pub const BORDER: Color32 = Color32::from_rgb(0x2A, 0x30, 0x3B);
pub const TEXT: Color32 = Color32::from_rgb(0xE8, 0xEA, 0xED);
pub const TEXT_DIM: Color32 = Color32::from_rgb(0x9A, 0xA1, 0xAC);
pub const TEXT_FAINT: Color32 = Color32::from_rgb(0x67, 0x6E, 0x7A);
pub const ACCENT: Color32 = Color32::from_rgb(0x44, 0xD6, 0x2C);
pub const ACCENT_DIM: Color32 = Color32::from_rgb(0x2C, 0x8C, 0x1D);
pub const DANGER: Color32 = Color32::from_rgb(0xFF, 0x5C, 0x5C);
pub const WARN: Color32 = Color32::from_rgb(0xF2, 0xC9, 0x4C);

pub const RADIUS: u8 = 10;

/// Segoe UI if Windows has it, with egui's own fonts behind for anything it
/// lacks; an icon family from Segoe's symbol fonts where present.
pub fn install_fonts(ctx: &egui::Context) {
    let mut fonts = FontDefinitions::default();
    let fonts_dir = std::env::var_os("WINDIR")
        .map(|w| std::path::PathBuf::from(w).join("Fonts"))
        .unwrap_or_else(|| std::path::PathBuf::from(r"C:\Windows\Fonts"));
    let load = |name: &str| std::fs::read(fonts_dir.join(name)).ok();

    if let Some(regular) = load("segoeui.ttf") {
        fonts.font_data.insert("segoe".into(), Arc::new(FontData::from_owned(regular)));
        fonts.families.entry(FontFamily::Proportional).or_default().insert(0, "segoe".into());
    }
    if let Some(bold) = load("seguisb.ttf").or_else(|| load("segoeuib.ttf")) {
        fonts.font_data.insert("segoe-semibold".into(), Arc::new(FontData::from_owned(bold)));
        let mut family = vec!["segoe-semibold".to_string()];
        family.extend(fonts.families.get(&FontFamily::Proportional).cloned().unwrap_or_default());
        fonts.families.insert(FontFamily::Name("bold".into()), family);
    } else {
        let proportional = fonts.families.get(&FontFamily::Proportional).cloned().unwrap_or_default();
        fonts.families.insert(FontFamily::Name("bold".into()), proportional);
    }
    if let Some(icons) = load("SegoeIcons.ttf").or_else(|| load("segmdl2.ttf")) {
        fonts.font_data.insert("icons".into(), Arc::new(FontData::from_owned(icons)));
        fonts.families.insert(FontFamily::Name("icons".into()), vec!["icons".into()]);
    } else {
        let proportional = fonts.families.get(&FontFamily::Proportional).cloned().unwrap_or_default();
        fonts.families.insert(FontFamily::Name("icons".into()), proportional);
    }
    ctx.set_fonts(fonts);
}

pub fn bold(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("bold".into()))
}

pub fn icon_font(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("icons".into()))
}

/// Glyphs from Segoe Fluent Icons / MDL2 Assets.
pub mod icon {
    pub const SETTINGS: &str = "\u{E713}";
    pub const REFRESH: &str = "\u{E72C}";
    pub const PLAY: &str = "\u{E768}";
    pub const DELETE: &str = "\u{E74D}";
    pub const EDIT: &str = "\u{E70F}";
    pub const ADD: &str = "\u{E710}";
    pub const POWER: &str = "\u{E7E8}";
    pub const BRIGHTNESS: &str = "\u{E706}";
    pub const INFO: &str = "\u{E946}";
    pub const WARNING: &str = "\u{E7BA}";
    pub const CHECK: &str = "\u{E73E}";
    pub const FOLDER: &str = "\u{E8B7}";
    pub const RECORD: &str = "\u{E7C8}";
}

pub fn apply(ctx: &egui::Context) {
    install_fonts(ctx);
    let mut style = (*ctx.style()).clone();
    style.text_styles = [
        (TextStyle::Small, FontId::proportional(12.0)),
        (TextStyle::Body, FontId::proportional(14.5)),
        (TextStyle::Button, FontId::proportional(14.0)),
        (TextStyle::Heading, bold(22.0)),
        (TextStyle::Monospace, FontId::monospace(13.5)),
    ]
    .into();
    style.spacing.item_spacing = egui::vec2(10.0, 10.0);
    style.spacing.button_padding = egui::vec2(14.0, 7.0);
    style.spacing.slider_width = 280.0;
    style.spacing.interact_size.y = 30.0;
    style.spacing.combo_width = 180.0;
    style.spacing.window_margin = Margin::same(14);

    let mut visuals = egui::Visuals::dark();
    visuals.panel_fill = BG;
    visuals.window_fill = CARD;
    visuals.window_stroke = Stroke::new(1.0_f32, BORDER);
    visuals.window_corner_radius = CornerRadius::same(RADIUS);
    visuals.menu_corner_radius = CornerRadius::same(RADIUS);
    visuals.extreme_bg_color = Color32::from_rgb(0x0A, 0x0C, 0x0F);
    visuals.faint_bg_color = CARD;
    visuals.selection.bg_fill = ACCENT_DIM;
    visuals.selection.stroke = Stroke::new(1.0_f32, ACCENT);
    visuals.hyperlink_color = ACCENT;
    visuals.override_text_color = Some(TEXT);
    let widgets = &mut visuals.widgets;
    for w in [&mut widgets.noninteractive, &mut widgets.inactive, &mut widgets.hovered, &mut widgets.active, &mut widgets.open] {
        w.corner_radius = CornerRadius::same(8);
    }
    widgets.noninteractive.bg_fill = CARD;
    widgets.noninteractive.bg_stroke = Stroke::new(1.0_f32, BORDER);
    widgets.noninteractive.fg_stroke = Stroke::new(1.0_f32, TEXT_DIM);
    widgets.inactive.bg_fill = Color32::from_rgb(0x24, 0x29, 0x33);
    widgets.inactive.weak_bg_fill = Color32::from_rgb(0x24, 0x29, 0x33);
    widgets.inactive.bg_stroke = Stroke::NONE;
    widgets.inactive.fg_stroke = Stroke::new(1.0_f32, TEXT);
    widgets.hovered.bg_fill = Color32::from_rgb(0x2D, 0x33, 0x40);
    widgets.hovered.weak_bg_fill = Color32::from_rgb(0x2D, 0x33, 0x40);
    widgets.hovered.bg_stroke = Stroke::new(1.0_f32, Color32::from_rgb(0x3A, 0x41, 0x50));
    widgets.hovered.fg_stroke = Stroke::new(1.5_f32, TEXT);
    widgets.active.bg_fill = ACCENT_DIM;
    widgets.active.weak_bg_fill = ACCENT_DIM;
    widgets.active.fg_stroke = Stroke::new(1.5_f32, Color32::WHITE);
    style.visuals = visuals;
    ctx.set_style(style);
}
