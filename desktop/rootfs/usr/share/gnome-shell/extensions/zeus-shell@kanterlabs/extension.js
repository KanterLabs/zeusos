import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const KEYBINDING_NAME = 'spotlight-keybinding';
const MAX_RESULTS = 10;
const SEARCH_REFRESH_DELAY = 80;

function actorNamed(actor, name) {
    if (!actor)
        return null;

    if (actor.get_name?.() === name)
        return actor;

    for (const child of actor.get_children?.() ?? []) {
        const result = actorNamed(child, name);
        if (result)
            return result;
    }

    return null;
}

function setClass(actor, className, enabled) {
    if (!actor)
        return;

    if (enabled)
        actor.add_style_class_name(className);
    else
        actor.remove_style_class_name(className);
}

function booleanSetting(settings, property, fallback) {
    try {
        const value = settings?.[property];
        return typeof value === 'boolean' ? value : fallback;
    } catch {
        return fallback;
    }
}

function reducedMotionEnabled(settings) {
    try {
        const value = settings?.reduced_motion;
        if (typeof value === 'boolean')
            return value;

        // St.ReducedMotion is an enum on newer shells and is absent on GNOME
        // 50, where enable-animations is the available user preference.
        const reduce = St.ReducedMotion?.REDUCE;
        return reduce !== undefined && value === reduce;
    } catch {
        return false;
    }
}

function animationsAllowed(settings = St.Settings.get()) {
    // GNOME 50 exposes enable-animations.  Newer shells additionally expose
    // reduced-motion, so use it when available without requiring that API on
    // the preview's target shell.
    return booleanSetting(settings, 'enable_animations', true) &&
        !reducedMotionEnabled(settings);
}

const ZeusMenuButton = GObject.registerClass(
class ZeusMenuButton extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Zeus menu', false);

        this._extension = extension;
        this.add_style_class_name('zeus-menu-button');
        this.container.add_style_class_name('zeus-menu-container');

        const content = new St.BoxLayout({
            style_class: 'zeus-menu-content',
            y_align: Clutter.ActorAlign.CENTER,
        });
        content.add_child(new St.Label({
            text: 'Z',
            style_class: 'zeus-menu-mark',
            y_align: Clutter.ActorAlign.CENTER,
        }));
        content.add_child(new St.Label({
            text: 'Zeus',
            style_class: 'zeus-menu-label',
            y_align: Clutter.ActorAlign.CENTER,
        }));
        this.add_child(content);

        const spotlightItem = new PopupMenu.PopupMenuItem('Find Applications');
        spotlightItem.connect('activate', () => {
            this.menu.close();
            this._extension.openSearch();
        });
        this.menu.addMenuItem(spotlightItem);

        const overviewItem = new PopupMenu.PopupMenuItem('Show Overview');
        overviewItem.connect('activate', () => {
            this.menu.close();
            if (Main.overview)
                Main.overview.toggle();
        });
        this.menu.addMenuItem(overviewItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._addLauncher('Files', 'org.gnome.Nautilus.desktop');
        this._addLauncher('Settings', 'org.zeus.Settings.desktop');
        this._addLauncher('All Settings', 'org.gnome.Settings.desktop');
        this._addLauncher('Terminal', 'org.gnome.Ptyxis.desktop');
    }

    _addLauncher(label, desktopId) {
        const item = new PopupMenu.PopupMenuItem(label);
        item.connect('activate', () => {
            this.menu.close();
            this._extension.launchDesktopId(desktopId);
        });
        this.menu.addMenuItem(item);
    }
});

const SearchResultButton = GObject.registerClass(
class SearchResultButton extends St.Button {
    _init(app, activate) {
        super._init({
            style_class: 'zeus-search-result',
            reactive: true,
            can_focus: false,
            x_expand: true,
        });

        this.app = app;
        this._activate = activate;

        const row = new St.BoxLayout({
            style_class: 'zeus-search-result-row',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });

        try {
            const icon = app.create_icon_texture(32);
            icon.x_align = Clutter.ActorAlign.CENTER;
            icon.y_align = Clutter.ActorAlign.CENTER;
            row.add_child(icon);
        } catch (error) {
            console.debug(`Zeus search could not load an icon: ${error.message}`);
        }

        this._labelActor = new St.Label({
            text: app.get_name(),
            style_class: 'zeus-search-result-label',
            y_align: Clutter.ActorAlign.CENTER,
            x_expand: true,
        });
        row.add_child(this._labelActor);
        this.label_actor = this._labelActor;
        this.set_child(row);

        this.connect('clicked', () => this._activate(this.app));
    }

    setSelected(selected) {
        if (selected)
            this.add_style_pseudo_class('selected');
        else
            this.remove_style_pseudo_class('selected');
    }
});

class SpotlightDialog {
    constructor(extension) {
        this._extension = extension;
        this._open = false;
        this._modalGrab = null;
        this._rows = [];
        this._selectedIndex = -1;
        this._searchSerial = 0;
        this._refreshId = 0;

        this._overlay = new St.Widget({
            name: 'zeusSpotlightOverlay',
            style_class: 'zeus-spotlight-overlay',
            reactive: true,
            can_focus: true,
            layout_manager: new Clutter.BinLayout(),
            x_expand: true,
            y_expand: true,
        });
        this._overlay.add_constraint(new Clutter.BindConstraint({
            source: global.stage,
            coordinate: Clutter.BindCoordinate.ALL,
        }));

        this._dialog = new St.BoxLayout({
            name: 'zeusSpotlightDialog',
            style_class: 'zeus-spotlight-dialog',
            vertical: true,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._overlay.add_child(this._dialog);

        const heading = new St.BoxLayout({
            style_class: 'zeus-search-heading',
            y_align: Clutter.ActorAlign.CENTER,
        });
        heading.add_child(new St.Label({
            text: 'Zeus Search',
            style_class: 'zeus-search-title',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        }));
        heading.add_child(new St.Label({
            text: 'Super + Space',
            style_class: 'zeus-search-shortcut',
            y_align: Clutter.ActorAlign.CENTER,
        }));
        this._dialog.add_child(heading);

        this._entry = new St.Entry({
            style_class: 'zeus-search-entry',
            hint_text: 'Search applications',
            reactive: true,
            can_focus: true,
            x_expand: true,
        });
        this._entry.set_primary_icon(new St.Icon({
            icon_name: 'system-search-symbolic',
            icon_size: 22,
            style_class: 'zeus-search-entry-icon',
        }));
        this._entry.clutter_text.connect('text-changed', () =>
            this._queueRefreshResults());
        this._entry.clutter_text.connect('key-press-event', (_actor, event) =>
            this._onEntryKeyPress(event));
        this._dialog.add_child(this._entry);

        this._scrollView = new St.ScrollView({
            style_class: 'zeus-search-scroll',
            overlay_scrollbars: true,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            x_expand: true,
            y_expand: true,
        });
        this._resultsBox = new St.BoxLayout({
            style_class: 'zeus-search-results',
            vertical: true,
            x_expand: true,
        });
        this._scrollView.set_child(this._resultsBox);
        this._dialog.add_child(this._scrollView);

        this._emptyLabel = new St.Label({
            text: 'No applications found',
            style_class: 'zeus-search-empty',
            x_align: Clutter.ActorAlign.CENTER,
        });

        this._overlay.connect('button-press-event', (_actor, event) => {
            const target = global.stage.get_event_actor(event);
            if (!this._dialog.contains(target)) {
                this.close();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        // Keep the modal usable even when the initial focus hand-off lands on
        // the overlay.  Only navigation/activation keys are handled here;
        // printable input still propagates to St.Entry and the focused app.
        this._overlay.connect('key-press-event', (_actor, event) =>
            this._onEntryKeyPress(event));
    }

    open() {
        if (this._open)
            return;

        this._open = true;
        this._cancelRefresh();
        this._entry.text = '';
        this._cancelRefresh();
        const animate = animationsAllowed();
        this._overlay.opacity = animate ? 0 : 255;
        Main.layoutManager.addChrome(this._overlay);
        this._overlay.show();
        this._modalGrab = Main.pushModal(this._overlay, {
            actionMode: Shell.ActionMode.POPUP,
        });
        this._entry.clutter_text.grab_key_focus();
        this._entry.clutter_text.set_cursor_visible(true);
        this._refreshResults();
        if (animate) {
            this._overlay.ease({
                opacity: 255,
                duration: 160,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            });
        }
    }

    close() {
        if (!this._open)
            return;

        this._open = false;
        this._cancelRefresh();
        ++this._searchSerial;
        if (this._modalGrab) {
            try {
                Main.popModal(this._modalGrab);
            } catch (error) {
                console.debug(`Zeus search modal was already closed: ${error.message}`);
            }
            this._modalGrab = null;
        }

        this._overlay.hide();
        Main.layoutManager.removeChrome(this._overlay);
    }

    destroy() {
        this.close();
        this._overlay.destroy();
    }

    scheduleRefresh() {
        this._queueRefreshResults();
    }

    _queueRefreshResults() {
        if (!this._open || this._refreshId)
            return;

        this._refreshId = GLib.timeout_add_once(GLib.PRIORITY_DEFAULT,
            SEARCH_REFRESH_DELAY, () => {
                this._refreshId = 0;
                this._refreshResults();
            });
        GLib.Source.set_name_by_id(this._refreshId, '[zeus-shell] refresh search');
    }

    _cancelRefresh() {
        if (!this._refreshId)
            return;

        GLib.source_remove(this._refreshId);
        this._refreshId = 0;
    }

    _onEntryKeyPress(event) {
        const symbol = event.get_key_symbol();
        if (symbol === Clutter.KEY_Escape) {
            this.close();
            return Clutter.EVENT_STOP;
        }

        if (symbol === Clutter.KEY_Down || symbol === Clutter.KEY_Tab) {
            this._select(1);
            return Clutter.EVENT_STOP;
        }

        if (symbol === Clutter.KEY_Up || symbol === Clutter.KEY_ISO_Left_Tab) {
            this._select(-1);
            return Clutter.EVENT_STOP;
        }

        if (symbol === Clutter.KEY_Return || symbol === Clutter.KEY_KP_Enter) {
            const row = this._rows[this._selectedIndex];
            if (row)
                this._extension.launchApp(row.app);
            return Clutter.EVENT_STOP;
        }

        return Clutter.EVENT_PROPAGATE;
    }

    _select(delta) {
        if (this._rows.length === 0)
            return;

        this._rows[this._selectedIndex]?.setSelected(false);
        this._selectedIndex = (this._selectedIndex + delta + this._rows.length) %
            this._rows.length;
        this._rows[this._selectedIndex].setSelected(true);
    }

    _clearResults() {
        for (const row of this._rows)
            row.destroy();
        this._rows = [];
        this._resultsBox.remove_all_children();
        this._selectedIndex = -1;
    }

    _refreshResults() {
        const serial = ++this._searchSerial;
        if (!this._open)
            return;

        try {
            const query = String(this._entry.get_text() ?? '').trim();
            const apps = this._extension.findApplications(query);
            if (!this._open || serial !== this._searchSerial)
                return;

            this._clearResults();
            for (const app of apps) {
                try {
                    const row = new SearchResultButton(app, selectedApp =>
                        this._extension.launchApp(selectedApp));
                    this._rows.push(row);
                    this._resultsBox.add_child(row);
                } catch (error) {
                    console.error(`Zeus could not render search result: ${error.message}`);
                }
            }

            if (this._rows.length === 0) {
                this._resultsBox.add_child(this._emptyLabel);
                return;
            }

            this._selectedIndex = 0;
            this._rows[0].setSelected(true);
        } catch (error) {
            console.error(`Zeus search update failed: ${error.message}`);
            if (!this._open || serial !== this._searchSerial)
                return;

            this._clearResults();
            this._resultsBox.add_child(this._emptyLabel);
        }
    }
}

export default class ZeusShellExtension extends Extension {
    enable() {
        this._settings = null;
        this._keybindingInstalled = false;
        this._panelState = null;
        this._panel = null;
        this._dock = null;
        this._panelWatchActors = [];
        this._dockWatchActors = [];
        this._dockRediscoveryId = 0;
        this._menuButton = null;
        this._panelDivider = null;
        this._appLabel = null;
        this._search = new SpotlightDialog(this);
        this._appSystem = Shell.AppSystem.get_default();
        this._installedApps = null;
        this._tracker = Shell.WindowTracker.get_default();

        Main.sessionMode.connectObject('updated',
            () => this._syncSessionMode(), this);
        this._tracker.connectObject('notify::focus-app',
            () => this._updateFocusedApp(), this);
        this._appSystem.connectObject('installed-changed', () => {
            this._installedApps = null;
            this._search?.scheduleRefresh();
        }, this);

        const shellSettings = St.Settings.get();
        const appearanceChanged = () => this._syncAppearance();
        shellSettings.connectObject(
            'notify::color-scheme', appearanceChanged,
            'notify::high-contrast', appearanceChanged,
            'notify::enable-animations', appearanceChanged, this);
        try {
            if (shellSettings.find_property?.('reduced-motion'))
                shellSettings.connectObject('notify::reduced-motion', appearanceChanged, this);
        } catch {
            // GNOME 50 does not expose reduced-motion as a separate property.
        }

        try {
            this._settings = this.getSettings();
            Main.wm.addKeybinding(
                KEYBINDING_NAME,
                this._settings,
                Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
                Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
                () => this.openSearch());
            this._keybindingInstalled = true;
        } catch (error) {
            console.error(`Zeus Spotlight keybinding unavailable: ${error.message}`);
        }

        this._syncSessionMode();
    }

    disable() {
        this._search?.destroy();
        this._search = null;

        if (this._keybindingInstalled) {
            try {
                Main.wm.removeKeybinding(KEYBINDING_NAME);
            } catch (error) {
                console.debug(`Zeus keybinding cleanup failed: ${error.message}`);
            }
        }
        this._keybindingInstalled = false;
        this._settings = null;

        this._appSystem?.disconnectObject(this);
        this._appSystem = null;
        this._installedApps = null;
        this._cancelDockRediscovery();
        this._stopPanelWatch();
        this._stopDockWatch();
        this._restorePanel();
        this._tracker?.disconnectObject(this);
        Main.sessionMode?.disconnectObject(this);
        St.Settings.get()?.disconnectObject(this);
    }

    openSearch() {
        if (!this._search || Main.sessionMode.isGreeter || Main.sessionMode.isLocked)
            return;

        if (Main.overview?.visible) {
            Main.overview.hide();
            // Overview hides asynchronously.  Opening the modal on the next
            // main-loop turn prevents its search actor from remaining behind
            // the Spotlight overlay when the shortcut is pressed there.
            GLib.idle_add_once(GLib.PRIORITY_DEFAULT, () => {
                if (this._search && !Main.sessionMode.isGreeter &&
                    !Main.sessionMode.isLocked)
                    this._search.open();
            });
            return;
        }

        this._search.open();
    }

    launchDesktopId(desktopId) {
        const app = (this._appSystem ?? Shell.AppSystem.get_default()).lookup_app(desktopId);
        if (app)
            this.launchApp(app);
    }

    launchApp(app) {
        try {
            app.activate();
        } catch (error) {
            console.error(`Zeus could not launch ${app.get_name()}: ${error.message}`);
        }
        this._search?.close();
    }

    findApplications(query) {
        const appSystem = this._appSystem ?? Shell.AppSystem.get_default();
        const apps = [];
        const seen = new Set();

        const addApp = appOrId => {
            const app = typeof appOrId === 'string'
                ? appSystem.lookup_app(appOrId)
                : appOrId;
            if (!app || seen.has(app.get_id()))
                return;

            const info = app.get_app_info();
            if (!info)
                return;
            try {
                if (!info.should_show())
                    return;
            } catch {}

            seen.add(app.get_id());
            apps.push(app);
        };

        if (query !== '') {
            try {
                for (const group of Shell.AppSystem.search(query)) {
                    for (const appId of group) {
                        addApp(appId);
                        if (apps.length >= MAX_RESULTS)
                            return apps;
                    }
                }
            } catch (error) {
                console.debug(`Zeus application search failed: ${error.message}`);
            }

            if (apps.length === 0) {
                const terms = query.toLowerCase().split(/\s+/);
                for (const info of this._getInstalledApplications(appSystem)) {
                    const haystack = [
                        info.get_name(),
                        info.get_description?.() ?? '',
                        info.get_id(),
                    ].join(' ').toLowerCase();
                    if (terms.every(term => haystack.includes(term)))
                        addApp(info.get_id());
                    if (apps.length >= MAX_RESULTS)
                        break;
                }
            }
            return apps;
        }

        try {
            for (const desktopId of global.settings.get_strv('favorite-apps'))
                addApp(desktopId);
            for (const app of appSystem.get_running())
                addApp(app);
        } catch (error) {
            console.debug(`Zeus could not load favorite applications: ${error.message}`);
        }

        const installed = this._getInstalledApplications(appSystem);
        for (const info of installed) {
            addApp(info.get_id());
            if (apps.length >= MAX_RESULTS)
                break;
        }
        return apps.slice(0, MAX_RESULTS);
    }

    _getInstalledApplications(appSystem) {
        if (!this._installedApps) {
            this._installedApps = Array.from(appSystem.get_installed()).sort((a, b) =>
                a.get_name().localeCompare(b.get_name()));
        }
        return this._installedApps;
    }

    _syncSessionMode() {
        if (!Main.sessionMode || Main.sessionMode.isGreeter || Main.sessionMode.isLocked) {
            this._search?.close();
            this._restorePanel();
            return;
        }

        this._customizePanel();
        if (!this._panelState)
            this._watchPanel();
    }

    _watchPanel() {
        if (this._panelState || this._panelWatchActors.length > 0)
            return;

        const panel = Main.panel;
        const actors = [panel, panel?._leftBox, panel?._centerBox, panel?._rightBox]
            .filter(Boolean);
        if (actors.length === 0)
            return;

        const panelChanged = () => {
            if (this._panelState) {
                this._stopPanelWatch();
                return;
            }
            this._customizePanel();
        };
        this._panelWatchActors = [...new Set(actors)];
        for (const actor of this._panelWatchActors)
            actor.connectObject('child-added', panelChanged, this);
        panelChanged();
    }

    _stopPanelWatch() {
        for (const actor of this._panelWatchActors)
            actor?.disconnectObject(this);
        this._panelWatchActors = [];
    }

    _customizePanel() {
        const panel = Main.panel;
        const leftBox = panel?._leftBox;
        const centerBox = panel?._centerBox;
        const rightBox = panel?._rightBox;
        if (!panel || !leftBox || !centerBox || !rightBox)
            return;

        if (!this._panelState) {
            const dateContainer = panel.statusArea?.dateMenu?.container ?? null;
            const activitiesContainer = panel.statusArea?.activities?.container ?? null;
            // Wait for Panel._updatePanel() to materialize native indicators;
            // child-added signals below will retry this once those actors exist.
            if (!dateContainer || !activitiesContainer)
                return;

            this._panelState = {
                panel,
                dateContainer,
                dateParent: dateContainer?.get_parent() ?? null,
                dateIndex: dateContainer?.get_parent()
                    ?.get_children().indexOf(dateContainer) ?? -1,
                activitiesContainer,
                activitiesVisible: activitiesContainer?.visible ?? false,
            };
            this._panel = panel;
            this._stopPanelWatch();
        }

        const {dateContainer, activitiesContainer} = this._panelState;
        if (dateContainer && dateContainer.get_parent() !== rightBox) {
            dateContainer.get_parent()?.remove_child(dateContainer);
            rightBox.insert_child_at_index(dateContainer, 0);
        }
        activitiesContainer?.hide();

        if (!this._menuButton) {
            this._menuButton = new ZeusMenuButton(this);
            panel.menuManager.addMenu(this._menuButton.menu);
            leftBox.insert_child_at_index(this._menuButton.container, 0);
        }
        if (!this._panelDivider) {
            this._panelDivider = new St.Widget({
                style_class: 'zeus-panel-divider',
                width: 1,
                height: 16,
                y_align: Clutter.ActorAlign.CENTER,
            });
            leftBox.insert_child_at_index(this._panelDivider, 1);
        }
        if (!this._appLabel) {
            this._appLabel = new St.Label({
                name: 'zeusFocusedApp',
                text: 'Desktop',
                style_class: 'zeus-app-name',
                y_align: Clutter.ActorAlign.CENTER,
                x_expand: false,
            });
            leftBox.insert_child_at_index(this._appLabel, 2);
        }

        panel.add_style_class_name('zeus-panel');
        this._updateFocusedApp();
        this._syncAppearance();
        this._watchDock();
    }

    _restorePanel() {
        this._cancelDockRediscovery();
        this._stopPanelWatch();
        this._stopDockWatch();

        const state = this._panelState;
        if (!state)
            return;

        for (const actor of [this._appLabel, this._panelDivider]) {
            actor?.get_parent()?.remove_child(actor);
            actor?.destroy();
        }
        this._appLabel = null;
        this._panelDivider = null;

        if (this._menuButton) {
            this._menuButton.container?.get_parent()?.remove_child(this._menuButton.container);
            this._menuButton.destroy();
            this._menuButton = null;
        }

        const {dateContainer, dateParent, dateIndex, activitiesContainer,
            activitiesVisible, panel} = state;
        if (dateContainer && dateParent) {
            dateContainer.get_parent()?.remove_child(dateContainer);
            dateParent.insert_child_at_index(
                dateContainer,
                Math.max(0, Math.min(dateIndex, dateParent.get_n_children())));
        }
        if (activitiesContainer && activitiesVisible)
            activitiesContainer.show();

        panel.remove_style_class_name('zeus-panel');
        this._removeAppearanceClasses(panel);
        this._dock?.disconnectObject(this);
        this._removeAppearanceClasses(this._dock);
        this._dock = null;
        this._panel = null;
        this._panelState = null;
    }

    _updateFocusedApp() {
        if (!this._appLabel)
            return;

        const app = this._tracker?.focus_app;
        const name = app?.get_name() || 'Desktop';
        if (this._appLabel.text !== name)
            this._appLabel.text = name;
    }

    _syncAppearance() {
        const panel = this._panel;
        if (!panel)
            return;

        const settings = St.Settings.get();
        const light = settings.color_scheme === St.SystemColorScheme.PREFER_LIGHT;
        const highContrast = settings.high_contrast;
        const reducedMotion = !animationsAllowed(settings);
        for (const actor of [panel, this._dock, this._search?._overlay]) {
            setClass(actor, 'zeus-light', light);
            setClass(actor, 'zeus-dark', !light);
            setClass(actor, 'zeus-high-contrast', highContrast);
            setClass(actor, 'zeus-reduced-motion', reducedMotion);
        }
    }

    _removeAppearanceClasses(actor) {
        for (const className of [
            'zeus-light', 'zeus-dark', 'zeus-high-contrast', 'zeus-reduced-motion', 'zeus-dock',
        ])
            setClass(actor, className, false);
    }

    _watchDock() {
        if (this._dock || this._dockWatchActors.length > 0 || this._dockRediscoveryId)
            return;

        const uiGroup = Main.layoutManager?.uiGroup;
        if (!uiGroup)
            return;

        const dockAdded = () => {
            const dock = actorNamed(uiGroup, 'dashtodockContainer');
            if (!dock)
                return;

            this._stopDockWatch();
            this._dock = dock;
            this._cancelDockRediscovery();
            dock.connectObject('destroy', () => {
                if (this._dock !== dock)
                    return;
                this._dock = null;
                // Clutter may emit destroy before removing the actor from its
                // parent.  Let the current destruction finish before scanning
                // uiGroup again, while keeping discovery event-driven.
                this._scheduleDockRediscovery();
            }, this);
            dock.add_style_class_name('zeus-dock');
            this._syncAppearance();
        };
        this._dockWatchActors = [uiGroup];
        uiGroup.connectObject('child-added', dockAdded, this);
        dockAdded();
    }

    _scheduleDockRediscovery() {
        if (this._dock || this._dockRediscoveryId || !this._panelState)
            return;

        this._dockRediscoveryId = GLib.idle_add_once(GLib.PRIORITY_DEFAULT, () => {
            this._dockRediscoveryId = 0;
            if (this._panelState && !this._dock)
                this._watchDock();
        });
    }

    _cancelDockRediscovery() {
        if (!this._dockRediscoveryId)
            return;

        GLib.source_remove(this._dockRediscoveryId);
        this._dockRediscoveryId = 0;
    }

    _stopDockWatch() {
        for (const actor of this._dockWatchActors)
            actor?.disconnectObject(this);
        this._dockWatchActors = [];
    }
}
