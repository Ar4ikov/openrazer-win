//! What the lighting looks like, frame by frame, for drawing on screen.
//!
//! Per-key layers are drawn with the same arithmetic as the daemon's
//! `effects/layers.py`, so the preview is what the keyboard shows. The
//! device's own effects run in its firmware and cannot be asked for a frame;
//! for those this is a good likeness.

use egui::Color32;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub const BREATH_PERIOD: f32 = 4.0;
pub const SPECTRUM_PERIOD: f32 = 6.0;
pub const WAVE_PERIOD: f32 = 3.0;
pub const STARLIGHT_PERIOD: f32 = 2.5;
pub const REACTIVE_FADE: f32 = 1.0;
pub const RIPPLE_DURATION: f32 = 0.8;

pub fn hue(h: f32) -> Color32 {
    openrazer_studio_core::widgets::hsv(h, 1.0, 1.0)
}

fn mix(a: Color32, b: Color32, t: f32) -> Color32 {
    let t = t.clamp(0.0, 1.0);
    let m = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t) as u8;
    Color32::from_rgb(m(a.r(), b.r()), m(a.g(), b.g()), m(a.b(), b.b()))
}

fn scale(c: Color32, k: f32) -> Color32 {
    let k = k.clamp(0.0, 1.0);
    Color32::from_rgb((c.r() as f32 * k) as u8, (c.g() as f32 * k) as u8, (c.b() as f32 * k) as u8)
}

/// The same fixed pseudo-random number per cell as the daemon's `twinkle_phase`.
pub fn twinkle_phase(row: u8, column: u8) -> f32 {
    let seed = (row as u32 * 31 + column as u32 * 17 + 7).wrapping_mul(2_654_435_761);
    seed as f64 as f32 / 4_294_967_296.0
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Static,
    Gradient,
    Breath,
    Spectrum,
    Wave,
    Starlight,
    Reactive,
    Ripple,
}

impl Kind {
    pub const ALL: [Kind; 8] = [Kind::Static, Kind::Gradient, Kind::Breath, Kind::Spectrum, Kind::Wave, Kind::Starlight, Kind::Reactive, Kind::Ripple];

    pub fn label(self) -> &'static str {
        use openrazer_studio_core::t;
        match self {
            Kind::Static => t("Static"),
            Kind::Gradient => t("Gradient"),
            Kind::Breath => t("Breathing"),
            Kind::Spectrum => t("Spectrum cycle"),
            Kind::Wave => t("Rainbow wave"),
            Kind::Starlight => t("Starlight"),
            Kind::Reactive => t("Reactive"),
            Kind::Ripple => t("Ripple"),
        }
    }

    pub fn hint(self) -> &'static str {
        use openrazer_studio_core::t;
        match self {
            Kind::Static => t("One steady colour."),
            Kind::Gradient => t("Fades from one colour to another, left to right."),
            Kind::Breath => t("Fades in and out, through each of its colours in turn."),
            Kind::Spectrum => t("Every colour of the rainbow, one after another."),
            Kind::Wave => t("A rainbow rolling across the keys."),
            Kind::Starlight => t("Keys twinkle on and off at random."),
            Kind::Reactive => t("A key lights up when pressed, then fades."),
            Kind::Ripple => t("A ring of light spreads out from each key pressed."),
        }
    }

    /// How many colours it uses, at the most.
    pub fn colours(self) -> usize {
        match self {
            Kind::Spectrum | Kind::Wave => 0,
            Kind::Static | Kind::Reactive | Kind::Ripple => 1,
            Kind::Gradient => 2,
            Kind::Breath | Kind::Starlight => 3,
        }
    }

    pub fn has_speed(self) -> bool {
        !matches!(self, Kind::Static | Kind::Gradient)
    }

    pub fn has_direction(self) -> bool {
        matches!(self, Kind::Wave | Kind::Gradient)
    }

    pub fn key_driven(self) -> bool {
        matches!(self, Kind::Reactive | Kind::Ripple)
    }
}

fn rgb(c: &Color32) -> [u8; 3] {
    [c.r(), c.g(), c.b()]
}

mod colour_list {
    use egui::Color32;
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S: Serializer>(colours: &[Color32], s: S) -> Result<S::Ok, S::Error> {
        colours.iter().map(super::rgb).collect::<Vec<_>>().serialize(s)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Color32>, D::Error> {
        let raw: Vec<Vec<i64>> = Vec::deserialize(d)?;
        Ok(raw.into_iter().map(|c| Color32::from_rgb(c.first().copied().unwrap_or(0) as u8, c.get(1).copied().unwrap_or(0) as u8, c.get(2).copied().unwrap_or(0) as u8)).collect())
    }
}

/// One layer of per-key lighting, as the daemon's `layers` effect takes it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Layer {
    pub kind: Kind,
    pub cells: Vec<(u8, u8)>,
    #[serde(with = "colour_list")]
    pub colours: Vec<Color32>,
    #[serde(default = "one")]
    pub speed: f32,
    #[serde(default = "first")]
    pub direction: u8,
    #[serde(default)]
    pub name: String,
    #[serde(default = "yes")]
    pub enabled: bool,
}

fn one() -> f32 {
    1.0
}
fn first() -> u8 {
    1
}
fn yes() -> bool {
    true
}

impl Layer {
    pub fn new(kind: Kind, cells: Vec<(u8, u8)>, colour: Color32) -> Layer {
        let colours = match kind.colours() {
            0 => vec![],
            1 => vec![colour],
            2 => vec![colour, Color32::from_rgb(0x00, 0x50, 0xFF)],
            _ => vec![colour],
        };
        Layer { kind, cells, colours, speed: 1.0, direction: 1, name: String::new(), enabled: true }
    }

    pub fn colour(&self, i: usize) -> Color32 {
        self.colours.get(i).or(self.colours.first()).copied().unwrap_or(Color32::from_rgb(0, 255, 0))
    }
}

/// Draw `layers` at `t` seconds; `presses` are `(age in seconds, cell)`.
pub fn render_layers(layers: &[Layer], t: f32, presses: &[(f32, (u8, u8))]) -> HashMap<(u8, u8), Color32> {
    let mut frame: HashMap<(u8, u8), Color32> = HashMap::new();
    for layer in layers.iter().filter(|l| l.enabled && !l.cells.is_empty()) {
        let first = layer.colour(0);
        let speed = layer.speed.clamp(0.1, 5.0);
        let left = layer.cells.iter().map(|c| c.1).min().unwrap_or(0) as f32;
        let right = layer.cells.iter().map(|c| c.1).max().unwrap_or(0) as f32;
        let width = (right - left).max(1.0);
        let below = |frame: &HashMap<(u8, u8), Color32>, cell: &(u8, u8)| frame.get(cell).copied().unwrap_or(Color32::BLACK);
        match layer.kind {
            Kind::Static => {
                for cell in &layer.cells {
                    frame.insert(*cell, first);
                }
            }
            Kind::Gradient => {
                let last = if layer.colours.len() > 1 { layer.colour(layer.colours.len() - 1) } else { Color32::BLACK };
                for cell in &layer.cells {
                    let mut position = (cell.1 as f32 - left) / width;
                    if layer.direction == 2 {
                        position = 1.0 - position;
                    }
                    frame.insert(*cell, mix(first, last, position));
                }
            }
            Kind::Breath => {
                let cycle = t * speed / BREATH_PERIOD;
                let colour = layer.colour(cycle as usize % layer.colours.len().max(1));
                let level = 0.5 - 0.5 * (cycle * std::f32::consts::TAU).cos();
                for cell in &layer.cells {
                    frame.insert(*cell, scale(colour, level));
                }
            }
            Kind::Spectrum => {
                let colour = hue(t * speed / SPECTRUM_PERIOD);
                for cell in &layer.cells {
                    frame.insert(*cell, colour);
                }
            }
            Kind::Wave => {
                let travel = t * speed / WAVE_PERIOD;
                let sign = if layer.direction == 2 { -1.0 } else { 1.0 };
                for cell in &layer.cells {
                    let position = (cell.1 as f32 - left) / (width + 1.0);
                    frame.insert(*cell, hue(position - sign * travel));
                }
            }
            Kind::Starlight => {
                for cell in &layer.cells {
                    let phase = twinkle_phase(cell.0, cell.1);
                    let cycle = t * speed / STARLIGHT_PERIOD + phase * 7.0;
                    let level = (cycle * std::f32::consts::TAU).sin().max(0.0).powi(6);
                    let colour = layer.colour((phase * 997.0) as usize % layer.colours.len().max(1));
                    frame.insert(*cell, mix(below(&frame, cell), colour, level));
                }
            }
            Kind::Reactive => {
                let fade = REACTIVE_FADE / speed;
                for cell in &layer.cells {
                    let level = presses.iter().filter(|(age, c)| c == cell && *age < fade).map(|(age, _)| 1.0 - age / fade).fold(0.0f32, f32::max);
                    if level > 0.0 {
                        frame.insert(*cell, mix(below(&frame, cell), first, level));
                    }
                }
            }
            Kind::Ripple => {
                let duration = RIPPLE_DURATION / speed;
                let reach = layer.cells.iter().map(|c| c.1).max().unwrap_or(1).max(1) as f32 + 1.0;
                for cell in &layer.cells {
                    let mut level = 0.0f32;
                    for (age, origin) in presses {
                        if *age >= duration {
                            continue;
                        }
                        let radius = age / duration * reach;
                        let distance = (((cell.0 as f32 - origin.0 as f32) * 2.0).powi(2) + (cell.1 as f32 - origin.1 as f32).powi(2)).sqrt();
                        let delta = (distance - radius).abs();
                        if delta < 1.2 {
                            level = level.max((1.0 - delta / 1.2) * (1.0 - age / duration));
                        }
                    }
                    if level > 0.0 {
                        frame.insert(*cell, mix(below(&frame, cell), first, level));
                    }
                }
            }
        }
    }
    frame
}

/// Whether drawing `layers` needs a new frame as time passes.
pub fn animated(layers: &[Layer]) -> bool {
    layers.iter().any(|l| l.enabled && !matches!(l.kind, Kind::Static | Kind::Gradient))
}

/// A likeness of a device effect at `t` seconds, for an LED `x` of the way
/// across the device (0-1) with its own fixed `seed`.
pub fn hardware(effect: &str, colours: &[Color32; 3], speed: i64, direction: i64, t: f32, x: f32, seed: f32) -> Color32 {
    let c1 = colours[0];
    match effect {
        "none" | "" => Color32::BLACK,
        "static" | "custom" => c1,
        "on" => Color32::from_rgb(0xE0, 0xF0, 0xDC),
        "spectrum" | "spectrum_soft" => hue(t / SPECTRUM_PERIOD),
        "wave" => hue(if direction == 2 { x + t / WAVE_PERIOD } else { x - t / WAVE_PERIOD }),
        "wave_soft" => scale(c1, 0.25 + 0.75 * (0.5 + 0.5 * ((x - t / 2.0) * std::f32::consts::TAU).cos())),
        "wheel" => hue(x * 0.5 + seed * 0.5 - t / WAVE_PERIOD),
        "breath_single" => scale(c1, 0.5 - 0.5 * (t / BREATH_PERIOD * std::f32::consts::TAU).cos()),
        "breath_dual" | "breath_triple" => {
            let count = if effect == "breath_dual" { 2 } else { 3 };
            let cycle = t / BREATH_PERIOD;
            scale(colours[cycle as usize % count], 0.5 - 0.5 * (cycle * std::f32::consts::TAU).cos())
        }
        "breath_random" => {
            let cycle = t / BREATH_PERIOD;
            scale(hue((cycle as u32).wrapping_mul(2_654_435_761) as f32 / 4_294_967_296.0), 0.5 - 0.5 * (cycle * std::f32::consts::TAU).cos())
        }
        "blinking" => {
            if ((t * 2.0) as u32).is_multiple_of(2) {
                c1
            } else {
                Color32::BLACK
            }
        }
        "reactive" | "ripple" => scale(c1, 0.18),
        "ripple_random" => scale(hue(seed), 0.18),
        e if e.starts_with("starlight") => {
            let period = STARLIGHT_PERIOD * (1.0 + (speed.clamp(1, 3) - 1) as f32 * 0.5);
            let level = ((t / period + seed * 7.0) * std::f32::consts::TAU).sin().max(0.0).powi(6);
            let colour = match e {
                "starlight_random" => hue(seed * 13.0),
                "starlight_dual" if seed > 0.5 => colours[1],
                _ => c1,
            };
            scale(colour, level)
        }
        _ => c1,
    }
}

/// Whether a device effect changes over time.
pub fn hardware_animated(effect: &str) -> bool {
    !matches!(effect, "none" | "" | "static" | "custom" | "on" | "reactive" | "ripple" | "ripple_random")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn layer(kind: Kind, cells: &[(u8, u8)], colours: &[Color32]) -> Layer {
        Layer { kind, cells: cells.to_vec(), colours: colours.to_vec(), speed: 1.0, direction: 1, name: String::new(), enabled: true }
    }

    #[test]
    fn the_twinkle_matches_the_daemon() {
        // test_layers.py: twinkle_phase(0, 0) == 0.3262379
        assert!((twinkle_phase(0, 0) - 0.326_237_9).abs() < 1e-5);
    }

    #[test]
    fn layers_cover_and_blend_like_the_daemon() {
        let red = Color32::from_rgb(250, 0, 0);
        let grey = Color32::from_rgb(10, 10, 10);
        let stack = [layer(Kind::Static, &[(2, 3), (2, 4)], &[grey]), layer(Kind::Reactive, &[(2, 3), (2, 4)], &[red])];
        let frame = render_layers(&stack, 0.0, &[(0.0, (2, 3))]);
        assert_eq!(frame[&(2, 3)], red);
        assert_eq!(frame[&(2, 4)], grey);
        let gradient = render_layers(&[layer(Kind::Gradient, &[(1, 2), (1, 7), (1, 12)], &[Color32::from_rgb(255, 0, 0), Color32::from_rgb(0, 0, 255)])], 0.0, &[]);
        assert_eq!(gradient[&(1, 7)], Color32::from_rgb(127, 0, 127));
    }

    #[test]
    fn layers_go_to_the_daemon_as_plain_json() {
        let stack = layer(Kind::Breath, &[(1, 1)], &[Color32::from_rgb(1, 2, 3)]);
        let json = serde_json::to_value(&stack).unwrap();
        assert_eq!(json["kind"], "breath");
        assert_eq!(json["cells"], serde_json::json!([[1, 1]]));
        assert_eq!(json["colours"], serde_json::json!([[1, 2, 3]]));
        let back: Layer = serde_json::from_value(json).unwrap();
        assert_eq!(back, stack);
    }
}
