//! The SDK's effect structures, read out of a game's memory into the JSON
//! Razer's REST API takes.
//!
//! Every layout here is from Razer's `RzChromaSDKTypes.h`. Colours are
//! `COLORREF`s and are passed on as they are; the daemon reads them.

use std::ffi::c_void;
use std::fmt::Write;

pub type RzResult = i32;
pub const SUCCESS: RzResult = 0;
pub const NOT_SUPPORTED: RzResult = 50;
pub const INVALID_PARAMETER: RzResult = 87;
pub const NOT_FOUND: RzResult = 1168;

/// The kinds of device the SDK draws on, by the name the REST API gives them.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kind {
    Keyboard,
    Mouse,
    Headset,
    Mousepad,
    Keypad,
    ChromaLink,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Keyboard => "keyboard",
            Kind::Mouse => "mouse",
            Kind::Headset => "headset",
            Kind::Mousepad => "mousepad",
            Kind::Keypad => "keypad",
            Kind::ChromaLink => "chromalink",
        }
    }

    /// The SDK's `DEVICE_TYPE` for it.
    pub fn device_type(self) -> i32 {
        match self {
            Kind::Keyboard => 1,
            Kind::Mouse => 2,
            Kind::Headset => 3,
            Kind::Mousepad => 4,
            Kind::Keypad => 5,
            Kind::ChromaLink => 8,
        }
    }

    pub const ALL: [Kind; 6] = [Kind::Keyboard, Kind::Mouse, Kind::Headset, Kind::Mousepad, Kind::Keypad, Kind::ChromaLink];
}

/// The grid each kind is drawn on, as rows and columns.
fn grid(kind: Kind) -> (usize, usize) {
    match kind {
        Kind::Keyboard => (6, 22),
        Kind::Mouse => (9, 7),
        Kind::Headset => (1, 5),
        Kind::Mousepad => (1, 15),
        Kind::Keypad => (4, 5),
        Kind::ChromaLink => (1, 5),
    }
}

/// `count` `u32`s at `at`.
///
/// # Safety
/// `at` must point at that many readable `u32`s.
unsafe fn read(at: *const u32, count: usize) -> Vec<u32> {
    std::slice::from_raw_parts(at, count).to_vec()
}

fn rows(values: &[u32], columns: usize, mask: u32) -> String {
    let mut out = String::from("[");
    for (r, row) in values.chunks(columns).enumerate() {
        if r > 0 {
            out.push(',');
        }
        out.push_str(&strip(row, mask));
    }
    out.push(']');
    out
}

fn strip(values: &[u32], mask: u32) -> String {
    let mut out = String::from("[");
    for (i, value) in values.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        let _ = write!(out, "{}", value & mask);
    }
    out.push(']');
    out
}

const COLOUR: u32 = 0x00FF_FFFF;
const ANY: u32 = u32::MAX;

fn none() -> String {
    r#"{"effect":"CHROMA_NONE"}"#.to_string()
}

fn still(colour: u32) -> String {
    format!(r#"{{"effect":"CHROMA_STATIC","param":{{"color":{}}}}}"#, colour & COLOUR)
}

fn custom(name: &str, param: String) -> String {
    format!(r#"{{"effect":"{name}","param":{param}}}"#)
}

/// Where each of the old mouse layout's LEDs sits on the 9 x 7 grid: the
/// scroll wheel, the logo, the backlight, then the side strips, seven a side.
fn mouse_cell(led: usize) -> Option<(usize, usize)> {
    match led {
        1 => Some((2, 3)),
        2 => Some((7, 3)),
        3 => Some((4, 3)),
        4..=10 => Some((led - 3, 0)),
        11..=17 => Some((led - 10, 6)),
        _ => None,
    }
}

/// The REST body for one of a kind's effects, or the SDK's reason there is none.
///
/// # Safety
/// `param` must point at the structure `effect` takes for `kind`, or be null.
pub unsafe fn body(kind: Kind, effect: i32, param: *const c_void) -> Result<String, RzResult> {
    let (height, width) = grid(kind);
    let needs = |param: *const c_void| if param.is_null() { Err(INVALID_PARAMETER) } else { Ok(param as *const u32) };
    match (kind, effect) {
        (_, 0) => Ok(none()),
        // Keyboard: CUSTOM, STATIC, CUSTOM_KEY, CUSTOM2.
        (Kind::Keyboard, 2) => Ok(custom("CHROMA_CUSTOM", rows(&read(needs(param)?, 6 * 22), 22, COLOUR))),
        (Kind::Keyboard, 4) => Ok(still(*needs(param)?)),
        (Kind::Keyboard, 8) => {
            let at = needs(param)?;
            let colour = rows(&read(at, 6 * 22), 22, COLOUR);
            let keys = rows(&read(at.add(6 * 22), 6 * 22), 22, ANY);
            Ok(custom("CHROMA_CUSTOM_KEY", format!(r#"{{"color":{colour},"key":{keys}}}"#)))
        }
        (Kind::Keyboard, 9) => {
            let at = needs(param)?;
            let colour = rows(&read(at, 8 * 24), 24, COLOUR);
            let keys = rows(&read(at.add(8 * 24), 6 * 22), 22, ANY);
            Ok(custom("CHROMA_CUSTOM2", format!(r#"{{"color":{colour},"key":{keys}}}"#)))
        }
        // Mouse: the old CUSTOM by LED, STATIC with an LED id first, CUSTOM2.
        (Kind::Mouse, 3) => {
            let leds = read(needs(param)?, 30);
            let mut cells = vec![0u32; 9 * 7];
            for (led, colour) in leds.iter().enumerate() {
                if let Some((row, column)) = mouse_cell(led) {
                    cells[row * 7 + column] = *colour;
                }
            }
            Ok(custom("CHROMA_CUSTOM2", rows(&cells, 7, COLOUR)))
        }
        (Kind::Mouse, 6) => Ok(still(*needs(param)?.add(1))),
        (Kind::Mouse, 8) => Ok(custom("CHROMA_CUSTOM2", rows(&read(needs(param)?, 9 * 7), 7, COLOUR))),
        (Kind::Headset, 1) | (Kind::Mousepad, 4) | (Kind::Keypad, 5) | (Kind::ChromaLink, 2) => Ok(still(*needs(param)?)),
        (Kind::Headset, 4) | (Kind::ChromaLink, 1) => Ok(custom("CHROMA_CUSTOM", strip(&read(needs(param)?, 5), COLOUR))),
        (Kind::Mousepad, 2) => Ok(custom("CHROMA_CUSTOM", strip(&read(needs(param)?, 15), COLOUR))),
        (Kind::Mousepad, 6) => Ok(custom("CHROMA_CUSTOM2", strip(&read(needs(param)?, 20), COLOUR))),
        (Kind::Keypad, 2) => Ok(custom("CHROMA_CUSTOM", rows(&read(needs(param)?, height * width), width, COLOUR))),
        // The rest -- breathing, waves and the like -- were deprecated by Razer
        // years ago, and games draw those themselves now.
        _ => Err(NOT_SUPPORTED),
    }
}

/// The generic effects of `CreateEffect`, which name a device rather than a kind.
///
/// # Safety
/// As for [`body`], with the generic structures: each starts with a size and
/// a parameter word.
pub unsafe fn generic_body(kind: Kind, effect: i32, param: *const c_void) -> Result<String, RzResult> {
    let header = std::mem::size_of::<usize>() + 4;
    match effect {
        0 => Ok(none()),
        6 if !param.is_null() => Ok(still(*((param as *const u8).add(header) as *const u32))),
        7 if !param.is_null() => {
            // A 30 x 30 grid, of which the kind uses the top left.
            let (height, width) = grid(kind);
            let all = read((param as *const u8).add(header) as *const u32, 30 * 30);
            let cells: Vec<u32> = (0..height).flat_map(|r| all[r * 30..r * 30 + width].to_vec()).collect();
            if height == 1 {
                Ok(custom("CHROMA_CUSTOM", strip(&cells, COLOUR)))
            } else {
                Ok(custom("CHROMA_CUSTOM", rows(&cells, width, COLOUR)))
            }
        }
        6 | 7 => Err(INVALID_PARAMETER),
        _ => Err(NOT_SUPPORTED),
    }
}

/// The kind of device a product's GUID names, from Razer's
/// `RzChromaSDKDefines.h`, by its first field.
pub fn kind_of_device(data1: u32) -> Option<Kind> {
    const KEYBOARDS: [u32; 11] = [0x2EA1BB63, 0xED1C1B82, 0x18C5AD9B, 0x872AB2A9, 0x5AF60076, 0x2D84DD51, 0x803378C1, 0xC83BDFE8, 0xF2BEDFAF, 0xA73AC338, 0x608E743F];
    const MICE: [u32; 9] = [0xAEC50D91, 0x7EC00450, 0xFF8A5929, 0xD527CBDC, 0xD714C50B, 0xF1876328, 0x52C15681, 0x195D70F5, 0x77834867];
    const HEADSETS: [u32; 3] = [0xCD1E09A5, 0xDF3164D7, 0x7FB8A36E];
    const KEYPADS: [u32; 2] = [0x00F0545C, 0x9D24B0AB];
    const LINKED: [u32; 4] = [0x35F6F18D, 0x47DB1FA7, 0x0201203B, 0xBB2E9C9B];
    if KEYBOARDS.contains(&data1) {
        Some(Kind::Keyboard)
    } else if MICE.contains(&data1) {
        Some(Kind::Mouse)
    } else if HEADSETS.contains(&data1) {
        Some(Kind::Headset)
    } else if data1 == 0x80F95A94 {
        Some(Kind::Mousepad)
    } else if KEYPADS.contains(&data1) {
        Some(Kind::Keypad)
    } else if LINKED.contains(&data1) {
        Some(Kind::ChromaLink)
    } else {
        None
    }
}

/// `text` as a JSON string, quotes included.
pub fn quoted(text: &str) -> String {
    let mut out = String::from("\"");
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_keyboard_grid_becomes_rows_of_colours() {
        let mut grid = [0u32; 6 * 22];
        grid[3 * 22 + 2] = 0x00FF00;
        let body = unsafe { body(Kind::Keyboard, 2, grid.as_ptr().cast()) }.unwrap();
        assert!(body.starts_with(r#"{"effect":"CHROMA_CUSTOM","param":[[0,0"#));
        assert!(body.contains("[0,0,65280,0"));
        assert_eq!(body.matches('[').count(), 7);
    }

    #[test]
    fn keys_keep_their_flag_and_colours_lose_their_top_byte() {
        let mut both = [0u32; 2 * 6 * 22];
        both[0] = 0xFF00_00FF;
        both[6 * 22] = 0x0100_FF00;
        let body = unsafe { body(Kind::Keyboard, 8, both.as_ptr().cast()) }.unwrap();
        assert!(body.contains(r#""color":[[255,"#));
        assert!(body.contains(r#""key":[[16842496,"#));
    }

    #[test]
    fn a_mouse_s_static_colour_comes_after_its_led() {
        let param = [0xFFFFu32, 0x0000FF];
        assert_eq!(unsafe { body(Kind::Mouse, 6, param.as_ptr().cast()) }.unwrap(), r#"{"effect":"CHROMA_STATIC","param":{"color":255}}"#);
    }

    #[test]
    fn the_old_mouse_layout_lands_on_the_grid() {
        let mut leds = [0u32; 30];
        leds[2] = 7; // the logo
        let body = unsafe { body(Kind::Mouse, 3, leds.as_ptr().cast()) }.unwrap();
        assert!(body.starts_with(r#"{"effect":"CHROMA_CUSTOM2""#));
        assert!(body.contains("[0,0,0,7,0,0,0]"));
    }

    #[test]
    fn what_razer_deprecated_is_not_supported_and_nothing_is_not_read() {
        assert_eq!(unsafe { body(Kind::Keyboard, 1, std::ptr::null()) }, Err(NOT_SUPPORTED));
        assert_eq!(unsafe { body(Kind::Keyboard, 2, std::ptr::null()) }, Err(INVALID_PARAMETER));
        assert_eq!(unsafe { body(Kind::Headset, 0, std::ptr::null()) }.unwrap(), r#"{"effect":"CHROMA_NONE"}"#);
    }

    #[test]
    fn a_generic_static_effect_skips_its_size_and_parameter() {
        #[repr(C)]
        struct Static {
            size: usize,
            param: u32,
            colour: u32,
        }
        let effect = Static { size: std::mem::size_of::<Static>(), param: 0, colour: 0x00FF00 };
        let body = unsafe { generic_body(Kind::Keyboard, 6, (&effect as *const Static).cast()) }.unwrap();
        assert_eq!(body, r#"{"effect":"CHROMA_STATIC","param":{"color":65280}}"#);
    }

    #[test]
    fn devices_are_known_by_their_guid() {
        assert_eq!(kind_of_device(0x2EA1BB63), Some(Kind::Keyboard));
        assert_eq!(kind_of_device(0x80F95A94), Some(Kind::Mousepad));
        assert_eq!(kind_of_device(0x12345678), None);
    }

    #[test]
    fn titles_are_quoted_for_json() {
        assert_eq!(quoted("A \"game\"\\"), r#""A \"game\"\\""#);
    }
}
