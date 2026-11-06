//! Giving a game the stand-in for Razer's Chroma SDK DLL.
//!
//! The daemon answers Razer's REST API, which some games use. Most load the
//! SDK's DLL instead, and without Synapse there is none to load; a copy of
//! `studio/chroma` beside the game's program is found first and passes what
//! the game shows on to the daemon. Only a copy put there by Studio is ever
//! taken away again.

use std::path::{Path, PathBuf};

#[cfg(chroma)]
const DLL64: Option<&[u8]> = Some(include_bytes!(concat!(env!("OUT_DIR"), "/RzChromaSDK64.dll")));
#[cfg(not(chroma))]
const DLL64: Option<&[u8]> = None;
#[cfg(chroma)]
const DLL32: Option<&[u8]> = Some(include_bytes!(concat!(env!("OUT_DIR"), "/RzChromaSDK.dll")));
#[cfg(not(chroma))]
const DLL32: Option<&[u8]> = None;

/// In every copy of the stand-in, which exports it, and in none of Razer's.
const MARK: &[u8] = b"openrazer-chroma";

/// Whether this build of Studio carries the DLL to give.
pub fn available() -> bool {
    DLL64.is_some() && DLL32.is_some()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Bits {
    X64,
    X86,
}

impl Bits {
    pub fn dll_name(self) -> &'static str {
        match self {
            Bits::X64 => "RzChromaSDK64.dll",
            Bits::X86 => "RzChromaSDK.dll",
        }
    }

    fn dll(self) -> Option<&'static [u8]> {
        match self {
            Bits::X64 => DLL64,
            Bits::X86 => DLL32,
        }
    }
}

/// Whether the program in `header` -- its first kilobytes -- is 64- or 32-bit.
pub fn bits_of(header: &[u8]) -> Result<Bits, String> {
    let at = |offset: usize, len: usize| header.get(offset..offset + len);
    if at(0, 2) != Some(b"MZ") {
        return Err("not a Windows program".into());
    }
    let pe = at(0x3C, 4).map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]) as usize).ok_or("not a Windows program")?;
    if at(pe, 4) != Some(b"PE\0\0") {
        return Err("not a Windows program".into());
    }
    match at(pe + 4, 2).map(|b| u16::from_le_bytes([b[0], b[1]])) {
        Some(0x8664) => Ok(Bits::X64),
        Some(0x014C) => Ok(Bits::X86),
        _ => Err("a program for a processor games do not run on here".into()),
    }
}

fn bits(game: &Path) -> Result<Bits, String> {
    use std::io::Read;
    let mut header = vec![0u8; 4096];
    let mut file = std::fs::File::open(game).map_err(|e| e.to_string())?;
    let read = file.read(&mut header).map_err(|e| e.to_string())?;
    header.truncate(read);
    bits_of(&header)
}

fn is_ours(dll: &Path) -> bool {
    std::fs::read(dll).is_ok_and(|bytes| bytes.windows(MARK.len()).any(|w| w == MARK))
}

fn beside(game: &Path, bits: Bits) -> Option<PathBuf> {
    game.parent().map(|dir| dir.join(bits.dll_name()))
}

/// Whether `game` has the stand-in beside it.
pub fn given(game: &Path) -> bool {
    [Bits::X64, Bits::X86].into_iter().filter_map(|b| beside(game, b)).any(|dll| is_ours(&dll))
}

/// Put the stand-in beside `game`, as the DLL it loads.
pub fn give(game: &Path) -> Result<(), String> {
    let bits = bits(game)?;
    let bytes = bits.dll().ok_or("this build of Studio has no Chroma DLL to give")?;
    let dll = beside(game, bits).ok_or("the game is in no folder")?;
    if dll.exists() && !is_ours(&dll) {
        // A game that ships Razer's own: it would be gone for good.
        return Err(format!("{} beside it is Razer's own; it is left alone", bits.dll_name()));
    }
    std::fs::write(&dll, bytes).map_err(|e| e.to_string())
}

/// Take the stand-in away from beside `game`, if Studio put it there.
pub fn take_back(game: &Path) -> Result<(), String> {
    for bits in [Bits::X64, Bits::X86] {
        if let Some(dll) = beside(game, bits).filter(|dll| is_ours(dll)) {
            std::fs::remove_file(&dll).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn program(machine: u16) -> Vec<u8> {
        let mut header = vec![0u8; 512];
        header[..2].copy_from_slice(b"MZ");
        header[0x3C..0x40].copy_from_slice(&0x80u32.to_le_bytes());
        header[0x80..0x84].copy_from_slice(b"PE\0\0");
        header[0x84..0x86].copy_from_slice(&machine.to_le_bytes());
        header
    }

    #[test]
    fn a_program_says_whether_it_is_64_or_32_bit() {
        assert_eq!(bits_of(&program(0x8664)), Ok(Bits::X64));
        assert_eq!(bits_of(&program(0x014C)), Ok(Bits::X86));
        assert!(bits_of(&program(0xAA64)).is_err());
        assert!(bits_of(b"not a program").is_err());
        assert!(bits_of(&program(0x8664)[..0x60]).is_err());
    }

    #[test]
    fn a_game_is_given_the_dll_for_its_bits_and_it_is_taken_back() {
        if !available() {
            return; // a build without the DLL has nothing to give
        }
        let dir = std::env::temp_dir().join(format!("openrazer-chroma-give-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let game = dir.join("old-game.exe");
        std::fs::write(&game, program(0x014C)).unwrap();
        give(&game).unwrap();
        assert!(is_ours(&dir.join("RzChromaSDK.dll")) && !dir.join("RzChromaSDK64.dll").exists());
        assert!(given(&game));
        give(&game).unwrap(); // again, over its own copy
        take_back(&game).unwrap();
        assert!(!dir.join("RzChromaSDK.dll").exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_studio_s_own_copy_is_taken_back() {
        let dir = std::env::temp_dir().join(format!("openrazer-chroma-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let game = dir.join("game.exe");
        std::fs::write(&game, program(0x8664)).unwrap();
        let razer = dir.join("RzChromaSDK64.dll");
        std::fs::write(&razer, b"Razer's own").unwrap();
        assert!(!given(&game));
        if available() {
            assert!(give(&game).is_err(), "Razer's DLL must not be replaced");
        }
        take_back(&game).unwrap();
        assert!(razer.exists(), "Razer's DLL must not be removed");
        std::fs::write(&razer, b"...openrazer-chroma...").unwrap();
        assert!(given(&game));
        take_back(&game).unwrap();
        assert!(!razer.exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
