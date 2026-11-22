//! English, Russian and Simplified Chinese.
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
#[path = "zh.rs"]
mod zh;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Lang {
    /// Whatever Windows is set to.
    #[default]
    System,
    En,
    Ru,
    Zh,
}

/// What `CURRENT` holds: the language in force, `System` resolved.
const EN: u8 = 0;
const RU: u8 = 1;
const ZH: u8 = 2;

static CURRENT: AtomicU8 = AtomicU8::new(EN);

/// Use `lang` from now on.
pub fn set(lang: Lang) {
    let resolved = match lang {
        Lang::System => system(),
        other => other,
    };
    CURRENT.store(
        match resolved {
            Lang::Ru => RU,
            Lang::Zh => ZH,
            _ => EN,
        },
        Ordering::Relaxed,
    );
}

pub fn current() -> Lang {
    match CURRENT.load(Ordering::Relaxed) {
        RU => Lang::Ru,
        ZH => Lang::Zh,
        _ => Lang::En,
    }
}

/// The language Windows shows its own menus in.
pub fn system() -> Lang {
    #[cfg(windows)]
    {
        let id = unsafe { windows_sys::Win32::Globalization::GetUserDefaultUILanguage() };
        match id & 0x3FF {
            0x19 => return Lang::Ru,
            0x04 => return Lang::Zh,
            _ => {}
        }
    }
    Lang::En
}

fn table(lang: Lang) -> &'static [(&'static str, &'static str)] {
    match lang {
        Lang::Ru => ru::RU,
        Lang::Zh => zh::ZH,
        _ => &[],
    }
}

fn map(lang: Lang) -> &'static HashMap<&'static str, &'static str> {
    static RU_MAP: OnceLock<HashMap<&'static str, &'static str>> = OnceLock::new();
    static ZH_MAP: OnceLock<HashMap<&'static str, &'static str>> = OnceLock::new();
    static EMPTY: OnceLock<HashMap<&'static str, &'static str>> = OnceLock::new();
    match lang {
        Lang::Ru => RU_MAP.get_or_init(|| ru::RU.iter().copied().collect()),
        Lang::Zh => ZH_MAP.get_or_init(|| zh::ZH.iter().copied().collect()),
        _ => EMPTY.get_or_init(HashMap::new),
    }
}

/// `english`, in the current language.
pub fn t(english: &'static str) -> &'static str {
    if let Some(translated) = map(current()).get(english) {
        return translated;
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

/// Every English string with a translation, in every language, for the tests
/// that check the placeholders agree and that each translation is complete.
pub fn pairs() -> impl Iterator<Item = (&'static str, &'static str)> {
    table(Lang::Ru)
        .iter()
        .chain(table(Lang::Zh).iter())
        .copied()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;
    use std::sync::Mutex;

    /// The language is one for the whole process, and tests run side by side:
    /// a test that sets it holds this, or another's choice lands mid-test.
    static LANGUAGE: Mutex<()> = Mutex::new(());

    #[test]
    fn placeholders_match_in_every_translation() {
        for (english, translated) in pairs() {
            assert_eq!(english.matches("{}").count(), translated.matches("{}").count(), "{english:?} / {translated:?}");
        }
    }

    #[test]
    fn no_string_is_translated_twice() {
        for lang in [Lang::Ru, Lang::Zh] {
            let mut seen = HashSet::new();
            for (english, _) in table(lang) {
                assert!(seen.insert(*english), "translated twice in {lang:?}: {english:?}");
            }
        }
    }

    #[test]
    fn every_language_covers_the_same_strings() {
        let english: HashSet<&str> = table(Lang::Ru).iter().map(|(key, _)| *key).collect();
        for lang in [Lang::Ru, Lang::Zh] {
            let translated: HashSet<&str> = table(lang).iter().map(|(key, _)| *key).collect();
            let missing: Vec<_> = english.difference(&translated).take(5).collect();
            assert!(missing.is_empty(), "{lang:?} is missing {missing:?}");
            let extra: Vec<_> = translated.difference(&english).take(5).collect();
            assert!(extra.is_empty(), "{lang:?} has extra strings {extra:?}");
        }
    }

    #[test]
    fn arguments_fill_the_placeholders_in_order() {
        let _language = LANGUAGE.lock().unwrap_or_else(|e| e.into_inner());
        set(Lang::En);
        assert_eq!(tf("{} of {}", &[&1, &2]), "1 of 2");
    }

    #[test]
    fn chinese_and_russian_are_looked_up() {
        let _language = LANGUAGE.lock().unwrap_or_else(|e| e.into_inner());
        set(Lang::Zh);
        assert_eq!(t("Settings"), "设置");
        set(Lang::Ru);
        assert_eq!(t("Settings"), "Настройки");
        set(Lang::En);
        assert_eq!(t("Settings"), "Settings");
    }
}
