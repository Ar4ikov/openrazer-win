//! English and Russian.
//!
//! Strings are written in English in the code and looked up in a table here,
//! gettext-style, so the code reads plainly and a missing translation falls
//! back to English rather than to a key.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::OnceLock;

#[path = "ru.rs"]
mod ru;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Lang {
    /// Whatever Windows is set to.
    #[default]
    System,
    En,
    Ru,
}

static CURRENT: AtomicU8 = AtomicU8::new(0);

/// Use `lang` from now on.
pub fn set(lang: Lang) {
    let resolved = match lang {
        Lang::System => system(),
        other => other,
    };
    CURRENT.store(if resolved == Lang::Ru { 1 } else { 0 }, Ordering::Relaxed);
}

pub fn current() -> Lang {
    if CURRENT.load(Ordering::Relaxed) == 1 {
        Lang::Ru
    } else {
        Lang::En
    }
}

/// The language Windows shows its own menus in.
pub fn system() -> Lang {
    #[cfg(windows)]
    {
        let id = unsafe { windows_sys::Win32::Globalization::GetUserDefaultUILanguage() };
        if id & 0x3FF == 0x19 {
            return Lang::Ru;
        }
    }
    Lang::En
}

fn table() -> &'static HashMap<&'static str, &'static str> {
    static TABLE: OnceLock<HashMap<&'static str, &'static str>> = OnceLock::new();
    TABLE.get_or_init(|| ru::RU.iter().copied().collect())
}

/// `english`, in the current language.
pub fn t(english: &'static str) -> &'static str {
    if current() == Lang::Ru {
        if let Some(russian) = table().get(english) {
            return russian;
        }
    }
    english
}

/// `english` translated, with each `{}` replaced by the next argument.
pub fn tf(english: &'static str, args: &[&dyn std::fmt::Display]) -> String {
    let mut out = String::new();
    let mut args = args.iter();
    let mut rest = t(english);
    while let Some(at) = rest.find("{}") {
        out.push_str(&rest[..at]);
        if let Some(arg) = args.next() {
            out.push_str(&arg.to_string());
        }
        rest = &rest[at + 2..];
    }
    out.push_str(rest);
    out
}

/// Every English string with a translation, for the test that checks the
/// placeholders agree.
pub fn pairs() -> &'static [(&'static str, &'static str)] {
    ru::RU
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn placeholders_match_in_every_translation() {
        for (english, russian) in pairs() {
            assert_eq!(english.matches("{}").count(), russian.matches("{}").count(), "{english:?} / {russian:?}");
        }
    }

    #[test]
    fn no_string_is_translated_twice() {
        let mut seen = std::collections::HashSet::new();
        for (english, _) in pairs() {
            assert!(seen.insert(*english), "translated twice: {english:?}");
        }
    }

    #[test]
    fn arguments_fill_the_placeholders_in_order() {
        set(Lang::En);
        assert_eq!(tf("{} of {}", &[&1, &2]), "1 of 2");
    }
}
