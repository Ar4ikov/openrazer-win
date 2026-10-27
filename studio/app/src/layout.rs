//! Where each key of a Razer keyboard physically is.
//!
//! Razer keyboards address their LEDs as a matrix of rows and columns; a full
//! size board is 6 x 22, with the numpad in the last columns and M1-M5 in
//! column 0 on boards that have macro keys.  The matrix says nothing about
//! the size or place of a key, so this table does, in key units (1u = one
//! letter key), for the standard full-size board.  Smaller boards use the
//! same matrix positions, so clipping the table to their columns gives a
//! tenkeyless board, and so on.

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct KeyCap {
    pub row: u8,
    pub col: u8,
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
    pub label: &'static str,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum Variant {
    /// The long left Shift and the backslash above Enter.
    Ansi,
    /// The tall Enter and an extra key beside a short left Shift.
    Iso,
}

#[derive(Clone, Debug, Default)]
pub struct Layout {
    pub keys: Vec<KeyCap>,
    pub width: f32,
    pub height: f32,
}

/// `(row, column, x, y, width, height, label)` for keys every board shares.
#[rustfmt::skip]
const COMMON: &[(u8, u8, f32, f32, f32, f32, &str)] = &[
    (0, 1, 0.0, 0.0, 1.0, 1.0, "Esc"),
    (0, 3, 2.0, 0.0, 1.0, 1.0, "F1"), (0, 4, 3.0, 0.0, 1.0, 1.0, "F2"), (0, 5, 4.0, 0.0, 1.0, 1.0, "F3"), (0, 6, 5.0, 0.0, 1.0, 1.0, "F4"),
    (0, 7, 6.5, 0.0, 1.0, 1.0, "F5"), (0, 8, 7.5, 0.0, 1.0, 1.0, "F6"), (0, 9, 8.5, 0.0, 1.0, 1.0, "F7"), (0, 10, 9.5, 0.0, 1.0, 1.0, "F8"),
    (0, 11, 11.0, 0.0, 1.0, 1.0, "F9"), (0, 12, 12.0, 0.0, 1.0, 1.0, "F10"), (0, 13, 13.0, 0.0, 1.0, 1.0, "F11"), (0, 14, 14.0, 0.0, 1.0, 1.0, "F12"),
    (0, 15, 15.25, 0.0, 1.0, 1.0, "PrtSc"), (0, 16, 16.25, 0.0, 1.0, 1.0, "ScrLk"), (0, 17, 17.25, 0.0, 1.0, 1.0, "Pause"),

    (1, 1, 0.0, 1.25, 1.0, 1.0, "`"), (1, 2, 1.0, 1.25, 1.0, 1.0, "1"), (1, 3, 2.0, 1.25, 1.0, 1.0, "2"), (1, 4, 3.0, 1.25, 1.0, 1.0, "3"),
    (1, 5, 4.0, 1.25, 1.0, 1.0, "4"), (1, 6, 5.0, 1.25, 1.0, 1.0, "5"), (1, 7, 6.0, 1.25, 1.0, 1.0, "6"), (1, 8, 7.0, 1.25, 1.0, 1.0, "7"),
    (1, 9, 8.0, 1.25, 1.0, 1.0, "8"), (1, 10, 9.0, 1.25, 1.0, 1.0, "9"), (1, 11, 10.0, 1.25, 1.0, 1.0, "0"), (1, 12, 11.0, 1.25, 1.0, 1.0, "-"),
    (1, 13, 12.0, 1.25, 1.0, 1.0, "="), (1, 14, 13.0, 1.25, 2.0, 1.0, "Backspace"),
    (1, 15, 15.25, 1.25, 1.0, 1.0, "Ins"), (1, 16, 16.25, 1.25, 1.0, 1.0, "Home"), (1, 17, 17.25, 1.25, 1.0, 1.0, "PgUp"),
    (1, 18, 18.5, 1.25, 1.0, 1.0, "Num"), (1, 19, 19.5, 1.25, 1.0, 1.0, "/"), (1, 20, 20.5, 1.25, 1.0, 1.0, "*"), (1, 21, 21.5, 1.25, 1.0, 1.0, "-"),

    (2, 1, 0.0, 2.25, 1.5, 1.0, "Tab"), (2, 2, 1.5, 2.25, 1.0, 1.0, "Q"), (2, 3, 2.5, 2.25, 1.0, 1.0, "W"), (2, 4, 3.5, 2.25, 1.0, 1.0, "E"),
    (2, 5, 4.5, 2.25, 1.0, 1.0, "R"), (2, 6, 5.5, 2.25, 1.0, 1.0, "T"), (2, 7, 6.5, 2.25, 1.0, 1.0, "Y"), (2, 8, 7.5, 2.25, 1.0, 1.0, "U"),
    (2, 9, 8.5, 2.25, 1.0, 1.0, "I"), (2, 10, 9.5, 2.25, 1.0, 1.0, "O"), (2, 11, 10.5, 2.25, 1.0, 1.0, "P"), (2, 12, 11.5, 2.25, 1.0, 1.0, "["),
    (2, 13, 12.5, 2.25, 1.0, 1.0, "]"),
    (2, 15, 15.25, 2.25, 1.0, 1.0, "Del"), (2, 16, 16.25, 2.25, 1.0, 1.0, "End"), (2, 17, 17.25, 2.25, 1.0, 1.0, "PgDn"),
    (2, 18, 18.5, 2.25, 1.0, 1.0, "7"), (2, 19, 19.5, 2.25, 1.0, 1.0, "8"), (2, 20, 20.5, 2.25, 1.0, 1.0, "9"), (2, 21, 21.5, 2.25, 1.0, 2.0, "+"),

    (3, 1, 0.0, 3.25, 1.75, 1.0, "Caps"), (3, 2, 1.75, 3.25, 1.0, 1.0, "A"), (3, 3, 2.75, 3.25, 1.0, 1.0, "S"), (3, 4, 3.75, 3.25, 1.0, 1.0, "D"),
    (3, 5, 4.75, 3.25, 1.0, 1.0, "F"), (3, 6, 5.75, 3.25, 1.0, 1.0, "G"), (3, 7, 6.75, 3.25, 1.0, 1.0, "H"), (3, 8, 7.75, 3.25, 1.0, 1.0, "J"),
    (3, 9, 8.75, 3.25, 1.0, 1.0, "K"), (3, 10, 9.75, 3.25, 1.0, 1.0, "L"), (3, 11, 10.75, 3.25, 1.0, 1.0, ";"), (3, 12, 11.75, 3.25, 1.0, 1.0, "'"),
    (3, 18, 18.5, 3.25, 1.0, 1.0, "4"), (3, 19, 19.5, 3.25, 1.0, 1.0, "5"), (3, 20, 20.5, 3.25, 1.0, 1.0, "6"),

    (4, 3, 2.25, 4.25, 1.0, 1.0, "Z"), (4, 4, 3.25, 4.25, 1.0, 1.0, "X"), (4, 5, 4.25, 4.25, 1.0, 1.0, "C"), (4, 6, 5.25, 4.25, 1.0, 1.0, "V"),
    (4, 7, 6.25, 4.25, 1.0, 1.0, "B"), (4, 8, 7.25, 4.25, 1.0, 1.0, "N"), (4, 9, 8.25, 4.25, 1.0, 1.0, "M"), (4, 10, 9.25, 4.25, 1.0, 1.0, ","),
    (4, 11, 10.25, 4.25, 1.0, 1.0, "."), (4, 12, 11.25, 4.25, 1.0, 1.0, "/"), (4, 14, 12.25, 4.25, 2.75, 1.0, "Shift"),
    (4, 16, 16.25, 4.25, 1.0, 1.0, "↑"),
    (4, 18, 18.5, 4.25, 1.0, 1.0, "1"), (4, 19, 19.5, 4.25, 1.0, 1.0, "2"), (4, 20, 20.5, 4.25, 1.0, 1.0, "3"), (4, 21, 21.5, 4.25, 1.0, 2.0, "Enter"),

    (5, 1, 0.0, 5.25, 1.25, 1.0, "Ctrl"), (5, 2, 1.25, 5.25, 1.25, 1.0, "Win"), (5, 3, 2.5, 5.25, 1.25, 1.0, "Alt"),
    (5, 7, 3.75, 5.25, 6.25, 1.0, ""), (5, 11, 10.0, 5.25, 1.25, 1.0, "Alt"), (5, 12, 11.25, 5.25, 1.25, 1.0, "Fn"),
    (5, 13, 12.5, 5.25, 1.25, 1.0, "Menu"), (5, 14, 13.75, 5.25, 1.25, 1.0, "Ctrl"),
    (5, 15, 15.25, 5.25, 1.0, 1.0, "←"), (5, 16, 16.25, 5.25, 1.0, 1.0, "↓"), (5, 17, 17.25, 5.25, 1.0, 1.0, "→"),
    (5, 19, 18.5, 5.25, 2.0, 1.0, "0"), (5, 20, 20.5, 5.25, 1.0, 1.0, "."),
];

#[rustfmt::skip]
const ANSI: &[(u8, u8, f32, f32, f32, f32, &str)] = &[
    (2, 14, 13.5, 2.25, 1.5, 1.0, "\\"),
    (3, 14, 12.75, 3.25, 2.25, 1.0, "Enter"),
    (4, 1, 0.0, 4.25, 2.25, 1.0, "Shift"),
];

#[rustfmt::skip]
const ISO: &[(u8, u8, f32, f32, f32, f32, &str)] = &[
    (3, 13, 12.75, 3.25, 1.0, 1.0, "#"),
    (3, 14, 13.75, 2.25, 1.25, 2.0, "Enter"),
    (4, 1, 0.0, 4.25, 1.25, 1.0, "Shift"),
    (4, 2, 1.25, 4.25, 1.0, 1.0, "\\"),
];

/// M1-M5, `gap` key units left of the board and `width` wide, on keyboards
/// that have them.  Boards differ in both, so the editor looks for them.
pub fn macro_keys(gap: f32, width: f32, rows: usize) -> Vec<KeyCap> {
    const NAMES: [&str; 5] = ["M1", "M2", "M3", "M4", "M5"];
    let shift = if rows == 5 { 1 } else { 0 };
    (0..5u8)
        .filter(|i| ((i + 1 - shift) as usize) < rows)
        .map(|i| KeyCap { row: i + 1 - shift, col: 0, x: -gap - width, y: 1.25 + i as f32, w: width, h: 1.0, label: NAMES[i as usize] })
        .collect()
}

/// The keys of a keyboard whose matrix is `rows` x `columns`.
///
/// A five-row matrix is a board without a function row: its rows are the
/// physical rows below one.  Keys beyond the matrix are left off, so a 6 x 18
/// board comes out tenkeyless.
pub fn keyboard(rows: usize, columns: usize, variant: Variant, macro_column: bool) -> Layout {
    let shift_down = rows == 5;
    let extra = match variant {
        Variant::Ansi => ANSI,
        Variant::Iso => ISO,
    };
    let mut keys: Vec<KeyCap> = Vec::new();
    for &(row, col, x, y, w, h, label) in COMMON.iter().chain(extra) {
        let matrix_row = if shift_down {
            if row == 0 {
                continue;
            }
            row - 1
        } else {
            row
        };
        if matrix_row as usize >= rows || col as usize >= columns {
            continue;
        }
        let label = if shift_down && row == 1 && col == 1 { "Esc" } else { label };
        keys.push(KeyCap { row: matrix_row, col, x, y, w, h, label });
    }
    if macro_column {
        keys.extend(macro_keys(0.2, 1.0, rows.min(6)).into_iter().map(|k| KeyCap { y: k.y - if shift_down { 1.25 } else { 0.0 }, ..k }));
    }
    normalise(keys)
}

/// `layout` with `keys` added beside it.
pub fn with_keys(layout: &Layout, keys: &[KeyCap]) -> Layout {
    let mut all = layout.keys.clone();
    all.extend_from_slice(keys);
    // Measured as before: the board's own width, which the logo is centred on.
    Layout { keys: all, width: layout.width, height: layout.height }
}

/// Shift the keys so the board starts at 0,0, and measure it.
fn normalise(mut keys: Vec<KeyCap>) -> Layout {
    if keys.is_empty() {
        return Layout::default();
    }
    let left = keys.iter().map(|k| k.x).fold(f32::INFINITY, f32::min);
    let top = keys.iter().map(|k| k.y).fold(f32::INFINITY, f32::min);
    for key in &mut keys {
        key.x -= left;
        key.y -= top;
    }
    let width = keys.iter().map(|k| k.x + k.w).fold(0.0, f32::max);
    let height = keys.iter().map(|k| k.y + k.h).fold(0.0, f32::max);
    Layout { keys, width, height }
}

/// A Razer Tartarus or Orbweaver: four rows of five, a thumb pad and a
/// direction pad, in the positions their matrix gives (upstream's keymaps).
pub fn keypad(rows: usize, columns: usize) -> Layout {
    #[rustfmt::skip]
    const PAD: &[(u8, u8, f32, f32, f32, f32, &str)] = &[
        (0, 0, 0.0, 0.0, 1.0, 1.0, "01"), (0, 1, 1.0, 0.0, 1.0, 1.0, "02"), (0, 2, 2.0, 0.0, 1.0, 1.0, "03"), (0, 3, 3.0, 0.0, 1.0, 1.0, "04"), (0, 4, 4.0, 0.0, 1.0, 1.0, "05"),
        (1, 0, 0.0, 1.0, 1.0, 1.0, "06"), (1, 1, 1.0, 1.0, 1.0, 1.0, "07"), (1, 2, 2.0, 1.0, 1.0, 1.0, "08"), (1, 3, 3.0, 1.0, 1.0, 1.0, "09"), (1, 4, 4.0, 1.0, 1.0, 1.0, "10"),
        (2, 0, 0.0, 2.0, 1.0, 1.0, "11"), (2, 1, 1.0, 2.0, 1.0, 1.0, "12"), (2, 2, 2.0, 2.0, 1.0, 1.0, "13"), (2, 3, 3.0, 2.0, 1.0, 1.0, "14"), (2, 4, 4.0, 2.0, 1.0, 1.0, "15"),
        (3, 0, 0.25, 3.0, 1.0, 1.0, "16"), (3, 1, 1.25, 3.0, 1.0, 1.0, "17"), (3, 2, 2.25, 3.0, 1.0, 1.0, "18"), (3, 3, 3.25, 3.0, 1.0, 1.0, "19"), (3, 4, 4.25, 3.0, 1.0, 1.0, "20"),
        (2, 5, 5.25, 3.25, 1.25, 1.0, "Space"), (1, 5, 5.5, 1.75, 1.0, 1.0, "◀"), (1, 7, 7.0, 1.75, 1.0, 1.0, "▶"),
        (0, 6, 6.25, 1.0, 1.0, 0.75, "▲"), (2, 6, 6.25, 2.5, 1.0, 0.75, "▼"), (2, 7, 7.25, 3.25, 1.0, 1.0, "Mode"),
    ];
    let keys = PAD
        .iter()
        .filter(|(row, col, ..)| (*row as usize) < rows && (*col as usize) < columns)
        .map(|&(row, col, x, y, w, h, label)| KeyCap { row, col, x, y, w, h, label })
        .collect();
    normalise(keys)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn find(layout: &Layout, row: u8, col: u8) -> Option<KeyCap> {
        layout.keys.iter().copied().find(|k| k.row == row && k.col == col)
    }

    #[test]
    fn a_full_size_board_is_twenty_two_and_a_half_keys_wide() {
        let layout = keyboard(6, 22, Variant::Ansi, false);
        assert_eq!(layout.width, 22.5);
        assert_eq!(layout.height, 6.25);
        assert_eq!(find(&layout, 3, 2).unwrap().label, "A");
        assert_eq!(find(&layout, 5, 7).unwrap().w, 6.25);
        assert!(find(&layout, 3, 13).is_none(), "no ISO # key on ANSI");
    }

    #[test]
    fn no_two_keys_overlap_or_share_an_led() {
        for variant in [Variant::Ansi, Variant::Iso] {
            let layout = keyboard(6, 22, variant, true);
            for (i, a) in layout.keys.iter().enumerate() {
                for b in &layout.keys[i + 1..] {
                    assert!((a.row, a.col) != (b.row, b.col), "{a:?} and {b:?} share an LED");
                    let overlap = a.x < b.x + b.w - 0.01 && b.x < a.x + a.w - 0.01 && a.y < b.y + b.h - 0.01 && b.y < a.y + a.h - 0.01;
                    assert!(!overlap, "{a:?} overlaps {b:?}");
                }
            }
        }
    }

    #[test]
    fn a_narrower_matrix_is_a_tenkeyless_board() {
        let layout = keyboard(6, 18, Variant::Ansi, false);
        assert!(layout.keys.iter().all(|k| k.col < 18));
        assert_eq!(layout.width, 18.25);
    }

    #[test]
    fn macro_keys_sit_left_of_the_board() {
        let layout = keyboard(6, 22, Variant::Ansi, true);
        let m1 = find(&layout, 1, 0).unwrap();
        assert_eq!((m1.x, m1.label), (0.0, "M1"));
        assert_eq!(find(&layout, 1, 1).unwrap().x, 1.2);
        assert_eq!(macro_keys(0.3, 0.9, 6)[0].x, -1.2);
    }

    #[test]
    fn a_five_row_board_has_no_function_row() {
        let layout = keyboard(5, 15, Variant::Ansi, false);
        let first = find(&layout, 0, 1).unwrap();
        assert_eq!((first.label, first.y), ("Esc", 0.0));
        assert!(layout.keys.iter().all(|k| !(k.label.len() > 1 && k.label.starts_with('F') && k.label[1..].chars().all(|c| c.is_ascii_digit()))));
    }
}
