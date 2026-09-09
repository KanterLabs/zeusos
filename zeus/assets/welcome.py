#!/usr/bin/env python3
"""A small native welcome window for the Zeus OS preview.

The welcome app deliberately uses normal desktop launchers and a short,
readable message.  It does not own setup or recovery state; those capabilities
will arrive through the shared ``zeus`` command surface in a later preview.
"""

from pathlib import Path
import os
import shutil
import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango


VERSION = "0.1.0-preview.2"
try:
    BUILD_ID = Path("/usr/share/zeus/build-id").read_text().strip()
except OSError:
    BUILD_ID = "development"
APPLICATION_ID = "org.zeus.Welcome"
UPDATES_APPLICATION_ID = "org.zeus.Updates"


CSS = """
.zeus-window {
  background: @window_bg_color;
}

.hero {
  padding: 38px 40px 22px;
}

.eyebrow {
  color: @accent_color;
  font-size: 0.78em;
  font-weight: 700;
  letter-spacing: 0.12em;
}

.hero-title {
  color: @window_fg_color;
  font-size: 2.25em;
  font-weight: 800;
  letter-spacing: -0.03em;
  margin-top: 7px;
}

.hero-copy {
  color: alpha(@window_fg_color, 0.76);
  font-size: 1.08em;
  line-height: 1.4;
  margin-top: 10px;
}

.version-pill {
  background: alpha(@accent_bg_color, 0.12);
  border-radius: 999px;
  color: @accent_color;
  font-size: 0.82em;
  font-weight: 700;
  margin-top: 17px;
  padding: 6px 11px;
}

.update-entry {
  background: alpha(@accent_bg_color, 0.08);
  border: 1px solid alpha(@accent_bg_color, 0.14);
  border-radius: 14px;
  margin-top: 13px;
  padding: 9px 11px;
}

.update-icon {
  color: @accent_color;
  margin-right: 10px;
}

.update-title {
  color: @window_fg_color;
  font-weight: 700;
}

.update-copy {
  color: alpha(@window_fg_color, 0.66);
  font-size: 0.84em;
  margin-top: 2px;
}

.update-button {
  margin-left: 12px;
}

.section-title {
  color: @window_fg_color;
  font-size: 0.82em;
  font-weight: 700;
  letter-spacing: 0.08em;
  margin-bottom: 10px;
}

.cards {
  padding: 0 40px 24px;
}

.welcome-card {
  background: @card_bg_color;
  border: 1px solid rgba(39, 54, 98, 0.10);
  border-radius: 18px;
  padding: 20px;
}

.card-icon {
  background: alpha(@accent_bg_color, 0.12);
  border-radius: 13px;
  color: @accent_color;
  padding: 11px;
}

.card-title {
  color: @window_fg_color;
  font-size: 1.13em;
  font-weight: 750;
  margin-top: 16px;
}

.card-copy {
  color: alpha(@window_fg_color, 0.70);
  line-height: 1.35;
  margin-top: 7px;
}

.card-button {
  margin-top: 18px;
}

.shortcuts {
  background: alpha(@accent_bg_color, 0.08);
  border: 1px solid alpha(@accent_bg_color, 0.13);
  border-radius: 18px;
  margin: 0 40px 24px;
  padding: 18px 20px 19px;
}

.shortcut-list {
  margin-top: 4px;
}

.shortcut-row {
  min-height: 30px;
}

.shortcut-key {
  background: @card_bg_color;
  border: 1px solid alpha(@window_fg_color, 0.12);
  border-radius: 8px;
  color: @window_fg_color;
  font-size: 0.82em;
  font-weight: 700;
  min-width: 150px;
  padding: 5px 9px;
}

.shortcut-copy {
  color: alpha(@window_fg_color, 0.72);
  margin-left: 12px;
}

.shortcut-note {
  color: alpha(@window_fg_color, 0.60);
  font-size: 0.82em;
  margin-top: 10px;
}

.journey {
  background: alpha(@accent_bg_color, 0.12);
  border-radius: 18px;
  margin: 0 40px 38px;
  padding: 21px 23px;
}

.journey-title {
  color: @accent_color;
  font-weight: 750;
}

.journey-copy {
  color: alpha(@window_fg_color, 0.75);
  line-height: 1.4;
  margin-top: 6px;
}

.footer-copy {
  color: alpha(@window_fg_color, 0.66);
  font-size: 0.86em;
  padding: 0 40px 30px;
}
"""


def make_label(text, css_class, *, wrap=False, max_width_chars=None):
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.add_css_class(css_class)
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_hexpand(True)
        if max_width_chars is not None:
            # A wrapped label otherwise advertises its one-line natural width
            # to a Grid/ScrolledWindow and can force the whole window wider
            # than the display before GTK gets a chance to wrap it.
            label.set_max_width_chars(max_width_chars)
    return label


class WelcomeWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Welcome to Zeus OS")
        self.set_default_size(820, 640)
        self.set_size_request(600, 500)
        self.add_css_class("zeus-window")

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(True)
        header.set_show_end_title_buttons(True)
        header.set_title_widget(make_label("Zeus OS", "section-title"))
        toolbar.add_top_bar(header)

        toast_overlay = Adw.ToastOverlay()
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(820)
        clamp.set_tightening_threshold(600)
        clamp.set_child(self._build_content())
        scroller.set_child(clamp)
        toast_overlay.set_child(scroller)
        self._toast_overlay = toast_overlay
        toolbar.set_content(toast_overlay)
        self.set_content(toolbar)

    def _build_content(self):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hero.add_css_class("hero")
        hero.append(make_label("A QUIET START", "eyebrow"))
        hero.append(make_label("Welcome to Zeus OS", "hero-title"))
        hero.append(
            make_label(
                "A calm, capable desktop for focused work — with the essentials close and the rest out of your way.",
                "hero-copy",
                wrap=True,
                max_width_chars=72,
            )
        )
        hero.append(make_label(f"{VERSION} · {BUILD_ID}", "version-pill"))
        hero.append(self._build_update_entry())
        content.append(hero)

        cards_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        cards_section.add_css_class("cards")
        cards_section.append(make_label("READY WHEN YOU ARE", "section-title"))

        cards = Gtk.Grid(column_spacing=14, row_spacing=14)
        cards.set_column_homogeneous(True)
        cards.attach(
            self._make_card(
                "folder-documents-symbolic",
                "Files",
                "Browse your home folder and keep the things you need close at hand.",
                "Open Files",
                self._open_files,
            ),
            0,
            0,
            1,
            1,
        )
        cards.attach(
            self._make_card(
                "folder-download-symbolic",
                "Temp",
                "New downloads land here. Choose when they clear, or Keep the files you need.",
                "Open Temp",
                self._open_temp,
            ),
            1,
            0,
            1,
            1,
        )
        cards.attach(
            self._make_card(
                "preferences-system-symbolic",
                "Settings",
                "Wi-Fi, Bluetooth, brightness and battery controls, together in one place.",
                "Open Settings",
                self._open_settings,
            ),
            2,
            0,
            1,
            1,
        )
        cards_section.append(cards)
        content.append(cards_section)
        content.append(self._build_shortcuts())

        journey = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        journey.add_css_class("journey")
        journey.append(make_label("A PREVIEW WITH A CLEAR PATH", "journey-title"))
        journey.append(
            make_label(
                "This first preview focuses on a polished local desktop. Future updates will make it simple to return to your trusted remote workspace and bring your personal setup to a new laptop.",
                "journey-copy",
                wrap=True,
                max_width_chars=100,
            )
        )
        content.append(journey)
        content.append(
            make_label(
                "You can always open the standard GNOME applications from the top bar.",
                "footer-copy",
                wrap=True,
                max_width_chars=80,
            )
        )
        return content

    def _build_update_entry(self):
        entry = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        entry.add_css_class("update-entry")

        icon = Gtk.Image.new_from_icon_name("software-update-available-symbolic")
        icon.set_pixel_size(20)
        icon.add_css_class("update-icon")
        icon.set_valign(Gtk.Align.CENTER)
        entry.append(icon)

        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        copy.set_hexpand(True)
        copy.set_valign(Gtk.Align.CENTER)
        copy.append(make_label("Software Updates", "update-title"))
        copy.append(
            make_label(
                "Review signed OS updates when you are ready.",
                "update-copy",
                wrap=True,
                max_width_chars=52,
            )
        )
        entry.append(copy)

        button = Gtk.Button(label="Open Updates")
        button.set_valign(Gtk.Align.CENTER)
        button.add_css_class("update-button")
        button.add_css_class("flat")
        button.set_tooltip_text(f"Open {UPDATES_APPLICATION_ID}")
        button.connect("clicked", self._open_updates)
        entry.append(button)
        return entry

    def _build_shortcuts(self):
        shortcuts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        shortcuts.add_css_class("shortcuts")
        shortcuts.append(make_label("KEYBOARD SHORTCUTS", "section-title"))

        shortcut_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shortcut_list.add_css_class("shortcut-list")
        for shortcut, action in (
            ("Super + Space", "Find applications"),
            ("Super + E", "Open Files"),
            ("Super + Shift + T", "Open Temp"),
            ("Super + Return", "Open Terminal"),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            row.add_css_class("shortcut-row")
            key = make_label(shortcut, "shortcut-key")
            key.set_xalign(0.5)
            row.append(key)
            row.append(make_label(action, "shortcut-copy", wrap=True, max_width_chars=60))
            shortcut_list.append(row)
        shortcuts.append(shortcut_list)
        shortcuts.append(
            make_label(
                "Shortcuts can be customized in Settings; existing personal settings are preserved.",
                "shortcut-note",
                wrap=True,
                max_width_chars=96,
            )
        )
        return shortcuts

    def _make_card(self, icon_name, title, copy, action_text, callback):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.set_hexpand(True)
        card.set_vexpand(True)
        card.add_css_class("welcome-card")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(26)
        icon.add_css_class("card-icon")
        icon.set_halign(Gtk.Align.START)
        card.append(icon)
        card.append(make_label(title, "card-title"))
        card.append(make_label(copy, "card-copy", wrap=True, max_width_chars=32))
        button = Gtk.Button(label=action_text)
        button.set_halign(Gtk.Align.START)
        button.add_css_class("card-button")
        button.add_css_class("flat")
        button.connect("clicked", callback)
        card.append(button)
        return card

    def _launch_uri(self, uri, fallback):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as error:
            self._show_toast(f"Could not open {fallback}: {error.message}")

    def _launch_command(self, candidates, label):
        for candidate in candidates:
            executable = shutil.which(candidate)
            if executable:
                try:
                    Gio.Subprocess.new([executable], Gio.SubprocessFlags.NONE)
                    return
                except GLib.Error as error:
                    self._show_toast(f"Could not open {label}: {error.message}")
                    return
        self._show_toast(f"{label} is not available in this preview.")

    def _open_files(self, _button):
        home_uri = GLib.filename_to_uri(os.path.expanduser("~"), None)
        self._launch_uri(home_uri, "Files")

    def _open_temp(self, _button):
        self._launch_command(["/usr/libexec/zeus-temp-window"], "Temp")

    def _open_settings(self, _button):
        desktop = Gio.DesktopAppInfo.new("org.zeus.Settings.desktop")
        if desktop is not None:
            try:
                desktop.launch([], self.get_display().get_app_launch_context())
                return
            except GLib.Error:
                pass
        self._launch_command(["/usr/libexec/zeus-settings-window", "gnome-control-center"], "Settings")

    def _open_updates(self, _button):
        desktop = Gio.DesktopAppInfo.new(f"{UPDATES_APPLICATION_ID}.desktop")
        if desktop is not None:
            try:
                # Give GNOME the user activation context so an existing Updates
                # window can come forward on Wayland after this button click.
                desktop.launch([], self.get_display().get_app_launch_context())
            except GLib.Error as error:
                self._show_toast(f"Could not open Software Updates: {error.message}")
            return
        self._launch_command(["/usr/libexec/zeus-update-window"], "Software Updates")

    def _open_terminal(self, _button):
        self._launch_command(["ptyxis", "gnome-terminal", "kgx"], "Terminal")

    def _show_toast(self, message):
        self._toast_overlay.add_toast(Adw.Toast.new(message))


class WelcomeApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID)

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = WelcomeWindow(self)
        window.present()


def main():
    return WelcomeApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
