//! Zeus OS preview command line entry point.
//!
//! This binary intentionally has a small standard-library-only surface.  It
//! is used by the desktop and recovery image, so commands are explicit,
//! subprocess arguments are passed as arrays, and the preview does not claim
//! capabilities which have not landed yet.

use std::env;
use std::fmt;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const VERSION: &str = env!("CARGO_PKG_VERSION");
// Zeus uses the Fedora-supported Dash to Dock extension for its preview dock.
// Keeping the upstream UUID here lets the safe fallback work with the actual
// installed extension and avoids inventing a second shell module to disable.
const DOCK_EXTENSION: &str = "dash-to-dock@micxgx.gmail.com";
const MAX_OUTPUT_BYTES: u64 = 128 * 1024;
const COMMAND_TIMEOUT: Duration = Duration::from_secs(15);
const DEV_TIMEOUT: Duration = Duration::from_secs(60 * 60);

const HELP: &str = "\
zeus — the local Zeus OS preview helper

USAGE:
    zeus version
    zeus doctor [--json]
    zeus desktop safe [--dry-run]
    zeus desktop restore [--dry-run]
    zeus update status [--json]
    zeus dev [--target TARGET] [--dry-run]

The preview also reserves these commands for later releases:
    zeus restore              profile restore is not available yet
    zeus config export        profile export is not available yet

The desktop actions keep credentials and personal data in place.  `dev`
opens one validated SSH destination and never accepts a remote command.
";

#[derive(Debug)]
struct CliError {
    code: u8,
    message: String,
}

impl CliError {
    fn usage(message: impl Into<String>) -> Self {
        Self {
            code: 2,
            message: message.into(),
        }
    }

    fn operation(message: impl Into<String>) -> Self {
        Self {
            code: 1,
            message: message.into(),
        }
    }
}

impl fmt::Display for CliError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.message.fmt(formatter)
    }
}

#[derive(Debug)]
struct CommandOutput {
    status: ExitStatus,
    stdout: String,
    stderr: String,
}

#[derive(Debug)]
struct TerminalInfo {
    name: String,
    path: PathBuf,
}

#[derive(Debug)]
struct DesktopTool {
    path: PathBuf,
    helper: bool,
}

#[derive(Debug)]
struct BootcProbe {
    available: bool,
    status: String,
    exit_code: Option<i32>,
    summary: String,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("zeus: {}", error);
            ExitCode::from(error.code)
        }
    }
}

fn run() -> Result<(), CliError> {
    let arguments: Vec<String> = env::args().skip(1).collect();
    let Some(command) = arguments.first() else {
        print_help();
        return Err(CliError::usage("a command is required; try `zeus --help`"));
    };

    if command == "--help" || command == "-h" {
        print_help();
        return Ok(());
    }
    if command == "--version" || command == "-V" {
        println!("zeus {}", VERSION);
        return Ok(());
    }

    let rest = &arguments[1..];
    match command.as_str() {
        "version" => command_version(rest),
        "doctor" => command_doctor(rest),
        "desktop" => command_desktop(rest),
        "update" => command_update(rest),
        "dev" => command_dev(rest),
        "restore" => Err(unsupported(
            "restore",
            "profile restore is reserved for a later preview",
        )),
        "config" => command_config(rest),
        "help" => {
            if rest.is_empty() || (rest.len() == 1 && (rest[0] == "-h" || rest[0] == "--help")) {
                print_help();
                Ok(())
            } else {
                Err(CliError::usage("`help` takes no arguments"))
            }
        }
        _ => Err(CliError::usage(format!(
            "unknown command `{}`; try `zeus --help`",
            command
        ))),
    }
}

fn print_help() {
    print!("{}", HELP);
}

fn command_version(arguments: &[String]) -> Result<(), CliError> {
    if arguments.is_empty() {
        println!("Zeus OS {}", VERSION);
        if let Ok(build) = std::fs::read_to_string("/usr/share/zeus/build-id") {
            println!("Build {}", build.trim());
        }
        return Ok(());
    }
    if arguments.len() == 1 && (arguments[0] == "-h" || arguments[0] == "--help") {
        println!("zeus version — print the installed Zeus OS preview version");
        return Ok(());
    }
    Err(CliError::usage("`version` takes no arguments"))
}

fn command_doctor(arguments: &[String]) -> Result<(), CliError> {
    let mut json = false;
    for argument in arguments {
        match argument.as_str() {
            "--json" => json = true,
            "-h" | "--help" => {
                println!("zeus doctor [--json] — inspect the local session and bootc status");
                return Ok(());
            }
            _ => return Err(CliError::usage("`doctor` accepts only `--json`")),
        }
    }

    let session = session_info();
    let terminal = find_terminal();
    let bootc = probe_bootc();

    if json {
        let terminal_name = terminal
            .as_ref()
            .map(|item| item.name.as_str())
            .unwrap_or("none");
        let terminal_available = terminal.is_some();
        let exit_code = bootc
            .exit_code
            .map(|value| value.to_string())
            .unwrap_or_else(|| "null".to_string());
        println!(
            "{{\"version\":{},\"session\":{{\"desktop\":{},\"type\":{},\"ready\":{}}},\"terminal\":{{\"command\":{},\"available\":{}}},\"bootc\":{{\"available\":{},\"status\":{},\"exit_code\":{},\"summary\":{}}},\"safe_desktop\":{{\"credentials_untouched\":true,\"personal_data_untouched\":true}}}}",
            json_string(VERSION),
            json_string(&session.desktop),
            json_string(&session.session_type),
            json_bool(session.ready),
            json_string(terminal_name),
            json_bool(terminal_available),
            json_bool(bootc.available),
            json_string(&bootc.status),
            exit_code,
            json_string(&bootc.summary),
        );
        return Ok(());
    }

    println!("Zeus doctor {}", VERSION);
    println!(
        "Session: {} on {} ({})",
        session.desktop,
        session.session_type,
        if session.ready {
            "ready"
        } else {
            "not detected"
        }
    );
    match terminal {
        Some(info) => println!("Terminal: {} ({})", info.name, info.path.display()),
        None => println!("Terminal: no supported terminal found (ptyxis, gnome-terminal, kgx)"),
    }
    match bootc.status.as_str() {
        "ready" => {
            println!("Bootc: available (status exited successfully)");
            if !bootc.summary.is_empty() {
                println!("Bootc summary: {}", bootc.summary);
            }
        }
        "unavailable" => println!("Bootc: unavailable (the bootc command was not found)"),
        _ => {
            println!("Bootc: status command failed; inspect the local installation before updating")
        }
    }
    println!("Safe desktop actions leave credentials and personal data untouched.");
    Ok(())
}

fn command_desktop(arguments: &[String]) -> Result<(), CliError> {
    let Some(action) = arguments.first() else {
        return Err(CliError::usage("desktop requires `safe` or `restore`"));
    };

    let mut dry_run = false;
    for argument in &arguments[1..] {
        match argument.as_str() {
            "--dry-run" => dry_run = true,
            "-h" | "--help" => {
                println!("zeus desktop safe|restore [--dry-run]");
                return Ok(());
            }
            _ => {
                return Err(CliError::usage(
                    "desktop accepts only `--dry-run` after its action",
                ))
            }
        }
    }

    match action.as_str() {
        "safe" => desktop_safe(dry_run),
        "restore" => desktop_restore(dry_run),
        _ => Err(CliError::usage("desktop requires `safe` or `restore`")),
    }
}

fn desktop_safe(dry_run: bool) -> Result<(), CliError> {
    let terminal = find_terminal().ok_or_else(|| {
        CliError::operation(
            "desktop safe needs a usable terminal (expected ptyxis, gnome-terminal, or kgx); no desktop changes were made",
        )
    })?;
    let desktop_tool = resolve_desktop_tool().ok_or_else(|| {
        CliError::operation(
            "desktop safe needs the Zeus desktop helper or gnome-extensions; the dock was left unchanged",
        )
    })?;

    if dry_run {
        println!("Desktop safe (dry run)");
        if desktop_tool.helper {
            println!("Would run: zeus-desktop-safe disable");
        } else {
            println!("Would run: gnome-extensions disable {}", DOCK_EXTENSION);
        }
        println!("Would return the shell to stock GNOME behavior by disabling the dock extension");
        println!("Terminal remains available through {}", terminal.name);
        println!("Credentials and personal data would remain untouched");
        return Ok(());
    }

    let arguments: &[&str] = if desktop_tool.helper {
        &["disable"]
    } else {
        &["disable", DOCK_EXTENSION]
    };
    let result = run_capture(&desktop_tool.path, arguments, COMMAND_TIMEOUT)?;
    if !result.status.success() {
        return Err(CliError::operation(format!(
            "the desktop fallback could not disable the Zeus dock (exit {}); no stock reset was attempted",
            exit_code_text(result.status)
        )));
    }

    println!("Safe desktop enabled; native GNOME remains available. Reopen applications for window styling changes.");
    println!("Owner wallpaper and preferences are preserved; only managed Zeus styling changes.");
    println!("Terminal remains available through {}.", terminal.name);
    println!("Credentials and personal data were left untouched.");
    Ok(())
}

fn desktop_restore(dry_run: bool) -> Result<(), CliError> {
    let desktop_tool = resolve_desktop_tool().ok_or_else(|| {
        CliError::operation(
            "desktop restore needs the Zeus desktop helper or gnome-extensions; the dock was left unchanged",
        )
    })?;

    if dry_run {
        println!("Desktop restore (dry run)");
        if desktop_tool.helper {
            println!("Would run: zeus-desktop-safe enable");
        } else {
            println!("Would run: gnome-extensions enable {}", DOCK_EXTENSION);
        }
        println!("Credentials and personal data would remain untouched");
        return Ok(());
    }

    let arguments: &[&str] = if desktop_tool.helper {
        &["enable"]
    } else {
        &["enable", DOCK_EXTENSION]
    };
    let result = run_capture(&desktop_tool.path, arguments, COMMAND_TIMEOUT)?;
    if !result.status.success() {
        return Err(CliError::operation(format!(
            "gnome-extensions could not enable the Zeus dock (exit {})",
            exit_code_text(result.status)
        )));
    }
    println!("Zeus desktop restored. Reopen applications to apply window styling.");
    println!("Credentials and personal data were left untouched.");
    Ok(())
}

fn resolve_desktop_tool() -> Option<DesktopTool> {
    if let Some(path) = env::var_os("ZEUS_DESKTOP_HELPER") {
        let path = PathBuf::from(path);
        if is_executable(&path) {
            return Some(DesktopTool { path, helper: true });
        }
    }

    let installed_helper = Path::new("/usr/libexec/zeus-desktop-safe");
    if is_executable(installed_helper) {
        return Some(DesktopTool {
            path: installed_helper.to_path_buf(),
            helper: true,
        });
    }
    if let Some(path) = find_executable("zeus-desktop-safe") {
        return Some(DesktopTool { path, helper: true });
    }
    resolve_program("ZEUS_GNOME_EXTENSIONS_BIN", "gnome-extensions").map(|path| DesktopTool {
        path,
        helper: false,
    })
}

fn command_update(arguments: &[String]) -> Result<(), CliError> {
    let Some(action) = arguments.first() else {
        return Err(CliError::usage(
            "`update` currently supports read-only `status`; staging and reboot remain manual",
        ));
    };

    let mut json = false;
    for argument in &arguments[1..] {
        match argument.as_str() {
            "--json" => json = true,
            "-h" | "--help" => {
                println!("zeus update status [--json] — read bootc status without rebooting");
                return Ok(());
            }
            _ => return Err(CliError::usage("update status accepts only `--json`")),
        }
    }
    if action != "status" {
        return Err(unsupported(
            &format!("update {}", action),
            "only read-only bootc status is available in this preview",
        ));
    }

    let Some(bootc) = resolve_program("ZEUS_BOOTC_BIN", "bootc") else {
        return Err(CliError::operation(
            "bootc is unavailable; update status could not be read and no reboot was requested",
        ));
    };
    let args: &[&str] = if json {
        &["status", "--json"]
    } else {
        &["status"]
    };
    let result = run_capture(&bootc, args, COMMAND_TIMEOUT)?;
    let output = if result.stdout.trim().is_empty() {
        result.stderr.as_str()
    } else {
        result.stdout.as_str()
    };
    let summary = sanitize_output(output);

    if json {
        println!(
            "{{\"version\":{},\"status\":{},\"exit_code\":{},\"summary\":{},\"reboot_requested\":false}}",
            json_string(VERSION),
            json_string(if result.status.success() { "ready" } else { "error" }),
            result
                .status
                .code()
                .map(|value| value.to_string())
                .unwrap_or_else(|| "null".to_string()),
            json_string(&summary),
        );
    } else if summary.is_empty() {
        println!("bootc status completed; no deployment details were returned.");
    } else {
        print!("{}", summary);
        if !summary.ends_with('\n') {
            println!();
        }
    }
    if !json {
        println!("No reboot was requested; applying an update remains an explicit user action.");
    }

    if result.status.success() {
        Ok(())
    } else {
        Err(CliError::operation(format!(
            "bootc status failed (exit {}); no reboot was requested",
            exit_code_text(result.status)
        )))
    }
}

fn command_dev(arguments: &[String]) -> Result<(), CliError> {
    let mut target: Option<String> = None;
    let mut dry_run = false;
    let mut positional_target = false;
    let mut index = 0;
    while index < arguments.len() {
        match arguments[index].as_str() {
            "--target" => {
                if target.is_some() || index + 1 >= arguments.len() {
                    return Err(CliError::usage("dev needs one value after `--target`"));
                }
                target = Some(arguments[index + 1].clone());
                index += 2;
            }
            "--dry-run" => {
                dry_run = true;
                index += 1;
            }
            "-h" | "--help" => {
                println!("zeus dev [--target TARGET] [--dry-run] — open one configured SSH target");
                return Ok(());
            }
            argument if argument.starts_with('-') => {
                return Err(CliError::usage(
                    "dev accepts only `--target` and `--dry-run`",
                ));
            }
            value => {
                if positional_target {
                    return Err(CliError::usage(
                        "dev accepts one SSH target and no remote command",
                    ));
                }
                target = Some(value.to_string());
                positional_target = true;
                index += 1;
            }
        }
    }

    let target = target
        .or_else(configured_ssh_target)
        .ok_or_else(|| {
            CliError::operation(
                "no configured SSH target; set ZEUS_SSH_TARGET or add [dev] target to ~/.config/zeus/config.toml",
            )
        })?;
    validate_ssh_target(&target)?;

    if dry_run {
        println!("ssh -- {}", target);
        return Ok(());
    }

    let ssh = resolve_program("ZEUS_SSH_BIN", "ssh").ok_or_else(|| {
        CliError::operation(
            "ssh is unavailable; the configured development target was not contacted",
        )
    })?;
    let child = Command::new(&ssh)
        .arg("--")
        .arg(&target)
        .spawn()
        .map_err(|error| CliError::operation(format!("could not start ssh: {}", error)))?;
    let status = wait_for_child(child, DEV_TIMEOUT)?;
    if status.success() {
        Ok(())
    } else {
        Err(CliError::operation(format!(
            "ssh exited with {}; the remote development target was not changed",
            exit_code_text(status)
        )))
    }
}

fn command_config(arguments: &[String]) -> Result<(), CliError> {
    let Some(action) = arguments.first() else {
        return Err(CliError::usage("config requires `export`"));
    };
    if action == "export" {
        return Err(unsupported(
            "config export",
            "profile export is reserved for a later preview; no settings were written",
        ));
    }
    Err(CliError::usage("config requires `export`"))
}

fn unsupported(command: &str, reason: &str) -> CliError {
    CliError::operation(format!(
        "`{}` is unavailable in this preview: {}",
        command, reason
    ))
}

fn session_info() -> SessionInfo {
    let desktop_raw = env::var("XDG_CURRENT_DESKTOP").unwrap_or_default();
    let desktop = if desktop_raw.trim().is_empty() {
        if env::var_os("GNOME_SHELL_SESSION_MODE").is_some()
            || env::var_os("GNOME_DESKTOP_SESSION_ID").is_some()
        {
            "GNOME".to_string()
        } else {
            "unknown".to_string()
        }
    } else {
        safe_component(&desktop_raw, "unknown")
    };

    let session_type = env::var("XDG_SESSION_TYPE")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(|value| safe_component(&value, "unknown"))
        .unwrap_or_else(|| {
            if env::var_os("WAYLAND_DISPLAY").is_some() {
                "wayland".to_string()
            } else if env::var_os("DISPLAY").is_some() {
                "x11".to_string()
            } else {
                "unknown".to_string()
            }
        });

    SessionInfo {
        ready: desktop != "unknown" && session_type != "unknown",
        desktop,
        session_type,
    }
}

#[derive(Debug)]
struct SessionInfo {
    desktop: String,
    session_type: String,
    ready: bool,
}

fn probe_bootc() -> BootcProbe {
    let Some(bootc) = resolve_program("ZEUS_BOOTC_BIN", "bootc") else {
        return BootcProbe {
            available: false,
            status: "unavailable".to_string(),
            exit_code: None,
            summary: String::new(),
        };
    };

    match run_capture(&bootc, &["status", "--json"], COMMAND_TIMEOUT) {
        Ok(result) => {
            let output = if result.stdout.trim().is_empty() {
                result.stderr.as_str()
            } else {
                result.stdout.as_str()
            };
            BootcProbe {
                available: true,
                status: if result.status.success() {
                    "ready".to_string()
                } else {
                    "error".to_string()
                },
                exit_code: result.status.code(),
                summary: summarize_output(output),
            }
        }
        Err(error) => BootcProbe {
            available: true,
            status: "error".to_string(),
            exit_code: None,
            summary: sanitize_output(&error.message),
        },
    }
}

fn summarize_output(value: &str) -> String {
    let sanitized = sanitize_output(value);
    sanitized
        .lines()
        .filter(|line| !line.trim().is_empty())
        .take(3)
        .collect::<Vec<_>>()
        .join(" | ")
}

fn sanitize_output(value: &str) -> String {
    value
        .lines()
        .take(64)
        .map(sanitize_line)
        .filter(|line| !line.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn sanitize_line(line: &str) -> String {
    const SENSITIVE_MARKERS: &[&str] = &[
        "password",
        "passwd",
        "token",
        "secret",
        "private_key",
        "private-key",
        "credential",
        "authorization",
        "bearer",
        "cookie",
        "pairing",
        "prompt",
    ];

    let lower = line.to_ascii_lowercase();
    for marker in SENSITIVE_MARKERS {
        if let Some(position) = lower.find(marker) {
            let prefix = line[..position].trim_end();
            return if prefix.is_empty() {
                "[redacted sensitive status line]".to_string()
            } else {
                format!("{} [redacted]", prefix)
            };
        }
    }
    line.trim().to_string()
}

fn find_terminal() -> Option<TerminalInfo> {
    if let Some(path) = env::var_os("ZEUS_TERMINAL_BIN") {
        let path = PathBuf::from(path);
        if is_executable(&path) {
            return Some(TerminalInfo {
                name: path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .unwrap_or("terminal")
                    .to_string(),
                path,
            });
        }
    }

    for name in ["ptyxis", "gnome-terminal", "kgx"] {
        if let Some(path) = find_executable(name) {
            return Some(TerminalInfo {
                name: name.to_string(),
                path,
            });
        }
    }
    None
}

fn configured_ssh_target() -> Option<String> {
    for key in ["ZEUS_SSH_TARGET", "ZEUS_DEV_TARGET"] {
        if let Some(value) = env::var_os(key).and_then(|value| value.into_string().ok()) {
            if !value.trim().is_empty() {
                return Some(value);
            }
        }
    }

    let config_path = env::var_os("ZEUS_CONFIG")
        .map(PathBuf::from)
        .or_else(default_config_path)?;
    let contents = fs::read_to_string(config_path).ok()?;
    parse_config_target(&contents)
}

fn default_config_path() -> Option<PathBuf> {
    let config_root = env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
    Some(config_root.join("zeus").join("config.toml"))
}

/// Parse the intentionally small target subset of the config file.  This is
/// not a general TOML parser; accepting only the documented scalar keys keeps
/// the command surface predictable and avoids turning config text into shell.
fn parse_config_target(contents: &str) -> Option<String> {
    let mut section = String::new();
    for line in contents.lines().take(256) {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            section = line[1..line.len() - 1].trim().to_ascii_lowercase();
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim().to_ascii_lowercase();
        let value = value.trim().trim_end_matches(',').trim();
        let wanted = (section == "dev" && key == "target")
            || (section.is_empty() && (key == "dev_target" || key == "ssh_target"));
        if !wanted {
            continue;
        }
        let value = value
            .strip_prefix('"')
            .and_then(|value| value.strip_suffix('"'))
            .or_else(|| {
                value
                    .strip_prefix('\'')
                    .and_then(|value| value.strip_suffix('\''))
            })
            .unwrap_or(value)
            .trim();
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }
    None
}

fn validate_ssh_target(target: &str) -> Result<(), CliError> {
    if target.is_empty() || target.len() > 255 || target.starts_with('-') {
        return Err(CliError::usage(
            "invalid SSH target; use a configured host such as dev.example or user@dev.example",
        ));
    }
    if target
        .chars()
        .any(|character| !is_safe_ssh_character(character))
    {
        return Err(CliError::usage(
            "invalid SSH target; shell syntax, whitespace, paths, and remote commands are not accepted",
        ));
    }
    if target.chars().filter(|character| *character == '@').count() > 1 {
        return Err(CliError::usage(
            "invalid SSH target; only one user@host separator is allowed",
        ));
    }

    let host = target
        .rsplit_once('@')
        .map(|(_, host)| host)
        .unwrap_or(target);
    if host.is_empty() || host == ":" || host.starts_with(':') {
        return Err(CliError::usage("invalid SSH target; a host is required"));
    }
    if host.contains("::") && !(host.starts_with('[') && host.ends_with(']')) {
        return Err(CliError::usage(
            "invalid SSH target; use a bracketed IPv6 host",
        ));
    }
    Ok(())
}

fn is_safe_ssh_character(character: char) -> bool {
    character.is_ascii_alphanumeric()
        || matches!(character, '.' | '_' | '-' | '@' | ':' | '[' | ']')
}

fn json_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn json_string(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len() + 2);
    escaped.push('"');
    for character in value.chars() {
        match character {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            character if character.is_control() => {
                escaped.push_str(&format!("\\u{:04x}", character as u32));
            }
            character => escaped.push(character),
        }
    }
    escaped.push('"');
    escaped
}

fn safe_component(value: &str, fallback: &str) -> String {
    let value = value
        .split(|character: char| character == ':' || character == ';' || character.is_whitespace())
        .next()
        .unwrap_or_default();
    if value.is_empty()
        || value.len() > 64
        || value.chars().any(|character| !is_safe_component(character))
    {
        fallback.to_string()
    } else {
        value.to_string()
    }
}

fn is_safe_component(character: char) -> bool {
    character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
}

fn resolve_program(variable: &str, name: &str) -> Option<PathBuf> {
    if let Some(value) = env::var_os(variable) {
        let path = PathBuf::from(value);
        if path.components().count() > 1 || path.is_absolute() {
            return is_executable(&path).then_some(path);
        }
        return find_executable(path.to_str().unwrap_or(name));
    }
    find_executable(name)
}

fn find_executable(name: &str) -> Option<PathBuf> {
    let path = Path::new(name);
    if path.components().count() > 1 || path.is_absolute() {
        return is_executable(path).then(|| path.to_path_buf());
    }
    let path_env = env::var_os("PATH")?;
    for directory in env::split_paths(&path_env) {
        let candidate = directory.join(name);
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

fn is_executable(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        return fs::metadata(path)
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn run_capture(
    program: &Path,
    arguments: &[&str],
    timeout: Duration,
) -> Result<CommandOutput, CliError> {
    let mut child = Command::new(program)
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| {
            CliError::operation(format!("could not start {}: {}", program.display(), error))
        })?;

    let stdout = child.stdout.take().expect("stdout was piped");
    let stderr = child.stderr.take().expect("stderr was piped");
    let stdout_reader = thread::spawn(move || read_capped(stdout));
    let stderr_reader = thread::spawn(move || read_capped(stderr));
    let status = wait_for_child(child, timeout)?;
    let stdout = stdout_reader
        .join()
        .map_err(|_| CliError::operation("could not read command output"))?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| CliError::operation("could not read command error output"))?;

    Ok(CommandOutput {
        status,
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
    })
}

fn read_capped<R: Read>(reader: R) -> Vec<u8> {
    let mut output = Vec::new();
    let _ = reader.take(MAX_OUTPUT_BYTES).read_to_end(&mut output);
    output
}

fn wait_for_child(mut child: Child, timeout: Duration) -> Result<ExitStatus, CliError> {
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) if started.elapsed() >= timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CliError::operation(format!(
                    "command exceeded the {} second timeout",
                    timeout.as_secs()
                )));
            }
            Ok(None) => thread::sleep(Duration::from_millis(25)),
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CliError::operation(format!(
                    "could not inspect command status: {}",
                    error
                )));
            }
        }
    }
}

fn exit_code_text(status: ExitStatus) -> String {
    status
        .code()
        .map(|code| code.to_string())
        .unwrap_or_else(|| "signal".to_string())
}
