//! Colours, type and the look of every widget.
//!
//! Near-black surfaces, hairline borders, small capitals for headings and one
//! green accent: the look Razer's own software has taught its users to read.

use egui::{Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Margin, Stroke, TextStyle};
use std::sync::Arc;

pub const BG: Color32 = Color32::from_rgb(0x06, 0x07, 0x08);
pub const RAIL: Color32 = Color32::from_rgb(0x0B, 0x0C, 0x0E);
pub const BAR: Color32 = Color32::from_rgb(0x0B, 0x0C, 0x0E);
pub const SURFACE: Color32 = Color32::from_rgb(0x0F, 0x11, 0x14);
pub const SURFACE_HI: Color32 = Color32::from_rgb(0x17, 0x1A, 0x1E);
pub const SURFACE_HOVER: Color32 = Color32::from_rgb(0x1D, 0x21, 0x26);
pub const SURFACE_ACTIVE: Color32 = Color32::from_rgb(0x10, 0x20, 0x11);
pub const WELL: Color32 = Color32::from_rgb(0x08, 0x09, 0x0B);
pub const BORDER: Color32 = Color32::from_rgb(0x1E, 0x21, 0x26);
pub const BORDER_HI: Color32 = Color32::from_rgb(0x2C, 0x30, 0x37);
pub const TEXT: Color32 = Color32::from_rgb(0xF3, 0xF4, 0xF6);
pub const TEXT_DIM: Color32 = Color32::from_rgb(0xA0, 0xA5, 0xAE);
pub const TEXT_FAINT: Color32 = Color32::from_rgb(0x5E, 0x63, 0x6B);
pub const ACCENT: Color32 = Color32::from_rgb(0x44, 0xD6, 0x2C);
pub const ACCENT_HI: Color32 = Color32::from_rgb(0x7C, 0xFF, 0x5B);
pub const ACCENT_DIM: Color32 = Color32::from_rgb(0x2C, 0x8C, 0x1D);
pub const ACCENT_DEEP: Color32 = Color32::from_rgb(0x12, 0x2B, 0x10);
pub const ON_ACCENT: Color32 = Color32::from_rgb(0x04, 0x10, 0x03);
pub const DANGER: Color32 = Color32::from_rgb(0xFF, 0x5C, 0x6A);
pub const WARN: Color32 = Color32::from_rgb(0xFF, 0xC8, 0x4A);
pub const INFO: Color32 = Color32::from_rgb(0x52, 0xB4, 0xFF);

pub const RADIUS: u8 = 12;

/// The soft drop shadow raised panels sit on.
pub fn shadow() -> egui::epaint::Shadow {
    egui::epaint::Shadow { offset: [0, 8], blur: 24, spread: 0, color: Color32::from_black_alpha(150) }
}

/// A soft round glow: `colour` at the centre, fading out by `radius`.
pub fn glow(painter: &egui::Painter, centre: egui::Pos2, radius: egui::Vec2, colour: Color32) {
    let mut mesh = egui::epaint::Mesh::default();
    mesh.colored_vertex(centre, colour);
    let steps = 48;
    for i in 0..=steps {
        let a = i as f32 / steps as f32 * std::f32::consts::TAU;
        mesh.colored_vertex(centre + egui::vec2(a.cos() * radius.x, a.sin() * radius.y), Color32::TRANSPARENT);
    }
    for i in 1..=steps as u32 {
        mesh.add_triangle(0, i, i + 1);
    }
    painter.add(egui::Shape::mesh(mesh));
}

/// The window's background: near black, lit from the corners in the accent.
pub fn backdrop(painter: &egui::Painter, rect: egui::Rect) {
    painter.rect_filled(rect, CornerRadius::ZERO, BG);
    let size = rect.size();
    glow(painter, rect.left_top() + egui::vec2(size.x * 0.18, -size.y * 0.05), egui::vec2(size.x * 0.55, size.y * 0.5), ACCENT.gamma_multiply(0.10));
    glow(painter, rect.right_bottom() + egui::vec2(-size.x * 0.05, size.y * 0.05), egui::vec2(size.x * 0.45, size.y * 0.45), Color32::from_rgb(0x1C, 0x9C, 0x8C).gamma_multiply(0.06));
}

/// Segoe UI if Windows has it, with egui's own fonts behind for anything it
/// lacks (Cyrillic included); an icon family from Segoe's symbol fonts.
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
    let proportional = fonts.families.get(&FontFamily::Proportional).cloned().unwrap_or_default();
    let mut add_family = |family: &str, files: &[&str]| {
        let mut chain = Vec::new();
        for file in files {
            if let Some(bytes) = load(file) {
                let key = format!("{family}-{file}");
                fonts.font_data.insert(key.clone(), Arc::new(FontData::from_owned(bytes)));
                chain.push(key);
                break;
            }
        }
        chain.extend(proportional.iter().cloned());
        fonts.families.insert(FontFamily::Name(family.into()), chain);
    };
    add_family("bold", &["seguisb.ttf", "segoeuib.ttf"]);
    add_family("heavy", &["seguibl.ttf", "segoeuib.ttf"]);
    add_family("light", &["segoeuil.ttf", "segoeuisl.ttf"]);
    if let Some(icons) = load("SegoeIcons.ttf").or_else(|| load("segmdl2.ttf")) {
        fonts.font_data.insert("icons".into(), Arc::new(FontData::from_owned(icons)));
        fonts.families.insert(FontFamily::Name("icons".into()), vec!["icons".into()]);
    } else {
        fonts.families.insert(FontFamily::Name("icons".into()), proportional.clone());
    }
    ctx.set_fonts(fonts);
}

pub fn bold(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("bold".into()))
}

pub fn heavy(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("heavy".into()))
}

pub fn light(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("light".into()))
}

pub fn icon_font(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("icons".into()))
}

/// Glyphs from Segoe Fluent Icons / MDL2 Assets.
pub mod icon {
    pub const HOME: &str = "\u{E80F}";
    pub const DEVICES: &str = "\u{E772}";
    pub const LIGHTING: &str = "\u{E781}";
    pub const MACROS: &str = "\u{E765}";
    pub const INTEGRATIONS: &str = "\u{E71B}";
    pub const SETTINGS: &str = "\u{E713}";
    pub const REFRESH: &str = "\u{E72C}";
    pub const PLAY: &str = "\u{E768}";
    pub const STOP: &str = "\u{E71A}";
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
    pub const BACK: &str = "\u{E72B}";
    pub const FORWARD: &str = "\u{E72A}";
    pub const CHEVRON_DOWN: &str = "\u{E70D}";
    pub const CHEVRON_RIGHT: &str = "\u{E76C}";
    pub const UP: &str = "\u{E74A}";
    pub const DOWN: &str = "\u{E74B}";
    pub const EYE: &str = "\u{E7B3}";
    pub const EYE_OFF: &str = "\u{ED1A}";
    pub const LAYERS: &str = "\u{E81E}";
    pub const SELECT_ALL: &str = "\u{E8B3}";
    pub const CLEAR: &str = "\u{E894}";
    pub const SYNC: &str = "\u{E895}";
    pub const KEYBOARD: &str = "\u{E765}";
    pub const MOUSE: &str = "\u{E962}";
    pub const HEADSET: &str = "\u{E7F6}";
    pub const GAME: &str = "\u{E7FC}";
    pub const BATTERY: &str = "\u{E83F}";
    pub const SPEED: &str = "\u{EC4A}";
    pub const DOWNLOAD: &str = "\u{E896}";
    pub const SAVE: &str = "\u{E74E}";
    pub const COPY: &str = "\u{E8C8}";
    pub const LINK: &str = "\u{E71B}";
    pub const APP: &str = "\u{ECAA}";
    pub const BULB: &str = "\u{EA80}";
    pub const CODE: &str = "\u{E943}";
    pub const HEART: &str = "\u{EB51}";
    pub const SHIELD: &str = "\u{EA18}";
    pub const WRENCH: &str = "\u{E90F}";
    pub const CLOSE: &str = "\u{E711}";
    pub const PROFILE: &str = "\u{E77B}";
    pub const TEXT: &str = "\u{E8D2}";
    pub const TIMER: &str = "\u{E916}";
    pub const OPEN: &str = "\u{E8A7}";
    pub const CHAT: &str = "\u{E8BD}";
    pub const MUSIC: &str = "\u{E8D6}";
    pub const SCREEN: &str = "\u{E7F4}";
    pub const BELL: &str = "\u{EA8F}";
    pub const PALETTE: &str = "\u{E790}";
    pub const GLOBE: &str = "\u{E774}";
    pub const HOMEAUTO: &str = "\u{E80F}";
}

pub fn apply(ctx: &egui::Context) {
    install_fonts(ctx);
    let mut style = (*ctx.style()).clone();
    style.text_styles = [
        (TextStyle::Small, FontId::proportional(12.0)),
        (TextStyle::Body, FontId::proportional(14.0)),
        (TextStyle::Button, FontId::proportional(13.5)),
        (TextStyle::Heading, bold(22.0)),
        (TextStyle::Monospace, FontId::monospace(13.0)),
    ]
    .into();
    style.spacing.item_spacing = egui::vec2(10.0, 10.0);
    style.spacing.button_padding = egui::vec2(16.0, 7.0);
    style.spacing.slider_width = 260.0;
    style.spacing.interact_size.y = 28.0;
    style.spacing.combo_width = 180.0;
    style.spacing.window_margin = Margin::same(14);
    style.spacing.scroll.bar_width = 6.0;
    style.spacing.scroll.floating = true;

    let mut visuals = egui::Visuals::dark();
    visuals.panel_fill = BG;
    visuals.window_fill = SURFACE;
    visuals.window_stroke = Stroke::new(1.0_f32, BORDER_HI);
    visuals.window_corner_radius = CornerRadius::same(RADIUS + 2);
    visuals.window_shadow = shadow();
    visuals.popup_shadow = shadow();
    visuals.menu_corner_radius = CornerRadius::same(RADIUS);
    visuals.extreme_bg_color = WELL;
    visuals.faint_bg_color = SURFACE;
    visuals.selection.bg_fill = ACCENT_DIM;
    visuals.selection.stroke = Stroke::new(1.0_f32, ACCENT);
    visuals.hyperlink_color = ACCENT;
    visuals.override_text_color = Some(TEXT);
    visuals.slider_trailing_fill = true;
    visuals.handle_shape = egui::style::HandleShape::Circle;
    let widgets = &mut visuals.widgets;
    for w in [&mut widgets.noninteractive, &mut widgets.inactive, &mut widgets.hovered, &mut widgets.active, &mut widgets.open] {
        w.corner_radius = CornerRadius::same(10);
    }
    widgets.noninteractive.bg_fill = SURFACE;
    widgets.noninteractive.bg_stroke = Stroke::new(1.0_f32, BORDER);
    widgets.noninteractive.fg_stroke = Stroke::new(1.0_f32, TEXT_DIM);
    widgets.inactive.bg_fill = SURFACE_HI;
    widgets.inactive.weak_bg_fill = SURFACE_HI;
    widgets.inactive.bg_stroke = Stroke::new(1.0_f32, BORDER);
    widgets.inactive.fg_stroke = Stroke::new(1.0_f32, TEXT);
    widgets.hovered.bg_fill = SURFACE_HOVER;
    widgets.hovered.weak_bg_fill = SURFACE_HOVER;
    widgets.hovered.bg_stroke = Stroke::new(1.0_f32, BORDER_HI);
    widgets.hovered.fg_stroke = Stroke::new(1.5_f32, TEXT);
    widgets.active.bg_fill = ACCENT_DIM;
    widgets.active.weak_bg_fill = ACCENT_DIM;
    widgets.active.bg_stroke = Stroke::new(1.0_f32, ACCENT);
    widgets.active.fg_stroke = Stroke::new(1.5_f32, Color32::WHITE);
    widgets.open.weak_bg_fill = SURFACE_HOVER;
    style.visuals = visuals;
    ctx.set_style(style);
}
