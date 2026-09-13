/*
 * Codex Usage Indicator for Zeus OS.
 *
 * This file is adapted from radoslavdodek/codex-usage-gnome-shell-ext at
 * commit 22baeaef5bc4642851148c99b505cb6469679199 (AGPL-3.0).  Zeus keeps
 * this source as a separate AGPL-covered extension; see NOTICE and the
 * corresponding image/licenses/codex-usage provenance record.
 *
 * Local changes remove the upstream optional controls, alternate provider, and
 * CLI terminal-output auth probe.  The provider below speaks only the
 * official Codex app-server JSON-RPC interface over a fixed local stdio
 * subprocess.  It never receives credentials from this extension.
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const CODEX_COMMAND = '/usr/bin/codex';
const CODEX_APP_SERVER_ARGV = Object.freeze([
    CODEX_COMMAND,
    'app-server',
    '--listen',
    'stdio://',
]);
const CLIENT_INFO = Object.freeze({
    name: 'zeus-codex-usage',
    title: 'Zeus Codex Usage Indicator',
    version: '1.0.0',
});

// This interval is deliberately fixed.  There is no preferences or pause
// control: enabling the extension performs one refresh, then repeats every
// exactly five minutes.
export const REFRESH_INTERVAL_SECONDS = 300;
export const PROVIDER_TIMEOUT_SECONDS = 15;
const LOGIN_TIMEOUT_SECONDS = 900;

// The app-server is a line-delimited JSON-RPC stream.  Validate before JSON
// parsing so an unexpected child process cannot make the Shell allocate an
// unbounded line or payload.
export const MAX_JSON_LINE_BYTES = 64 * 1024;
export const MAX_JSON_PAYLOAD_BYTES = 256 * 1024;
const MAX_ERROR_CHARS = 240;

const INDICATOR_ID = 'codex-usage-indicator';
const PANEL_STATUS_CLASSES = Object.freeze([
    'codex-usage-panel-loading',
    'codex-usage-panel-refreshing',
    'codex-usage-panel-normal',
    'codex-usage-panel-warning',
    'codex-usage-panel-limit',
    'codex-usage-panel-signed-out',
    'codex-usage-panel-offline',
    'codex-usage-panel-stale',
    'codex-usage-panel-error',
]);

const STATES = Object.freeze({
    LOADING: 'loading',
    REFRESHING: 'refreshing',
    NORMAL: 'normal',
    WARNING: 'warning',
    LIMIT: 'limit',
    SIGNED_OUT: 'signed-out',
    OFFLINE: 'offline',
    STALE: 'stale',
    ERROR: 'error',
});

const BUCKETS = Object.freeze({
    FIVE_HOUR: 'five-hour',
    WEEKLY: 'weekly',
});

class ProviderError extends Error {
    constructor(kind, message, options = {}) {
        super(message);
        this.name = 'ProviderError';
        this.kind = kind;
        this.cause = options.cause ?? null;
    }
}

function utf8ByteLength(value) {
    try {
        return new TextEncoder().encode(String(value)).byteLength;
    } catch (_error) {
        // A conservative fallback is preferable to accepting an unbounded
        // value if an older JavaScript runtime lacks TextEncoder.
        return String(value).length * 4;
    }
}

function boundedText(value, fallback = '') {
    const text = String(value ?? '').trim();
    if (!text)
        return fallback;
    return text.length > MAX_ERROR_CHARS ? `${text.slice(0, MAX_ERROR_CHARS)}…` : text;
}

// Error text can contain protocol diagnostics.  Keep only a short,
// credential-safe explanation for the menu; never show a raw provider line.
export function redactError(value, fallback = 'Codex provider unavailable.') {
    if (!value)
        return fallback;

    let text = typeof value === 'string' ? value : (value.message ?? String(value));
    text = text.replace(/\b(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+/gi, '$1: [redacted]');
    text = text.replace(/\b(bearer)\s+[A-Za-z0-9._~+\/-]{8,}/gi, '$1 [redacted]');
    text = text.replace(/\b(api[ _-]?key|secret|password)\s*[:=]?\s*[^\s,;}]+/gi, '$1: [redacted]');
    text = text.replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[redacted]');
    text = text.replace(/\b[A-Za-z0-9+/=_-]{40,}\b/g, '[redacted]');
    return boundedText(text, fallback) || fallback;
}

function removeSource(sourceId) {
    if (!sourceId)
        return;
    try {
        GLib.Source.remove(sourceId);
    } catch (_error) {
        // The source may already have fired while disable() was unwinding.
    }
}

function readLineAsync(stream, cancellable) {
    return new Promise((resolve, reject) => {
        stream.read_line_async(GLib.PRIORITY_DEFAULT, cancellable, (_stream, result) => {
            try {
                const [line] = stream.read_line_finish_utf8(result);
                resolve(line);
            } catch (error) {
                reject(error);
            }
        });
    });
}

function isObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function classifyMessage(message) {
    const text = String(message ?? '').toLowerCase();
    if (text.includes('api key') || text.includes('apikey') || text.includes('api-key'))
        return 'api-key';
    if (text.includes('unauthenticated') || text.includes('not authenticated') ||
        text.includes('not logged in') || text.includes('login') || text.includes('sign in'))
        return 'signed-out';
    if (text.includes('offline') || text.includes('network') || text.includes('connect') ||
        text.includes('not found') || text.includes('no such file') || text.includes('spawn'))
        return 'offline';
    if (text.includes('timed out') || text.includes('timeout'))
        return 'offline';
    return 'error';
}

export function isApiKeyAuth(value) {
    const mode = String(value ?? '').toLowerCase().replace(/[-_\s]/g, '');
    return mode === 'apikey' || mode === 'openaiapikey';
}

function assertChatGptAuth(value) {
    if (!isObject(value))
        return;

    const possibleModes = [
        value.authMode,
        value.auth_mode,
        value.account?.authMode,
        value.account?.auth_mode,
        value.rateLimits?.authMode,
        value.rateLimits?.auth_mode,
    ];
    if (possibleModes.some(mode => isApiKeyAuth(mode))) {
        throw new ProviderError(
            'api-key',
            'API-key authentication is not supported by the Codex usage indicator.',
        );
    }

    // A login response with this type is never accepted by this feature.  The
    // only login request sent below is the official ChatGPT device-code flow.
    if (value.type === 'apiKey')
        throw new ProviderError('api-key', 'API-key authentication is not supported by the Codex usage indicator.');
}

function providerErrorFromMessage(message, fallback = 'Codex app-server request failed.') {
    const safeMessage = redactError(message, fallback);
    return new ProviderError(classifyMessage(safeMessage), safeMessage);
}

function parseJsonMessage(line) {
    if (utf8ByteLength(line) > MAX_JSON_LINE_BYTES)
        throw new ProviderError('malformed', 'Codex app-server response line exceeded the safety limit.');
    if (utf8ByteLength(line) > MAX_JSON_PAYLOAD_BYTES)
        throw new ProviderError('malformed', 'Codex app-server response exceeded the safety limit.');

    let message;
    try {
        message = JSON.parse(line);
    } catch (_error) {
        throw new ProviderError('malformed', 'Codex app-server returned malformed JSON.');
    }
    if (!isObject(message))
        throw new ProviderError('malformed', 'Codex app-server returned an invalid JSON-RPC message.');
    return message;
}

function serializeJsonMessage(message) {
    let text;
    try {
        text = `${JSON.stringify(message)}\n`;
    } catch (error) {
        throw new ProviderError('malformed', redactError(error, 'Unable to encode app-server request.'), {cause: error});
    }
    if (utf8ByteLength(text) > MAX_JSON_PAYLOAD_BYTES)
        throw new ProviderError('malformed', 'Codex app-server request exceeded the safety limit.');
    return text;
}

function isCancellation(cancellable, error) {
    if (cancellable?.is_cancelled?.())
        return true;
    return String(error?.message ?? '').toLowerCase().includes('cancel');
}

function normalizeRateLimitError(result) {
    if (result?.error?.message)
        return providerErrorFromMessage(result.error.message);
    return providerErrorFromMessage('Codex app-server did not return rate limit data.');
}

/**
 * One app-server transport.  It owns at most one child process and one reader
 * loop.  Every request is bounded by a timeout, and stopping the transport
 * rejects all pending requests before the child can be replaced.
 */
class CodexAppServerClient {
    constructor(notificationHandler = null) {
        this._notificationHandler = notificationHandler;
        this._process = null;
        this._stdin = null;
        this._stdout = null;
        this._cancellable = null;
        this._readerGeneration = 0;
        this._initialized = false;
        this._initializationPromise = null;
        this._pending = new Map();
        this._nextRequestId = 1;
        this._closed = false;
    }

    _spawn() {
        if (this._closed)
            throw new ProviderError('offline', 'Codex usage provider is stopped.');
        if (this._process)
            return;

        let process;
        try {
            const launcher = new Gio.SubprocessLauncher({
                flags: Gio.SubprocessFlags.STDIN_PIPE |
                    Gio.SubprocessFlags.STDOUT_PIPE |
                    Gio.SubprocessFlags.STDERR_SILENCE,
            });
            process = launcher.spawnv(CODEX_APP_SERVER_ARGV);
        } catch (error) {
            throw new ProviderError(
                'offline',
                redactError(error, 'The fixed Codex app-server command is unavailable.'),
                {cause: error},
            );
        }

        this._process = process;
        this._stdin = process.get_stdin_pipe();
        this._stdout = new Gio.DataInputStream({base_stream: process.get_stdout_pipe()});
        this._cancellable = new Gio.Cancellable();
        this._initialized = false;
        const generation = ++this._readerGeneration;

        // wait_check is only a lifecycle observer.  Protocol data always comes
        // from stdout; no CLI terminal output is parsed.
        process.wait_check_async(null, (_process, result) => {
            try {
                process.wait_check_finish(result);
            } catch (_error) {
                // The provider will report the bounded transport failure below.
            }
            if (generation === this._readerGeneration && this._process === process)
                this._stopProcess(new ProviderError('offline', 'Codex app-server exited.'));
        });

        this._readLoop(process, generation);
    }

    async _readLoop(process, generation) {
        while (generation === this._readerGeneration && this._process === process) {
            let line;
            try {
                line = await readLineAsync(this._stdout, this._cancellable);
            } catch (error) {
                if (generation !== this._readerGeneration || isCancellation(this._cancellable, error))
                    return;
                this._stopProcess(providerErrorFromMessage(error, 'Codex app-server could not be read.'));
                return;
            }

            if (line === null) {
                if (generation === this._readerGeneration)
                    this._stopProcess(new ProviderError('offline', 'Codex app-server closed its output.'));
                return;
            }

            const trimmed = String(line).trim();
            if (!trimmed)
                continue;
            if (utf8ByteLength(trimmed) > MAX_JSON_LINE_BYTES) {
                this._stopProcess(new ProviderError('malformed', 'Codex app-server response line exceeded the safety limit.'));
                return;
            }

            // App-server stdio is JSON-RPC.  Ignore non-protocol diagnostics,
            // but never interpret them as CLI auth or usage output.
            if (!trimmed.startsWith('{'))
                continue;

            let message;
            try {
                message = parseJsonMessage(trimmed);
            } catch (error) {
                this._stopProcess(error instanceof ProviderError ? error : providerErrorFromMessage(error));
                return;
            }
            this._dispatch(message);
        }
    }

    _dispatch(message) {
        if ('id' in message) {
            const key = String(message.id);
            const pending = this._pending.get(key);
            if (pending) {
                this._pending.delete(key);
                removeSource(pending.timeoutId);
                pending.timeoutId = 0;
                if (message.error) {
                    pending.reject(normalizeRateLimitError(message));
                    return;
                }
                pending.resolve(message.result ?? {});
                return;
            }
        }

        if (typeof message.method === 'string') {
            try {
                this._notificationHandler?.(message);
            } catch (_error) {
                // A UI callback must not stop the protocol reader.
            }
        }
    }

    _send(message) {
        if (!this._process || !this._stdin)
            throw new ProviderError('offline', 'Codex app-server is not running.');

        const text = serializeJsonMessage(message);
        try {
            const bytes = new TextEncoder().encode(text);
            this._stdin.write_all(bytes, this._cancellable);
        } catch (error) {
            throw providerErrorFromMessage(error, 'Unable to send the Codex app-server request.');
        }
    }

    _requestWithoutInitialization(method, params, timeoutSeconds) {
        this._spawn();
        const id = this._nextRequestId++;
        const message = {id, method};
        if (params !== undefined)
            message.params = params;

        return new Promise((resolve, reject) => {
            const timeoutId = GLib.timeout_add_seconds(
                GLib.PRIORITY_DEFAULT,
                timeoutSeconds,
                () => {
                    if (!this._pending.has(String(id)))
                        return GLib.SOURCE_REMOVE;
                    this._pending.delete(String(id));
                    reject(new ProviderError('timeout', 'Codex app-server request timed out.'));
                    this._stopProcess(new ProviderError('timeout', 'Codex app-server request timed out.'));
                    return GLib.SOURCE_REMOVE;
                },
            );
            this._pending.set(String(id), {resolve, reject, timeoutId});
            try {
                this._send(message);
            } catch (error) {
                this._pending.delete(String(id));
                removeSource(timeoutId);
                reject(error);
            }
        });
    }

    async _ensureInitialized() {
        if (this._initialized)
            return;
        if (this._initializationPromise)
            return this._initializationPromise;

        const initializationPromise = (async () => {
            const result = await this._requestWithoutInitialization('initialize', {
                clientInfo: CLIENT_INFO,
                capabilities: {
                    experimentalApi: true,
                    optOutNotificationMethods: [],
                },
            }, PROVIDER_TIMEOUT_SECONDS);
            assertChatGptAuth(result);
            this._send({method: 'initialized'});
            this._initialized = true;
        })();
        this._initializationPromise = initializationPromise;

        try {
            await initializationPromise;
        } catch (error) {
            this._initialized = false;
            // A rejected handshake must not poison all later refreshes.  Stop
            // the failed child and clear the promise so the next request can
            // create a fresh bounded app-server lifecycle.
            if (this._initializationPromise === initializationPromise)
                this._initializationPromise = null;
            if (this._process)
                this._stopProcess(error instanceof ProviderError ? error : providerErrorFromMessage(error));
            throw error instanceof ProviderError ? error : providerErrorFromMessage(error);
        }
    }

    async request(method, params = undefined, timeoutSeconds = PROVIDER_TIMEOUT_SECONDS) {
        await this._ensureInitialized();
        const result = await this._requestWithoutInitialization(method, params, timeoutSeconds);
        assertChatGptAuth(result);
        return result;
    }

    _stopProcess(reason = null) {
        const process = this._process;
        const cancellable = this._cancellable;
        this._readerGeneration++;
        this._process = null;
        this._stdin = null;
        this._stdout = null;
        this._cancellable = null;
        this._initialized = false;
        this._initializationPromise = null;

        const safeReason = reason instanceof ProviderError ? reason :
            providerErrorFromMessage(reason, 'Codex app-server stopped.');
        for (const pending of this._pending.values()) {
            removeSource(pending.timeoutId);
            pending.timeoutId = 0;
            pending.reject(safeReason);
        }
        this._pending.clear();

        try {
            cancellable?.cancel();
        } catch (_error) {
        }
        try {
            process?.force_exit();
        } catch (_error) {
        }
        try {
            process?.get_stdin_pipe()?.close(null);
        } catch (_error) {
        }
    }

    stop() {
        this._stopProcess(new ProviderError('canceled', 'Codex app-server stopped.'));
    }

    destroy() {
        this._closed = true;
        this._stopProcess(new ProviderError('canceled', 'Codex app-server destroyed.'));
        this._notificationHandler = null;
    }
}

function normalizeUnixSeconds(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0)
        return null;
    return number > 100000000000 ? Math.floor(number / 1000) : Math.floor(number);
}

function bucketFromWindow(window, key, label, durationMinutes) {
    if (!isObject(window)) {
        return {
            key,
            label,
            percentRemaining: null,
            resetAt: null,
            available: false,
        };
    }

    const usedPercent = Number(window.usedPercent);
    if (!Number.isFinite(usedPercent)) {
        return {
            key,
            label,
            percentRemaining: null,
            resetAt: null,
            available: false,
        };
    }

    // The protocol uses exact minute durations.  A malformed duration should
    // not be silently presented as a different quota bucket.
    if (window.windowDurationMins !== undefined && Number(window.windowDurationMins) !== durationMinutes) {
        return {
            key,
            label,
            percentRemaining: null,
            resetAt: null,
            available: false,
        };
    }

    return {
        key,
        label,
        percentRemaining: Math.max(0, Math.min(100, Math.round(100 - usedPercent))),
        resetAt: normalizeUnixSeconds(window.resetsAt ?? window.resetAt),
        available: true,
    };
}

function rateLimitsRoot(result) {
    if (!isObject(result))
        return null;
    const root = isObject(result.GetAccountRateLimitsResponse) ? result.GetAccountRateLimitsResponse : result;
    return root;
}

function rateLimitEntries(result) {
    const root = rateLimitsRoot(result);
    if (!root)
        return [];

    const entries = [];
    const seen = new Set();
    const add = entry => {
        if (!isObject(entry) || seen.has(entry))
            return;
        seen.add(entry);
        entries.push(entry);
    };

    // Prefer the documented `codex` entry, then inspect every returned limit
    // entry.  Some Codex releases put the 5-hour window under another opaque
    // limit ID while the codex entry carries only the weekly window.
    const byId = root.rateLimitsByLimitId;
    if (isObject(byId)) {
        add(byId.codex);
        for (const entry of Object.values(byId))
            add(entry);
    }
    if (Array.isArray(root.rateLimits)) {
        for (const entry of root.rateLimits)
            add(entry);
    }
    if (isObject(root.rateLimits))
        add(root.rateLimits);
    if (root.limitId === 'codex')
        add(root);
    return entries;
}

function mostConstrainedWindow(entries, key, label, durationMinutes) {
    let selected = null;
    for (const entry of entries) {
        for (const window of [entry.primary, entry.secondary]) {
            if (!isObject(window) || Number(window.windowDurationMins) !== durationMinutes)
                continue;
            const candidate = bucketFromWindow(window, key, label, durationMinutes);
            if (!candidate.available)
                continue;
            if (!selected || candidate.percentRemaining < selected.percentRemaining)
                selected = candidate;
        }
    }
    return selected ?? bucketFromWindow(null, key, label, durationMinutes);
}

export function normalizeRateLimits(result, nowSeconds = Math.floor(Date.now() / 1000)) {
    assertChatGptAuth(result);
    const entries = rateLimitEntries(result);
    if (!entries.length)
        throw new ProviderError('malformed', 'Codex app-server did not return rate limit data.');

    for (const entry of entries)
        assertChatGptAuth(entry);
    const fiveHour = mostConstrainedWindow(entries, BUCKETS.FIVE_HOUR, '5-hour usage', 300);
    const weekly = mostConstrainedWindow(entries, BUCKETS.WEEKLY, 'Weekly usage', 10080);
    const available = [fiveHour, weekly].filter(bucket => bucket.available);
    if (!available.length)
        throw new ProviderError('malformed', 'Codex app-server returned no supported usage windows.');

    let status = STATES.NORMAL;
    if (available.some(bucket => bucket.percentRemaining <= 0))
        status = STATES.LIMIT;
    else if (available.some(bucket => bucket.percentRemaining <= 25))
        status = STATES.WARNING;

    return {
        status,
        fiveHour,
        weekly,
        updatedAt: normalizeUnixSeconds(nowSeconds),
    };
}

export function isSafeVerificationUrl(value) {
    if (typeof value !== 'string' || !value.trim())
        return false;
    try {
        // GNOME Shell's GJS runtime does not provide the browser URL global.
        // GLib.Uri is available on every supported Shell version and rejects
        // malformed authority/path forms before the exact allowlist below.
        const uri = GLib.Uri.parse(value, GLib.UriFlags.NONE);
        return uri.get_scheme() === 'https' &&
            uri.get_host()?.toLowerCase() === 'auth.openai.com' &&
            uri.get_path() === '/codex/device' &&
            uri.get_port() === -1 &&
            !uri.get_query() && !uri.get_userinfo() && !uri.get_fragment();
    } catch (_error) {
        return false;
    }
}

function launchVerificationUrl(value) {
    if (!isSafeVerificationUrl(value))
        throw new ProviderError('malformed', 'Codex returned an unsafe sign-in URL.');
    try {
        Gio.AppInfo.launch_default_for_uri(value, null);
    } catch (error) {
        throw new ProviderError('offline', redactError(error, 'Unable to open the Codex sign-in page.'), {cause: error});
    }
}

class CodexUsageProvider {
    constructor(notificationHandler) {
        this._notificationHandler = notificationHandler;
        this._client = new CodexAppServerClient(message => this._onNotification(message));
        this._refreshPromise = null;
        this._login = null;
        this._loginStartPromise = null;
        this._loginTimerId = 0;
    }

    _onNotification(message) {
        const method = message?.method;
        const params = isObject(message?.params) ? message.params : {};

        if (method === 'account/updated' && isApiKeyAuth(params.authMode)) {
            this._notificationHandler?.({
                method: 'codex-usage/auth-rejected',
                params: {message: 'API-key authentication is not supported by the Codex usage indicator.'},
            });
            return;
        }

        if (method === 'account/login/completed') {
            const loginId = typeof params.loginId === 'string' ? params.loginId : null;
            if (!this._login || loginId !== this._login.loginId)
                return;
            const completed = {
                loginId,
                success: params.success === true,
                error: params.success === true ? null : redactError(params.error, 'Sign-in was not completed.'),
            };
            this._clearLogin();
            // The login lifecycle ends with this notification.  Stop the
            // child before notifying the UI; a successful UI callback may
            // immediately begin a new, independent usage read.
            this._client.stop();
            this._notificationHandler?.({method, params: completed});
            return;
        }

        if (method === 'account/rateLimits/updated') {
            this._notificationHandler?.({method, params});
        }
    }

    _scheduleLoginTimeout(loginId) {
        removeSource(this._loginTimerId);
        this._loginTimerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, LOGIN_TIMEOUT_SECONDS, () => {
            if (this._login?.loginId === loginId) {
                this._clearLogin();
                this._client.stop();
                this._notificationHandler?.({
                    method: 'codex-usage/login-timeout',
                    params: {message: 'The device-code sign-in attempt expired.'},
                });
            }
            this._loginTimerId = 0;
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearLogin() {
        removeSource(this._loginTimerId);
        this._loginTimerId = 0;
        this._login = null;
    }

    async readRateLimits() {
        if (this._refreshPromise)
            return this._refreshPromise;
        this._refreshPromise = (async () => {
            const result = await this._client.request('account/rateLimits/read');
            return normalizeRateLimits(result);
        })();
        try {
            return await this._refreshPromise;
        } finally {
            this._refreshPromise = null;
            // Do not leave an idle app-server resident between the fixed
            // five-minute reads.  A device-code login is the sole exception.
            if (!this._login)
                this._client.stop();
        }
    }

    async startDeviceCodeLogin() {
        if (this._login)
            return this._login;

        if (this._loginStartPromise)
            return this._loginStartPromise;

        const loginStartPromise = (async () => {
            const result = await this._client.request(
                'account/login/start',
                {type: 'chatgptDeviceCode'},
            );
            assertChatGptAuth(result);
            if (result.type !== 'chatgptDeviceCode' ||
                typeof result.loginId !== 'string' || !result.loginId ||
                typeof result.userCode !== 'string' || !result.userCode.trim() ||
                !isSafeVerificationUrl(result.verificationUrl)) {
                throw new ProviderError('malformed', 'Codex returned an invalid device-code sign-in response.');
            }

            this._login = {
                loginId: result.loginId,
                verificationUrl: result.verificationUrl,
                userCode: boundedText(result.userCode),
            };
            this._scheduleLoginTimeout(result.loginId);
            return this._login;
        })();
        this._loginStartPromise = loginStartPromise;
        try {
            return await loginStartPromise;
        } finally {
            if (this._loginStartPromise === loginStartPromise)
                this._loginStartPromise = null;
        }
    }

    get loginStarting() {
        return Boolean(this._loginStartPromise);
    }

    async cancelDeviceCodeLogin() {
        const login = this._login;
        if (!login)
            return;

        // Clear the UI state first so a late completion notification cannot
        // resurrect a canceled attempt.  The app-server cancel request still
        // uses the exact loginId required by the official protocol.
        this._clearLogin();
        this._notificationHandler?.({
            method: 'codex-usage/login-cancelled',
            params: {message: 'Device-code sign-in canceled.'},
        });
        try {
            await this._client.request('account/login/cancel', {loginId: login.loginId});
        } catch (_error) {
            // The child is stopped below; a canceled UI action does not expose
            // provider diagnostics or leave a second lifecycle running.
        } finally {
            this._client.stop();
        }
    }

    get login() {
        return this._login;
    }

    openLoginPage() {
        if (!this._login)
            throw new ProviderError('malformed', 'No Codex device-code sign-in is waiting.');
        launchVerificationUrl(this._login.verificationUrl);
    }

    destroy() {
        this._clearLogin();
        this._client.destroy();
        this._notificationHandler = null;
        this._refreshPromise = null;
        this._loginStartPromise = null;
    }
}

function formatPercent(value) {
    return value === null || value === undefined ? 'Unavailable' : `${value}% left`;
}

function formatReset(resetAt) {
    if (!resetAt)
        return 'Reset unavailable';
    const delta = resetAt - Math.floor(Date.now() / 1000);
    if (delta <= 0)
        return 'Reset due';
    const minutes = Math.floor(delta / 60);
    if (minutes < 60)
        return `Resets in ${Math.max(1, minutes)}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24)
        return `Resets in ${hours}h`;
    return `Resets in ${Math.floor(hours / 24)}d`;
}

function statusLabel(state) {
    switch (state) {
    case STATES.NORMAL:
        return 'Normal';
    case STATES.WARNING:
        return 'Low';
    case STATES.LIMIT:
        return 'Limit reached';
    case STATES.SIGNED_OUT:
        return 'Signed out';
    case STATES.OFFLINE:
        return 'Offline';
    case STATES.STALE:
        return 'Stale';
    case STATES.REFRESHING:
        return 'Refreshing';
    case STATES.LOADING:
        return 'Checking';
    default:
        return 'Unavailable';
    }
}

function panelText(state, snapshot) {
    if ([STATES.NORMAL, STATES.WARNING, STATES.LIMIT, STATES.STALE].includes(state)) {
        const bucket = snapshot?.fiveHour?.available ? snapshot.fiveHour : snapshot?.weekly;
        if (bucket?.available)
            return `Codex ${state === STATES.STALE ? '· Stale · ' : ''}${bucket.key === BUCKETS.FIVE_HOUR ? '5h' : 'Week'} ${bucket.percentRemaining}%`;
    }
    if (state === STATES.SIGNED_OUT)
        return 'Codex · Sign in';
    if (state === STATES.OFFLINE)
        return 'Codex · Offline';
    if (state === STATES.STALE)
        return 'Codex · Stale';
    if (state === STATES.LOADING)
        return 'Codex · Checking';
    if (state === STATES.REFRESHING)
        return 'Codex · Refreshing';
    return 'Codex · Unavailable';
}

const CodexUsageIndicator = GObject.registerClass(
class CodexUsageIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Codex Usage', false);
        this._extension = extension;
        this.add_style_class_name('codex-usage-panel');
        this._box = new St.BoxLayout({
            style_class: 'codex-usage-panel-box',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._label = new St.Label({
            text: 'Codex · Checking',
            style_class: 'codex-usage-panel-label',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._box.add_child(this._label);
        this.add_child(this._box);

        this._statusItem = new PopupMenu.PopupMenuItem('Checking Codex usage…', {
            reactive: false,
            can_focus: false,
        });
        this._fiveHourItem = new PopupMenu.PopupMenuItem('5-hour usage: Unavailable', {
            reactive: false,
            can_focus: false,
        });
        this._weeklyItem = new PopupMenu.PopupMenuItem('Weekly usage: Unavailable', {
            reactive: false,
            can_focus: false,
        });
        this._detailItem = new PopupMenu.PopupMenuItem('', {
            reactive: false,
            can_focus: false,
        });
        this._loginItem = new PopupMenu.PopupMenuItem('', {
            reactive: false,
            can_focus: false,
        });
        this._openLoginItem = new PopupMenu.PopupMenuItem('Open Sign-in Page');
        this._openLoginItem.connect('activate', () => this._extension.openSignInPage());
        this._cancelLoginItem = new PopupMenu.PopupMenuItem('Cancel Sign-in');
        this._cancelLoginItem.connect('activate', () => this._extension.cancelDeviceCodeLogin());
        this._refreshItem = new PopupMenu.PopupMenuItem('Refresh Usage');
        this._refreshItem.connect('activate', () => this._extension.manualRefresh());
        this._signInItem = new PopupMenu.PopupMenuItem('Sign in with ChatGPT device code');
        this._signInItem.connect('activate', () => this._extension.startDeviceCodeLogin());

        this.menu.addMenuItem(this._statusItem);
        this.menu.addMenuItem(this._fiveHourItem);
        this.menu.addMenuItem(this._weeklyItem);
        this.menu.addMenuItem(this._detailItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._loginItem);
        this.menu.addMenuItem(this._openLoginItem);
        this.menu.addMenuItem(this._cancelLoginItem);
        this.menu.addMenuItem(this._refreshItem);
        this.menu.addMenuItem(this._signInItem);
        this._loginItem.hide();
        this._openLoginItem.hide();
        this._cancelLoginItem.hide();
    }

    _setItemText(item, text) {
        if (item?.label)
            item.label.text = text;
    }

    update(state, snapshot, message, login, busy, loginStarting) {
        this._label.text = panelText(state, snapshot);
        for (const styleClass of PANEL_STATUS_CLASSES)
            this.remove_style_class_name(styleClass);
        this.add_style_class_name(`codex-usage-panel-${state}`);

        this._setItemText(this._statusItem, `Codex usage · ${statusLabel(state)}`);
        this._setItemText(this._fiveHourItem, `5-hour usage: ${formatPercent(snapshot?.fiveHour?.percentRemaining)} · ${formatReset(snapshot?.fiveHour?.resetAt)}`);
        this._setItemText(this._weeklyItem, `Weekly usage: ${formatPercent(snapshot?.weekly?.percentRemaining)} · ${formatReset(snapshot?.weekly?.resetAt)}`);
        this._setItemText(this._detailItem, message ?? 'No successful Codex usage refresh yet.');
        const operationBusy = busy || loginStarting;
        this._setItemText(this._refreshItem, busy ? 'Refreshing Usage…' : 'Refresh Usage');
        this._refreshItem.setSensitive(!operationBusy);

        const hasLogin = Boolean(login);
        this._loginItem.visible = hasLogin;
        this._openLoginItem.visible = hasLogin;
        this._cancelLoginItem.visible = hasLogin;
        if (hasLogin) {
            this._setItemText(this._loginItem, `Sign-in code: ${login.userCode} · ${login.verificationUrl}`);
            this._openLoginItem.setSensitive(isSafeVerificationUrl(login.verificationUrl));
        }
        this._signInItem.setSensitive(!operationBusy && !hasLogin);
    }
});

export default class CodexUsageExtension extends Extension {
    enable() {
        this._disabled = false;
        this._refreshTimerId = 0;
        this._refreshPromise = null;
        this._snapshot = null;
        this._state = STATES.LOADING;
        this._message = 'Checking Codex usage…';
        this._provider = new CodexUsageProvider(message => this._onProviderNotification(message));
        this._indicator = new CodexUsageIndicator(this);
        Main.panel.addToStatusArea(INDICATOR_ID, this._indicator, 0, 'right');
        this._render();

        this._refreshTimerId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT,
            REFRESH_INTERVAL_SECONDS,
            () => {
                if (this._disabled) {
                    this._refreshTimerId = 0;
                    return GLib.SOURCE_REMOVE;
                }
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            },
        );

        // Refresh immediately on enable; subsequent automatic refreshes use
        // only the fixed five-minute timer above.
        this._refresh();
    }

    disable() {
        this._disabled = true;
        removeSource(this._refreshTimerId);
        this._refreshTimerId = 0;

        this._provider?.destroy();
        this._provider = null;
        this._refreshPromise = null;

        this._indicator?.destroy();
        this._indicator = null;
        this._snapshot = null;
        this._state = STATES.OFFLINE;
        this._message = null;
    }

    _render() {
        this._indicator?.update(
            this._state,
            this._snapshot,
            this._message,
            this._provider?.login ?? null,
            Boolean(this._refreshPromise),
            Boolean(this._provider?.loginStarting),
        );
    }

    _setProviderFailure(error) {
        const safeMessage = redactError(error, 'Codex usage could not be refreshed.');
        const kind = error instanceof ProviderError ? error.kind : classifyMessage(safeMessage);
        if (kind === 'api-key' || safeMessage.toLowerCase().includes('api-key') || safeMessage.toLowerCase().includes('api key')) {
            this._state = STATES.SIGNED_OUT;
            this._message = 'API-key authentication is not supported; sign in with ChatGPT.';
        } else if (kind === 'signed-out') {
            this._state = STATES.SIGNED_OUT;
            this._message = 'Sign in to Codex with ChatGPT to see usage.';
        } else if (this._snapshot) {
            this._state = STATES.STALE;
            this._message = kind === 'offline' ?
                'Codex is offline; showing the last successful usage snapshot.' :
                'Codex usage is stale; showing the last successful snapshot.';
        } else if (kind === 'offline' || kind === 'timeout') {
            this._state = STATES.OFFLINE;
            this._message = 'Codex app-server is offline or did not respond.';
        } else {
            this._state = STATES.ERROR;
            this._message = safeMessage;
        }
    }

    _refresh() {
        if (this._disabled || !this._provider)
            return null;
        // The device-code ceremony owns the one app-server process until it
        // completes, is canceled, or expires. A timer tick during that brief
        // window is intentionally suppressed; completion triggers a fresh
        // read and the fixed five-minute cadence remains unchanged.
        if (this._provider.loginStarting || this._provider.login)
            return null;
        if (this._refreshPromise)
            return this._refreshPromise;

        if (!this._snapshot)
            this._state = STATES.LOADING;
        else if (this._state !== STATES.SIGNED_OUT)
            this._state = STATES.REFRESHING;
        this._message = 'Refreshing Codex usage…';
        this._render();

        this._refreshPromise = this._provider.readRateLimits()
            .then(snapshot => {
                if (this._disabled)
                    return;
                this._snapshot = snapshot;
                this._state = snapshot.status;
                this._message = `Last successful refresh ${new Date().toLocaleTimeString()}.`;
                this._render();
            })
            .catch(error => {
                if (!this._disabled) {
                    this._setProviderFailure(error);
                    this._render();
                }
            })
            .finally(() => {
                this._refreshPromise = null;
                if (!this._disabled)
                    this._render();
            });
        return this._refreshPromise;
    }

    manualRefresh() {
        this._refresh();
    }

    async startDeviceCodeLogin() {
        if (this._disabled || !this._provider || this._provider.login)
            return;
        this._message = 'Starting ChatGPT device-code sign-in…';
        this._render();
        try {
            await this._provider.startDeviceCodeLogin();
            this._state = STATES.SIGNED_OUT;
            this._message = 'Enter the displayed code, then open the sign-in page.';
            this._render();
        } catch (error) {
            if (!this._disabled) {
                this._setProviderFailure(error);
                this._render();
            }
        }
    }

    openSignInPage() {
        if (this._disabled || !this._provider)
            return;
        try {
            this._provider.openLoginPage();
            this._message = 'Sign-in page opened. Complete device-code authorization in your browser.';
        } catch (error) {
            this._setProviderFailure(error);
        }
        this._render();
    }

    async cancelDeviceCodeLogin() {
        if (this._disabled || !this._provider || !this._provider.login)
            return;
        try {
            await this._provider.cancelDeviceCodeLogin();
        } finally {
            if (!this._disabled) {
                this._state = STATES.SIGNED_OUT;
                this._message = 'Device-code sign-in canceled.';
                this._render();
            }
        }
    }

    _onProviderNotification(message) {
        if (this._disabled)
            return;

        if (message.method === 'codex-usage/auth-rejected') {
            this._state = STATES.SIGNED_OUT;
            this._message = 'API-key authentication is not supported; sign in with ChatGPT.';
            this._render();
            return;
        }

        if (message.method === 'codex-usage/login-timeout') {
            this._state = STATES.SIGNED_OUT;
            this._message = 'The device-code sign-in attempt expired.';
            this._render();
            return;
        }

        if (message.method === 'codex-usage/login-cancelled') {
            this._state = STATES.SIGNED_OUT;
            this._message = 'Device-code sign-in canceled.';
            this._render();
            return;
        }

        if (message.method === 'account/login/completed') {
            const params = message.params ?? {};
            if (params.success === true) {
                this._state = STATES.LOADING;
                this._message = 'ChatGPT sign-in completed; refreshing usage…';
                this._render();
                this._refresh();
            } else {
                this._state = STATES.SIGNED_OUT;
                this._message = 'Sign-in was not completed.';
                this._render();
            }
            return;
        }

        if (message.method === 'account/rateLimits/updated') {
            try {
                const snapshot = normalizeRateLimits(message.params);
                this._snapshot = snapshot;
                this._state = snapshot.status;
                this._message = 'Usage updated by Codex app-server.';
                this._render();
            } catch (_error) {
                // Sparse updates are advisory; the next fixed refresh reads a
                // complete snapshot and reports any actual provider failure.
            }
        }
    }
}
