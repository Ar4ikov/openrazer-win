//! Other processes: finding them, asking Studio to quit, and the app in front.

use std::path::{Path, PathBuf};
use std::time::Duration;

/// Studio waits on this; the installer sets it to have Studio quit cleanly
/// before its files are replaced or removed.
pub const QUIT_EVENT: &str = "Local\\openrazer-studio-quit";

#[derive(Clone, Debug)]
pub struct Process {
    pub pid: u32,
    /// The executable's file name, as Windows lists it.
    pub name: String,
    /// The full path, where this user is allowed to read it.
    pub path: Option<PathBuf>,
}

#[cfg(windows)]
mod win {
    use super::Process;
    use std::path::PathBuf;
    use windows_sys::Win32::Foundation::{CloseHandle, BOOL, HWND, INVALID_HANDLE_VALUE, LPARAM, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::Threading::{
        CreateEventW, OpenEventW, OpenProcess, QueryFullProcessImageNameW, SetEvent, TerminateProcess,
        WaitForSingleObject, EVENT_MODIFY_STATE, INFINITE, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
        PROCESS_TERMINATE,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetForegroundWindow, GetWindow, GetWindowThreadProcessId, IsWindowVisible, SendMessageTimeoutW,
        GW_OWNER, HWND_BROADCAST, SMTO_ABORTIFHUNG, WM_SETTINGCHANGE,
    };

    const SYNCHRONIZE: u32 = 0x0010_0000;

    pub fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn from_wide(buffer: &[u16]) -> String {
        let end = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
        String::from_utf16_lossy(&buffer[..end])
    }

    pub fn image_path(pid: u32) -> Option<PathBuf> {
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return None;
            }
            let mut buffer = [0u16; 1024];
            let mut size = buffer.len() as u32;
            let ok = QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, buffer.as_mut_ptr(), &mut size);
            CloseHandle(handle);
            (ok != 0).then(|| PathBuf::from(String::from_utf16_lossy(&buffer[..size as usize])))
        }
    }

    pub fn list(with_paths: bool) -> Vec<Process> {
        let mut found = Vec::new();
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if snapshot == INVALID_HANDLE_VALUE {
                return found;
            }
            let mut entry: PROCESSENTRY32W = std::mem::zeroed();
            entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
            let mut more = Process32FirstW(snapshot, &mut entry) != 0;
            while more {
                let pid = entry.th32ProcessID;
                found.push(Process { pid, name: from_wide(&entry.szExeFile), path: if with_paths { image_path(pid) } else { None } });
                more = Process32NextW(snapshot, &mut entry) != 0;
            }
            CloseHandle(snapshot);
        }
        found
    }

    pub fn terminate(pid: u32) -> bool {
        unsafe {
            let handle = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, 0, pid);
            if handle.is_null() {
                return false;
            }
            let ok = TerminateProcess(handle, 1) != 0;
            if ok {
                WaitForSingleObject(handle, 5000);
            }
            CloseHandle(handle);
            ok
        }
    }

    /// Wait for `pid` to exit; true if it did within `ms`.
    pub fn wait_exit(pid: u32, ms: u32) -> bool {
        unsafe {
            let handle = OpenProcess(SYNCHRONIZE, 0, pid);
            if handle.is_null() {
                return true; // already gone, or never there
            }
            let done = WaitForSingleObject(handle, ms) == WAIT_OBJECT_0;
            CloseHandle(handle);
            done
        }
    }

    pub fn signal_quit() -> bool {
        let name = wide(super::QUIT_EVENT);
        unsafe {
            let handle = OpenEventW(EVENT_MODIFY_STATE, 0, name.as_ptr());
            if handle.is_null() {
                return false;
            }
            let ok = SetEvent(handle) != 0;
            CloseHandle(handle);
            ok
        }
    }

    pub fn listen_for_quit(on_quit: impl FnOnce() + Send + 'static) {
        let name = wide(super::QUIT_EVENT);
        let handle = unsafe { CreateEventW(std::ptr::null(), 0, 0, name.as_ptr()) };
        if handle.is_null() {
            return;
        }
        let handle = handle as usize;
        std::thread::Builder::new()
            .name("quit-event".into())
            .spawn(move || unsafe {
                if WaitForSingleObject(handle as _, INFINITE) == WAIT_OBJECT_0 {
                    on_quit();
                }
            })
            .ok();
    }

    pub fn announce_environment_change() {
        let environment = wide("Environment");
        unsafe {
            let mut result = 0usize;
            SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, environment.as_ptr() as LPARAM, SMTO_ABORTIFHUNG, 2000, &mut result);
        }
    }

    fn window_pid(hwnd: HWND) -> u32 {
        let mut pid = 0u32;
        unsafe { GetWindowThreadProcessId(hwnd, &mut pid) };
        pid
    }

    pub fn foreground_pid() -> Option<u32> {
        let hwnd = unsafe { GetForegroundWindow() };
        if hwnd.is_null() {
            return None;
        }
        Some(window_pid(hwnd)).filter(|pid| *pid != 0)
    }

    unsafe extern "system" fn collect(hwnd: HWND, data: LPARAM) -> BOOL {
        let pids = &mut *(data as *mut Vec<u32>);
        if IsWindowVisible(hwnd) != 0 && GetWindow(hwnd, GW_OWNER).is_null() {
            let pid = window_pid(hwnd);
            if pid != 0 && !pids.contains(&pid) {
                pids.push(pid);
            }
        }
        1
    }

    /// Processes that have a visible top-level window: the apps on screen.
    pub fn windowed_pids() -> Vec<u32> {
        let mut pids: Vec<u32> = Vec::new();
        unsafe { EnumWindows(Some(collect), &mut pids as *mut Vec<u32> as LPARAM) };
        pids
    }
}

#[cfg(not(windows))]
mod win {
    use super::Process;
    use std::path::PathBuf;
    pub fn image_path(_pid: u32) -> Option<PathBuf> {
        None
    }
    pub fn list(_with_paths: bool) -> Vec<Process> {
        Vec::new()
    }
    pub fn terminate(_pid: u32) -> bool {
        false
    }
    pub fn wait_exit(_pid: u32, _ms: u32) -> bool {
        true
    }
    pub fn signal_quit() -> bool {
        false
    }
    pub fn listen_for_quit(_on_quit: impl FnOnce() + Send + 'static) {}
    pub fn announce_environment_change() {}
    pub fn foreground_pid() -> Option<u32> {
        None
    }
    pub fn windowed_pids() -> Vec<u32> {
        Vec::new()
    }
}

pub use win::{announce_environment_change, image_path, list, listen_for_quit, signal_quit, terminate, wait_exit};

/// Every process whose executable is inside `dir`.
pub fn running_from(dir: &Path) -> Vec<Process> {
    list(true)
        .into_iter()
        .filter(|p| p.path.as_ref().and_then(|path| path.parent()).is_some_and(|parent| crate::install::same_path(parent, dir)))
        .collect()
}

/// End every process running from `dir`: ask Studio and the daemon nicely
/// first, then insist.  Returns the names of any that would not go.
pub fn stop_everything_in(dir: &Path) -> Vec<String> {
    let mine = running_from(dir);
    if mine.is_empty() {
        return Vec::new();
    }
    signal_quit();
    let daemon_here = mine.iter().any(|p| p.name.eq_ignore_ascii_case(crate::install::DAEMON_EXE));
    if daemon_here {
        crate::rpc::shutdown_daemon(Duration::from_secs(10));
    }
    for process in &mine {
        wait_exit(process.pid, 4000);
    }
    let mut stuck = Vec::new();
    for process in running_from(dir) {
        if !terminate(process.pid) {
            stuck.push(process.name);
        }
    }
    stuck
}

/// The executable file name of the app in front, lower-cased: `game.exe`.
pub fn foreground_exe() -> Option<String> {
    let pid = win::foreground_pid()?;
    let path = image_path(pid)?;
    Some(path.file_name()?.to_string_lossy().to_lowercase())
}

/// The executables of the apps with a window on screen, lower-cased and sorted.
pub fn windowed_apps() -> Vec<String> {
    let mut names: Vec<String> = win::windowed_pids()
        .into_iter()
        .filter_map(image_path)
        .filter_map(|path| path.file_name().map(|n| n.to_string_lossy().to_lowercase()))
        .collect();
    names.sort();
    names.dedup();
    names
}
