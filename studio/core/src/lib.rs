//! What OpenRazer Studio and its installer have in common.
//!
//! The installer has to stop the daemon and the app before it can replace
//! them, knows where they live and how they start with Windows, and looks like
//! the app it installs; so all of that lives here rather than twice.

pub mod i18n;
pub mod install;
pub mod paths;
pub mod process;
pub mod rpc;
pub mod theme;
pub mod widgets;

pub use i18n::{t, tf};
