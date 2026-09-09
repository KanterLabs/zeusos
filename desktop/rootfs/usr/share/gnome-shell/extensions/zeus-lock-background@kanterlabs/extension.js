import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// UnlockDialog's native effect is a Shell.BlurEffect named "blur".  GNOME 50
// uses 90px and 0.65 in unlockDialog.js; a small physical-pixel radius keeps
// the stock effect's readability while letting the wallpaper show through.
const LOCK_RADIUS = 8;
const LOCK_BRIGHTNESS = 0.8;
const LOCK_GROUP_NAME = 'lockDialogGroup';
const UNLOCK_DIALOG_CLASS = 'unlock-dialog';
const BACKGROUND_CLASS = 'screen-shield-background';
const BLUR_NAME = 'blur';

function childrenOf(actor) {
    try {
        return actor?.get_children?.() ?? [];
    } catch {
        return [];
    }
}

function hasStyleClass(actor, className) {
    try {
        if (actor?.has_style_class_name?.(className))
            return true;

        return (actor?.get_style_class_name?.() ?? '')
            .split(/\s+/)
            .includes(className);
    } catch {
        return false;
    }
}

function finiteProperty(effect, property) {
    try {
        const value = Number(effect[property]);
        return Number.isFinite(value) ? value : null;
    } catch {
        return null;
    }
}

export default class ZeusLockBackgroundExtension extends Extension {
    enable() {
        this._enabled = true;
        this._syncId = 0;
        this._watchedActors = new Set();
        this._effects = new Map();
        try {
            this._settings = St.Settings.get();
        } catch {
            this._settings = null;
        }
        try {
            this._themeContext = St.ThemeContext.get_for_stage(global.stage);
        } catch {
            this._themeContext = null;
        }

        // These are the signals used by GNOME 50's ScreenShield and
        // UnlockDialog lifecycle.  A single idle coalesces actor creation and
        // lets native monitor/scale handlers finish before we read the effect.
        Main.sessionMode?.connectObject('updated',
            () => this._scheduleSync(), this);
        Main.layoutManager?.connectObject('monitors-changed',
            () => this._scheduleSync(), this);
        this._themeContext?.connectObject('notify::scale-factor',
            () => this._scheduleSync(), this);
        this._settings?.connectObject('notify::high-contrast',
            () => this._scheduleSync(), this);

        this._syncEffects();
        this._scheduleSync();
    }

    disable() {
        this._enabled = false;
        this._cancelSync();
        this._restoreEffects();

        for (const actor of this._watchedActors) {
            try {
                actor?.disconnectObject(this);
            } catch {
                // Destruction may already have disconnected this actor.
            }
        }
        this._watchedActors.clear();

        this._themeContext?.disconnectObject(this);
        this._settings?.disconnectObject(this);
        Main.layoutManager?.disconnectObject(this);
        Main.sessionMode?.disconnectObject(this);

        this._themeContext = null;
        this._settings = null;
        this._effects.clear();
    }

    _scheduleSync() {
        if (!this._enabled || this._syncId)
            return;

        this._syncId = GLib.idle_add_once(GLib.PRIORITY_DEFAULT, () => {
            this._syncId = 0;
            if (this._enabled)
                this._syncEffects();
        });
    }

    _cancelSync() {
        if (!this._syncId)
            return;

        GLib.source_remove(this._syncId);
        this._syncId = 0;
    }

    _isLocked() {
        try {
            return Main.sessionMode?.isLocked === true;
        } catch {
            return false;
        }
    }

    _isHighContrast() {
        try {
            const value = this._settings?.high_contrast;
            return typeof value === 'boolean' ? value : true;
        } catch {
            // If the accessibility preference cannot be read, retain stock
            // effects instead of risking a custom low-contrast presentation.
            return true;
        }
    }

    _scaleFactor() {
        try {
            const scale = Number(this._themeContext?.scale_factor ?? 1);
            return Number.isFinite(scale) && scale > 0 ? scale : 1;
        } catch {
            return 1;
        }
    }

    _discoverEffects() {
        const shieldGroup = Main.layoutManager?.screenShieldGroup;
        this._watchActor(shieldGroup);
        if (!shieldGroup)
            return [];

        // ScreenShield creates lockDialogGroup as a direct child.  Looking up
        // only this path avoids traversing or touching authentication actors.
        const lockGroup = childrenOf(shieldGroup).find(actor => {
            try {
                return actor.get_name?.() === LOCK_GROUP_NAME;
            } catch {
                return false;
            }
        });
        this._watchActor(lockGroup);
        if (!lockGroup)
            return [];

        const unlockDialog = childrenOf(lockGroup).find(actor =>
            hasStyleClass(actor, UNLOCK_DIALOG_CLASS));
        this._watchActor(unlockDialog);
        if (!unlockDialog)
            return [];

        // UnlockDialog's source-owned _backgroundGroup contains one
        // screen-shield-background per monitor.  Do not inspect its dialog
        // children, which include the native unlock/authentication UI.
        const backgroundGroup = unlockDialog._backgroundGroup;
        this._watchActor(backgroundGroup);
        if (!backgroundGroup)
            return [];

        const effects = [];
        for (const actor of childrenOf(backgroundGroup)) {
            if (!hasStyleClass(actor, BACKGROUND_CLASS))
                continue;

            let effect;
            try {
                effect = actor.get_effect?.(BLUR_NAME);
                if (!(effect instanceof Shell.BlurEffect))
                    return null;
            } catch {
                // An unavailable or unexpected effect leaves GNOME's native
                // blur untouched.
                return null;
            }

            const radius = finiteProperty(effect, 'radius');
            const brightness = finiteProperty(effect, 'brightness');
            if (radius === null || brightness === null ||
                typeof effect.set !== 'function')
                return null;

            this._watchActor(actor);
            effects.push({actor, effect});
        }
        return effects;
    }

    _syncEffects() {
        if (!this._enabled)
            return;

        if (!this._isLocked() || this._isHighContrast()) {
            this._restoreEffects();
            return;
        }

        const candidates = this._discoverEffects();
        if (!candidates || candidates.length === 0) {
            this._restoreEffects();
            return;
        }

        const currentEffects = new Set(candidates.map(candidate => candidate.effect));
        for (const [effect, record] of this._effects) {
            if (currentEffects.has(effect))
                continue;

            this._restoreRecord(record);
            this._effects.delete(effect);
        }

        const radius = LOCK_RADIUS * this._scaleFactor();
        for (const candidate of candidates) {
            if (!this._applyEffect(candidate.effect, candidate.actor, radius)) {
                this._restoreEffects();
                return;
            }
        }
    }

    _applyEffect(effect, actor, radius) {
        let currentRadius = finiteProperty(effect, 'radius');
        let currentBrightness = finiteProperty(effect, 'brightness');
        if (currentRadius === null || currentBrightness === null)
            return false;

        let record = this._effects.get(effect);
        if (!record) {
            record = {
                effect,
                actor,
                radius: currentRadius,
                brightness: currentBrightness,
                appliedRadius: null,
                appliedBrightness: null,
            };
            this._effects.set(effect, record);
        } else if (record.appliedRadius !== null) {
            // Native UnlockDialog updates the effect when scale or monitors
            // change.  Capture each changed native property independently so
            // an unchanged custom value is not mistaken for a new baseline.
            if (currentRadius !== record.appliedRadius)
                record.radius = currentRadius;
            if (currentBrightness !== record.appliedBrightness)
                record.brightness = currentBrightness;
        }

        // Mark both attempted values as owned before calling set().  GObject
        // property updates can be partial before throwing; keeping ownership
        // until the catch block lets _restoreRecord put any changed property
        // back before this record is discarded.
        record.appliedRadius = radius;
        record.appliedBrightness = LOCK_BRIGHTNESS;
        try {
            effect.set({radius, brightness: LOCK_BRIGHTNESS});
        } catch {
            this._restoreRecord(record, {
                radius: currentRadius,
                brightness: currentBrightness,
            });
            this._effects.delete(effect);
            return false;
        }

        return true;
    }

    _restoreRecord(record, changedFrom = null) {
        const restore = {};
        try {
            // Native UnlockDialog may have reset either property after our
            // last sync (for example, to 90px * a newly selected scale).
            // Restore only properties that still carry our last value so a
            // native update is never overwritten by an old-scale snapshot.
            const radius = finiteProperty(record.effect, 'radius');
            const brightness = finiteProperty(record.effect, 'brightness');
            if (changedFrom?.radius !== undefined &&
                radius !== null && radius !== changedFrom.radius)
                restore.radius = record.radius;
            else if (record.appliedRadius !== null &&
                radius !== null && radius === record.appliedRadius)
                restore.radius = record.radius;
            if (changedFrom?.brightness !== undefined &&
                brightness !== null && brightness !== changedFrom.brightness)
                restore.brightness = record.brightness;
            else if (record.appliedBrightness !== null &&
                brightness !== null && brightness === record.appliedBrightness)
                restore.brightness = record.brightness;

            if (Object.keys(restore).length > 0)
                record.effect.set(restore);
        } catch {
            // The actor/effect may already be in Clutter destruction.
        }
        record.appliedRadius = null;
        record.appliedBrightness = null;
    }

    _restoreEffects() {
        for (const record of this._effects.values())
            this._restoreRecord(record);
        this._effects.clear();
    }

    _watchActor(actor) {
        if (!actor || this._watchedActors.has(actor))
            return;

        this._watchedActors.add(actor);
        try {
            actor.connectObject('child-added', () => this._scheduleSync(), this);
        } catch {
            // Expected actors are Clutter containers.  If an optional child
            // signal is unavailable, retaining the destroy watch is still
            // enough to restore any effect that belongs to this actor.
        }
        try {
            actor.connectObject('destroy', () => this._actorDestroyed(actor), this);
        } catch {
            this._watchedActors.delete(actor);
        }
    }

    _actorDestroyed(actor) {
        this._watchedActors.delete(actor);
        for (const [effect, record] of this._effects) {
            if (record.actor !== actor)
                continue;

            this._restoreRecord(record);
            this._effects.delete(effect);
        }
        this._scheduleSync();
    }
}
