#!/usr/bin/env python3
"""A small native welcome window for the Zeus OS preview.

The welcome app deliberately uses normal desktop launchers and a short,
readable message.  It does not own setup or recovery state; those capabilities
will arrive through the shared ``zeus`` command surface in a later preview.
"""

import os
import shutil
import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk


VERSION = "0.1.0-preview.1"
APPLICATION_ID = "org.zeus.Welcome"


CSS = """
.zeus-window {
  background: #f7f8fc;
}

.hero {
  padding: 38px 40px 22px;
}

.eyebrow {
  color: #5268b8;
  font-size: 0.78em;
  font-weight: 700;
  letter-spacing: 0.12em;
}

.hero-title {
  color: #17213c;
  font-size: 2.25em;
  font-weight: 800;
  letter-spacing: -0.03em;
  margin-top: 7px;
}

.hero-copy {
  color: #4e5870;
  font-size: 1.08em;
  line-height: 1.4;
  margin-top: 10px;
}

.version-pill {
  background: #e7ebff;
  border-radius: 999px;
  color: #4259ae;
  font-size: 0.82em;
  font-weight: 700;
  margin-top: 17px;
  padding: 6px 11px;
}

.section-title {
  color: #25304d;
  font-size: 0.82em;
  font-weight: 700;
  letter-spacing: 0.08em;
  margin-bottom: 10px;
}

.cards {
  padding: 0 40px 24px;
}

.welcome-card {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(39, 54, 98, 0.10);
  border-radius: 18px;
  padding: 20px;
}

.card-icon {
  background: #edf0ff;
  border-radius: 13px;
  color: #4b61bb;
  padding: 11px;
}

.card-title {
  color: #202a45;
  font-size: 1.13em;
  font-weight: 750;
  margin-top: 16px;
}

.card-copy {
  color: #667089;
  line-height: 1.35;
  margin-top: 7px;
}

.card-button {
  margin-top: 18px;
}

.journey {
  background: #e9edff;
  border-radius: 18px;
  margin: 0 40px 38px;
  padding: 21px 23px;
}

.journey-title {
  color: #2f438e;
  font-weight: 750;
}

.journey-copy {
  color: #53638f;
  line-height: 1.4;
  margin-top: 6px;
}

.footer-copy {
  color: #737c91;
  font-size: 0.86em;
  padding: 0 40px 30px;
}
"""


def make_label(text, css_class, *, wrap=False):
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.add_css_class(css_class)
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
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
        header.set_title_widget(make_label("Zeus OS", "section-title"))
        toolbar.add_top_bar(header)

        toast_overlay = Adw.ToastOverlay()
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._build_content())
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
            )
        )
        hero.append(make_label(f"Preview {VERSION}", "version-pill"))
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
                "emblem-system-symbolic",
                "Settings",
                "Shape your desktop, connections, displays, and accessibility preferences.",
                "Open Settings",
                self._open_settings,
            ),
            1,
            0,
            1,
            1,
        )
        cards.attach(
            self._make_card(
                "utilities-terminal-symbolic",
                "Terminal",
                "Open Ptyxis for a direct, comfortable command line when you need it.",
                "Open Terminal",
                self._open_terminal,
            ),
            2,
            0,
            1,
            1,
        )
        cards_section.append(cards)
        content.append(cards_section)

        journey = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        journey.add_css_class("journey")
        journey.append(make_label("A PREVIEW WITH A CLEAR PATH", "journey-title"))
        journey.append(
            make_label(
                "This first preview focuses on a polished local desktop. Future updates will make it simple to return to your trusted remote workspace and bring your personal setup to a new laptop.",
                "journey-copy",
                wrap=True,
            )
        )
        content.append(journey)
        content.append(
            make_label(
                "You can always open the standard GNOME applications from the top bar.",
                "footer-copy",
                wrap=True,
            )
        )
        return content

    def _make_card(self, icon_name, title, copy, action_text, callback):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("welcome-card")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(26)
        icon.add_css_class("card-icon")
        icon.set_halign(Gtk.Align.START)
        card.append(icon)
        card.append(make_label(title, "card-title"))
        card.append(make_label(copy, "card-copy", wrap=True))
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

    def _open_settings(self, _button):
        self._launch_command(["gnome-control-center"], "Settings")

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
