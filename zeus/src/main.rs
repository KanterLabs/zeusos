//! Zeus OS preview command line entry point.
//!
//! This binary intentionally has a small standard-library-only surface.  It
//! is used by the desktop and recovery image, so commands are explicit and
//! subprocess arguments are passed as arrays.

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
const UPDATE_HELPER_PATH: &str = "/usr/libexec/zeus-update";
const DEVELOPER_HELPER_PATH: &str = "/usr/libexec/zeus-developer";
const MAX_OUTPUT_BYTES: u64 = 128 * 1024;
const COMMAND_TIMEOUT: Duration = Duration::from_secs(15);
const UPDATE_STATUS_TIMEOUT: Duration = Duration::from_secs(15);
const UPDATE_CHECK_TIMEOUT: Duration = Duration::from_secs(90);
const UPDATE_INSTALL_TIMEOUT: Duration = Duration::from_secs(300);
const DEVELOPER_STATUS_TIMEOUT: Duration = Duration::from_secs(15);
const DEVELOPER_ACTION_TIMEOUT: Duration = Duration::from_secs(300);
const DEV_TIMEOUT: Duration = Duration::from_secs(60 * 60);

const HELP: &str = "\
zeus — the local Zeus OS preview helper

USAGE:
    zeus version
    zeus doctor [--json]
    zeus desktop safe [--dry-run]
    zeus desktop restore [--dry-run]
    zeus update status [--json]
    zeus update check [--json]
    zeus update install [--json]
    zeus dev [--target TARGET] [--dry-run]
    zeus developer status [--json]
    zeus developer enable|apply [ARTIFACT_DIGEST]|undo|disable

The preview also reserves these commands for later releases:
    zeus restore              profile restore is not available yet
    zeus config export        profile export is not available yet

The desktop actions keep credentials and personal data in place.  `dev`
opens one validated SSH destination and never accepts a remote command.
Update checks and installs use the signed system updater; installing starts a
background job and never reboots automatically.
Developer Mode actions use the fixed local helper and require administrator
authentication where the helper requests it; they never invoke a shell.
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

#[derive(Debug)]
struct DeveloperProbe {
    available: bool,
    active: bool,
    state: String,
    base_build: String,
    active_commit: String,
    artifact_digest: String,
    required_action: String,
    focused_test_receipt: String,
    applied_at: String,
    message: String,
    error: String,
}

impl Default for DeveloperProbe {
    fn default() -> Self {
        Self {
            available: false,
            active: false,
            state: "off".to_string(),
            base_build: String::new(),
            active_commit: String::new(),
            artifact_digest: String::new(),
            required_action: "none".to_string(),
            focused_test_receipt: String::new(),
            applied_at: String::new(),
            message: String::new(),
            error: String::new(),
        }
    }
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
        "developer" => command_developer(rest),
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
        if let Some(developer) = probe_developer_status() {
            print_developer_human_status(&developer);
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
    let developer = probe_developer_status().unwrap_or_default();

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
            "{{\"version\":{},\"session\":{{\"desktop\":{},\"type\":{},\"ready\":{}}},\"terminal\":{{\"command\":{},\"available\":{}}},\"bootc\":{{\"available\":{},\"status\":{},\"exit_code\":{},\"summary\":{}}},\"developer\":{},\"safe_desktop\":{{\"credentials_untouched\":true,\"personal_data_untouched\":true}}}}",
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
            developer_json(&developer),
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
    print_developer_human_status(&developer);
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
            "`update` requires one of `status`, `check`, or `install`",
        ));
    };

    if action == "-h" || action == "--help" {
        print_update_help();
        return Ok(());
    }

    let mut json = false;
    for argument in &arguments[1..] {
        match argument.as_str() {
            "--json" => json = true,
            "-h" | "--help" => {
                print_update_help();
                return Ok(());
            }
            _ => {
                return Err(CliError::usage(
                    "`update status|check|install` accepts only `--json`",
                ))
            }
        }
    }

    match action.as_str() {
        "status" => run_update_action(action, json, UPDATE_STATUS_TIMEOUT),
        "check" => run_update_action(action, json, UPDATE_CHECK_TIMEOUT),
        "install" => run_update_action(action, json, UPDATE_INSTALL_TIMEOUT),
        _ => Err(CliError::usage(
            "`update` requires one of `status`, `check`, or `install`",
        )),
    }
}

fn print_update_help() {
    println!("zeus update status|check|install [--json] — inspect or stage signed OS updates");
    println!("status reads local state; check fetches signed metadata; install starts a privileged background job");
    println!("Installing never reboots automatically; reboot remains an explicit user action.");
}

fn run_update_action(action: &str, json: bool, timeout: Duration) -> Result<(), CliError> {
    let Some(helper) = resolve_update_helper() else {
        if action == "status" {
            return update_status_bootc_fallback(json);
        }
        return Err(CliError::operation(format!(
            "{} is unavailable; `zeus update {}` could not run and no reboot was requested",
            UPDATE_HELPER_PATH, action
        )));
    };

    let arguments = if json {
        vec![action, "--json"]
    } else {
        vec![action]
    };
    let result = run_capture(&helper, &arguments, timeout)?;

    if !result.stdout.is_empty() {
        print!("{}", result.stdout);
    }
    if !result.stderr.is_empty() {
        eprint!("{}", result.stderr);
    }

    if result.status.success() {
        Ok(())
    } else {
        Err(CliError::operation(format!(
            "zeus update {} failed (exit {})",
            action,
            exit_code_text(result.status)
        )))
    }
}

fn update_status_bootc_fallback(json: bool) -> Result<(), CliError> {
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

fn command_developer(arguments: &[String]) -> Result<(), CliError> {
    let Some(action) = arguments.first() else {
        return Err(CliError::usage(
            "`developer` requires one of `status`, `enable`, `apply`, `undo`, or `disable`",
        ));
    };

    if action == "-h" || action == "--help" {
        print_developer_help();
        return Ok(());
    }

    let mut json = false;
    let mut artifact_digest: Option<String> = None;
    for argument in &arguments[1..] {
        match argument.as_str() {
            "--json" if action == "status" => json = true,
            digest if action == "apply" && artifact_digest.is_none() && is_developer_digest(digest) => {
                artifact_digest = Some(digest.to_string());
            }
            "-h" | "--help" => {
                print_developer_help();
                return Ok(());
            }
            _ => {
                return Err(CliError::usage(
                    "`developer status` accepts only `--json`; other Developer Mode actions take no arguments",
                ))
            }
        }
    }

    let timeout = if action == "status" {
        DEVELOPER_STATUS_TIMEOUT
    } else {
        DEVELOPER_ACTION_TIMEOUT
    };
    match action.as_str() {
        "status" | "enable" | "apply" | "undo" | "disable" => {
            run_developer_action(action, json, artifact_digest.as_deref(), timeout)
        }
        _ => Err(CliError::usage(
            "`developer` requires one of `status`, `enable`, `apply`, `undo`, or `disable`",
        )),
    }
}

fn print_developer_help() {
    println!("zeus developer status [--json] — inspect Developer Mode provenance");
    println!("zeus developer enable|apply [ARTIFACT_DIGEST]|undo|disable — run one authenticated local Developer Mode action");
    println!("Actions are delegated to the installed /usr/libexec/zeus-developer helper.");
}

fn run_developer_action(
    action: &str,
    json: bool,
    artifact_digest: Option<&str>,
    timeout: Duration,
) -> Result<(), CliError> {
    let Some(helper) = resolve_developer_helper() else {
        return Err(CliError::operation(format!(
            "{} is unavailable; `zeus developer {}` could not run",
            DEVELOPER_HELPER_PATH, action
        )));
    };

    let mut arguments = vec![action];
    if let Some(digest) = artifact_digest {
        arguments.push(digest);
    }
    if json {
        arguments.push("--json");
    }
    let result = run_capture(&helper, &arguments, timeout)?;

    if !result.stdout.is_empty() {
        print!("{}", result.stdout);
    }
    if !result.stderr.is_empty() {
        eprint!("{}", result.stderr);
    }

    if result.status.success() {
        Ok(())
    } else {
        Err(CliError::operation(format!(
            "zeus developer {} failed (exit {})",
            action,
            exit_code_text(result.status)
        )))
    }
}

fn is_developer_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn resolve_developer_helper() -> Option<PathBuf> {
    // The environment override is intentionally test/development-only.  The
    // installed image always uses the fixed helper path below, and all
    // arguments remain a Rust argv array with no shell interpolation.
    if let Some(value) = env::var_os("ZEUS_DEVELOPER_HELPER") {
        let path = PathBuf::from(value);
        if path.components().count() > 1 || path.is_absolute() {
            return is_executable(&path).then_some(path);
        }
        return find_executable(path.to_str().unwrap_or_default());
    }

    let path = Path::new(DEVELOPER_HELPER_PATH);
    is_executable(path).then(|| path.to_path_buf())
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

fn probe_developer_status() -> Option<DeveloperProbe> {
    let helper = resolve_developer_helper()?;
    let result = match run_capture(&helper, &["status", "--json"], DEVELOPER_STATUS_TIMEOUT) {
        Ok(result) => result,
        Err(error) => {
            return Some(DeveloperProbe {
                available: true,
                active: false,
                state: "error".to_string(),
                error: sanitize_output(&error.message),
                ..DeveloperProbe::default()
            })
        }
    };

    let output = result.stdout.trim();
    let state_value = json_first_string(output, &["state", "mode"]);
    let error = json_first_string(output, &["error", "failure"]).unwrap_or_else(|| {
        if result.status.success() {
            String::new()
        } else {
            sanitize_output(&result.stderr)
        }
    });
    let explicit_active = json_bool_field(output, "active").unwrap_or(false);
    let state = state_value
        .map(|value| normalize_developer_state(&value))
        .or_else(|| explicit_active.then(|| "active".to_string()))
        .or_else(|| {
            json_bool_field(output, "incompatible")
                .unwrap_or(false)
                .then(|| "incompatible".to_string())
        })
        .or_else(|| {
            json_bool_field(output, "applying")
                .unwrap_or(false)
                .then(|| "applying".to_string())
        })
        .or_else(|| {
            json_bool_field(output, "enabled")
                .unwrap_or(false)
                .then(|| "enabled".to_string())
        })
        .unwrap_or_else(|| "error".to_string());
    let active = explicit_active || state == "active";
    Some(DeveloperProbe {
        available: true,
        active,
        state,
        base_build: json_first_string(
            output,
            &[
                "base_build",
                "base_build_id",
                "base_sysext_level",
                "build_id",
            ],
        )
        .unwrap_or_default(),
        active_commit: json_first_string(output, &["active_commit", "source_commit", "commit"])
            .unwrap_or_default(),
        artifact_digest: json_first_string(
            output,
            &[
                "artifact_digest",
                "active_artifact_digest",
                "active_digest",
                "artifact",
            ],
        )
        .unwrap_or_default(),
        required_action: json_first_required_action(output),
        focused_test_receipt: json_first_string(
            output,
            &[
                "focused_test_receipt",
                "test_receipt",
                "focused_tests",
                "focused_test",
            ],
        )
        .unwrap_or_default(),
        applied_at: json_first_string(output, &["applied_at", "application_time", "applied_time"])
            .unwrap_or_default(),
        message: json_first_string(output, &["message", "summary"]).unwrap_or_default(),
        error,
    })
}

fn print_developer_human_status(status: &DeveloperProbe) {
    if !status.available {
        return;
    }
    if status.state.is_empty() {
        println!("Developer Mode: unavailable");
        return;
    }
    println!(
        "Developer Mode: {}{}",
        status.state,
        if status.active { " (DEV)" } else { "" }
    );
    if !status.base_build.is_empty() {
        println!("Developer base: {}", status.base_build);
    }
    if !status.active_commit.is_empty() {
        println!("Developer commit: {}", status.active_commit);
    }
    if !status.artifact_digest.is_empty() {
        println!("Developer artifact: {}", status.artifact_digest);
    }
    if !status.required_action.is_empty() && status.required_action != "none" {
        println!("Developer activation: {}", status.required_action);
    }
    if !status.focused_test_receipt.is_empty() {
        println!("Developer focused tests: {}", status.focused_test_receipt);
    }
    if !status.applied_at.is_empty() {
        println!("Developer applied: {}", status.applied_at);
    }
    if !status.message.is_empty() {
        println!("Developer note: {}", status.message);
    }
    if !status.error.is_empty() {
        println!("Developer error: {}", status.error);
    }
}

fn developer_json(status: &DeveloperProbe) -> String {
    format!(
        "{{\"available\":{},\"active\":{},\"indicator\":{},\"state\":{},\"base_build\":{},\"base_build_id\":{},\"active_commit\":{},\"artifact_digest\":{},\"active_artifact_digest\":{},\"required_action\":{},\"focused_test_receipt\":{},\"applied_at\":{},\"message\":{},\"error\":{}}}",
        json_bool(status.available),
        json_bool(status.active),
        json_string(if status.active { "DEV" } else { "" }),
        json_string(&status.state),
        json_string(&status.base_build),
        json_string(&status.base_build),
        json_string(&status.active_commit),
        json_string(&status.artifact_digest),
        json_string(&status.artifact_digest),
        json_string(&status.required_action),
        json_string(&status.focused_test_receipt),
        json_string(&status.applied_at),
        json_string(&status.message),
        json_string(&status.error),
    )
}

fn json_first_string(value: &str, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| json_string_field(value, key))
}

fn json_first_required_action(value: &str) -> String {
    if json_bool_field(value, "required_logout").unwrap_or(false)
        || json_bool_field(value, "requires_logout").unwrap_or(false)
    {
        return "logout".to_string();
    }
    if json_bool_field(value, "required_restart").unwrap_or(false)
        || json_bool_field(value, "requires_restart").unwrap_or(false)
    {
        return "restart".to_string();
    }
    let candidate = json_first_string(
        value,
        &[
            "required_action",
            "activation",
            "required_restart",
            "requires_restart",
        ],
    )
    .or_else(|| {
        ["required_activation", "activation_actions"]
            .iter()
            .find_map(|key| json_array_first_string_field(value, key))
    })
    .unwrap_or_else(|| "none".to_string());
    normalize_developer_action(&candidate)
}

fn normalize_developer_state(value: &str) -> String {
    let state = value
        .to_ascii_lowercase()
        .replace('-', "_")
        .replace(' ', "_");
    match state.as_str() {
        "disabled" | "inactive" => "off".to_string(),
        "needs_rebuild" => "incompatible".to_string(),
        "needs_attention" | "attention" | "failed" | "interrupted" => "error".to_string(),
        "paused" => "enabled".to_string(),
        "off" | "enabled" | "active" | "incompatible" | "applying" | "error" => state,
        _ => "error".to_string(),
    }
}

fn normalize_developer_action(value: &str) -> String {
    let action = value
        .to_ascii_lowercase()
        .replace('-', "_")
        .replace(' ', "_");
    if matches!(
        action.as_str(),
        "logout" | "logout_login" | "log_out" | "log_out_in"
    ) {
        return "logout".to_string();
    }
    if matches!(action.as_str(), "reboot" | "restart_system" | "restart_os") {
        return "reboot".to_string();
    }
    if action.starts_with("restart") {
        return "restart".to_string();
    }
    if action == "none" {
        return action;
    }
    "none".to_string()
}

fn json_bool_field(value: &str, key: &str) -> Option<bool> {
    let needle = format!("\"{}\"", key);
    let position = value.find(&needle)?;
    let rest = &value[position + needle.len()..];
    let colon = rest.find(':')?;
    let scalar = rest[colon + 1..].trim_start();
    if scalar.starts_with("true") {
        Some(true)
    } else if scalar.starts_with("false") {
        Some(false)
    } else {
        None
    }
}

fn json_string_field(value: &str, key: &str) -> Option<String> {
    let needle = format!("\"{}\"", key);
    let position = value.find(&needle)?;
    let rest = &value[position + needle.len()..];
    let colon = rest.find(':')?;
    let scalar = rest[colon + 1..].trim_start();
    if scalar.starts_with("null") {
        return None;
    }
    json_quoted_string(scalar)
}

fn json_array_first_string_field(value: &str, key: &str) -> Option<String> {
    let needle = format!("\"{}\"", key);
    let position = value.find(&needle)?;
    let rest = &value[position + needle.len()..];
    let colon = rest.find(':')?;
    let scalar = rest[colon + 1..].trim_start();
    let scalar = scalar.strip_prefix('[')?.trim_start();
    if scalar.starts_with(']') {
        return None;
    }
    json_quoted_string(scalar)
}

fn json_quoted_string(scalar: &str) -> Option<String> {
    let mut characters = scalar.chars();
    if characters.next()? != '"' {
        return None;
    }

    let mut output = String::new();
    let mut escaped = false;
    for character in characters {
        if escaped {
            output.push(match character {
                'n' => '\n',
                'r' => '\r',
                't' => '\t',
                '\\' => '\\',
                '"' => '"',
                other => other,
            });
            escaped = false;
        } else if character == '\\' {
            escaped = true;
        } else if character == '"' {
            return Some(output);
        } else {
            output.push(character);
        }
    }
    None
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

fn resolve_update_helper() -> Option<PathBuf> {
    // The environment override exists for unprivileged tests and development
    // fixtures.  Once it is set, an invalid override is treated as absent so
    // a test cannot accidentally invoke a host-installed helper.
    if let Some(value) = env::var_os("ZEUS_UPDATE_HELPER") {
        let path = PathBuf::from(value);
        if path.components().count() > 1 || path.is_absolute() {
            return is_executable(&path).then_some(path);
        }
        return find_executable(path.to_str().unwrap_or_default());
    }

    let path = Path::new(UPDATE_HELPER_PATH);
    is_executable(path).then(|| path.to_path_buf())
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
