# Codex CLI

Zeus includes OpenAI's standalone **Codex CLI 0.154.0**. In Terminal, enter a
project directory and run:

```sh
codex --version
codex
```

The first interactive launch offers sign-in. Use your own ChatGPT account or
another method supported by Codex; Zeus does not supply an account, API key,
subscription or usage credits. Internet access is needed for sign-in and hosted
model work. Version, help and the local sandbox command work offline.

Running `codex` this way executes tools **on this computer**, in the selected
working directory. The planned remote development launcher remains separate:
it must clearly identify its remote host and never fall back to local execution.
This addition does not complete the T3 client/server or remote-session roadmap.

## Packaging and updates

The full official Linux musl package keeps its original helper layout under
`/usr/lib/codex`; `/usr/bin/codex` points to its executable. It includes the
matching execution host, sandbox helper, ripgrep and shell resources. No Node.js
or npm runtime is added for Codex, and no Codex startup service is installed.

`image/codex.lock.json` pins the release URL, archive size and SHA-256. Assembly
checks those values before extracting an exact set of regular files. The image
retains the lock and installed-file receipt, and its build gate verifies the
installed package again. The archive checksum is matched against GitHub's
official release metadata; this is not a claim of an upstream signature check.

Codex updates arrive through **Zeus Updates**. The system default in
`/etc/codex/config.toml` disables startup update checks. Personal configuration
has higher precedence; an existing `~/.codex` directory is never replaced or
seeded from the build machine. The image contains no personal authentication,
history or sessions. Upstream sandbox and approval defaults remain in place.

## Qualification

The [Codex preview iteration](../iterations/git-f080c2d9bc53/README.md) records
the installed package, native screen, preservation checks and measured costs.

Source checks exercise successful installation, corrupt downloads, unsafe
archive entries and an existing destination. Runtime qualification checks the
exact executable and helpers, offline help, a writable sandbox workspace with
outside writes and network access denied, and the native first-launch screen.
Signing in and making a paid model request are owner actions, so an unauthenticated
preview test does not claim a completed model conversation.

The deployment receipt and timestamped measurements record VM evidence, image
storage cost and owner-file preservation. Physical laptop power use remains a
separate measurement.

## Upstream documentation

- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
- [Authentication](https://learn.chatgpt.com/docs/auth)
- [Pinned release](https://github.com/openai/codex/releases/tag/rust-v0.154.0)
- [Configuration precedence at this release](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/config/src/loader/README.md)
