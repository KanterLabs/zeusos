# Standalone Codex notices

These unmodified notices accompany the official Codex 0.154.0 Linux package.
`sources.json` records the exact source URLs and SHA-256 values of the texts.
Installed copies are under `/usr/share/licenses/zeus-codex/`.

| Component | Pinned source | Notices |
| --- | --- | --- |
| Codex 0.154.0 | `openai/codex` tag `rust-v0.154.0`, source `6b9826e3aa83b1a5947db50f4332cb9c65f1b340` | `LICENSE`, `NOTICE` |
| ripgrep 15.2.0 | `BurntSushi/ripgrep`, commit `e89fff89ac9af12e8d4ce9d5fd07beb408ca730f` | `ripgrep-COPYING`, `ripgrep-LICENSE-MIT`, `ripgrep-UNLICENSE` |
| PCRE2 10.45, in ripgrep | `PCRE2Project/pcre2`, tag `pcre2-10.45` | `PCRE2-LICENCE` |
| bubblewrap 0.11.2, Codex build | `codex-rs/vendor/bubblewrap` in the pinned Codex source | `bubblewrap-COPYING` |
| zsh 5.9.0.3-test, Codex wrapper patch | zsh source `77045ef899e53b9598bebc5a41db93a548a40ca6`; `codex-rs/shell-escalation/patches/zsh-exec-wrapper.patch` in the pinned Codex source | `zsh-LICENCE` |

Codex's package manifests (`scripts/codex_package/rg` and
`scripts/codex_package/codex-zsh`) and release workflows identify its helper
inputs. Their exact shipped bytes are separately pinned in
[`image/codex.lock.json`](../../codex.lock.json). Zeus preserves those binaries
and their layout. These upstream components retain their own licensing; the
Zeus MIT license does not replace it.
