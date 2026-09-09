import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Exercise the shipped extension's lifecycle with a small signal/actor model.
// Real GNOME loading, rendering and native authentication are qualified on VM115.
const source = readFileSync(new URL('../desktop/rootfs/usr/share/gnome-shell/extensions/zeus-lock-background@kanterlabs/extension.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '')
    .replace('export default class ', 'class ');

class Signals {
    connections = [];
    connectObject(signal, callback, owner) {
        this.connections.push({signal, callback, owner});
    }
    disconnectObject(owner) {
        this.connections = this.connections.filter(item => item.owner !== owner);
    }
    emit(signal) {
        for (const item of [...this.connections]) {
            if (item.signal === signal)
                item.callback(this);
        }
    }
}

class Actor extends Signals {
    constructor(name = '', style = '', children = []) {
        super();
        Object.assign(this, {name, style, children});
    }
    get_children() { return this.children; }
    get_name() { return this.name; }
    has_style_class_name(name) { return this.style === name; }
    get_effect() { return this.effect; }
}

class Blur {
    radius = 90;
    brightness = 0.65;
    set(values) { Object.assign(this, values); }
}

function fixture({locked = true, highContrast = false} = {}) {
    const queue = new Map();
    let sequence = 0;
    const GLib = {
        PRIORITY_DEFAULT: 0,
        idle_add_once(_priority, callback) {
            queue.set(++sequence, callback);
            return sequence;
        },
        source_remove(id) { queue.delete(id); },
    };
    const effect = new Blur();
    const background = new Actor('', 'screen-shield-background');
    background.effect = effect;
    const backgroundGroup = new Actor('', '', [background]);
    const dialog = new Actor('', 'unlock-dialog');
    dialog._backgroundGroup = backgroundGroup;
    const lockGroup = new Actor('lockDialogGroup', '', [dialog]);
    const shield = new Actor('', '', [lockGroup]);
    const settings = Object.assign(new Signals(), {high_contrast: highContrast});
    const theme = Object.assign(new Signals(), {scale_factor: 1});
    const Main = {
        sessionMode: Object.assign(new Signals(), {isLocked: locked}),
        layoutManager: Object.assign(new Signals(), {screenShieldGroup: shield}),
    };
    const ExtensionClass = vm.runInNewContext(`${source}\nZeusLockBackgroundExtension`, {
        GLib, Shell: {BlurEffect: Blur},
        St: {Settings: {get: () => settings}, ThemeContext: {get_for_stage: () => theme}},
        Main, Extension: class {}, global: {stage: {}},
    });
    const extension = new ExtensionClass();
    function flush() {
        const callbacks = [...queue.values()];
        queue.clear();
        for (const callback of callbacks)
            callback();
        assert.equal(queue.size, 0, 'a sync must not start a recurring idle loop');
    }
    return {extension, effect, background, backgroundGroup, dialog, lockGroup,
        shield, settings, theme, Main, queue, flush};
}

test('unlocked desktop stays stock and does not poll', () => {
    const f = fixture({locked: false});
    f.extension.enable();
    f.flush();
    assert.equal(f.effect.radius, 90);
    assert.equal(f.effect.brightness, 0.65);
    f.Main.sessionMode.isLocked = true;
    f.Main.sessionMode.emit('updated');
    f.flush();
    assert.equal(f.effect.radius, 8);
    assert.equal(f.effect.brightness, 0.8);
});

test('disable cancels pending work, restores effects and disconnects signals', () => {
    const f = fixture();
    f.extension.enable();
    assert.equal(f.effect.radius, 8);
    f.extension.disable();
    assert.equal(f.queue.size, 0);
    assert.equal(f.effect.radius, 90);
    assert.equal(f.effect.brightness, 0.65);
    for (const emitter of [f.settings, f.theme, f.Main.sessionMode,
        f.Main.layoutManager, f.shield, f.lockGroup, f.dialog,
        f.backgroundGroup, f.background])
        assert.equal(emitter.connections.length, 0);
});

test('unlock and repeated locking restore and reapply native values', () => {
    const f = fixture();
    f.extension.enable();
    for (let i = 0; i < 3; i++) {
        f.Main.sessionMode.isLocked = false;
        f.Main.sessionMode.emit('updated');
        f.flush();
        assert.equal(f.effect.radius, 90);
        assert.equal(f.effect.brightness, 0.65);
        f.Main.sessionMode.isLocked = true;
        f.Main.sessionMode.emit('updated');
        f.flush();
        assert.equal(f.effect.radius, 8);
    }
});

test('high contrast skips adjustment and restores an already active effect', () => {
    const f = fixture({highContrast: true});
    f.extension.enable();
    f.flush();
    assert.equal(f.effect.radius, 90);
    f.settings.high_contrast = false;
    f.settings.emit('notify::high-contrast');
    f.flush();
    assert.equal(f.effect.radius, 8);
    f.settings.high_contrast = true;
    f.settings.emit('notify::high-contrast');
    f.flush();
    assert.equal(f.effect.radius, 90);
    assert.equal(f.effect.brightness, 0.65);
});

test('scale changes preserve independently updated native properties', () => {
    const f = fixture();
    f.extension.enable();
    f.flush();
    f.theme.scale_factor = 2;
    f.effect.radius = 180; // Native changed radius; our brightness remains.
    f.theme.emit('notify::scale-factor');
    f.flush();
    assert.equal(f.effect.radius, 16);
    f.extension.disable();
    assert.equal(f.effect.radius, 180);
    assert.equal(f.effect.brightness, 0.65);
});

test('disable never overwrites newer native values before a pending sync', () => {
    const f = fixture();
    f.extension.enable();
    f.effect.radius = 180;
    f.theme.emit('notify::scale-factor');
    f.extension.disable();
    assert.equal(f.effect.radius, 180);
    assert.equal(f.effect.brightness, 0.65);
});

test('missing or changed native internals leave the stock effect available', () => {
    const f = fixture();
    delete f.dialog._backgroundGroup;
    f.extension.enable();
    f.flush();
    assert.equal(f.effect.radius, 90);
    f.extension.disable();
    const changed = fixture();
    changed.background.effect = {radius: 90, brightness: 0.65};
    changed.extension.enable();
    changed.flush();
    assert.equal(changed.background.effect.radius, 90);
    changed.extension.disable();
});

test('destroyed backgrounds release effects and allow monitor replacement', () => {
    const f = fixture();
    f.extension.enable();
    f.backgroundGroup.children = [];
    f.background.emit('destroy');
    f.flush();
    assert.equal(f.effect.radius, 90);
    assert.equal(f.extension._effects.size, 0);
    f.backgroundGroup.children = [f.background];
    f.backgroundGroup.emit('child-added');
    f.flush();
    assert.equal(f.effect.radius, 8);
});

test('partial property failure restores the property already changed', () => {
    const f = fixture();
    let fail = true;
    f.effect.set = function(values) {
        if (fail && values.radius === 8) {
            fail = false;
            this.radius = values.radius;
            throw new Error('simulated native effect update failure');
        }
        Object.assign(this, values);
    };
    f.extension.enable();
    assert.equal(f.effect.radius, 90);
    assert.equal(f.effect.brightness, 0.65);
    f.extension.disable();
});
