# Google Chrome package trust

`google-linux.asc` was downloaded from Google's [Linux signing-key endpoint](https://dl.google.com/linux/linux_signing_key.pub)
on 2026-09-09. Its primary fingerprint is
`EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796`, matching Google's
[published repository documentation](https://www.google.com/linuxrepositories/).
The certificate includes rotated signing subkeys, including expired historical
subkeys; signature verification must use a currently valid subkey.

SHA-256: `54dea5f6c2a26091578cf52a999cebc6b64df478d37ad4dce96376b711e3b27c`.
Google repository metadata and the initial Chrome 153.0.8010.36 RPM both verified
with live subkey `0E225917414670F4442C250DFD533C07C264648F` during integration.

DNF requires signed packages **and** repository metadata, HTTPS verification,
and a repository restricted to `google-chrome-stable`. It enables this repository
only during an image build. Review future key changes against Google's published
fingerprint and update the trust test; never work around a verification failure
by disabling checks. Browser RPMs keep Google's license, branding and sandbox.

## Tailscale

`tailscale.asc` was downloaded from Tailscale's official Fedora repository on
2026-09-12. Its primary fingerprint is
`2596A99EAAB33821893C0A79458CA832957F5868` and its SHA-256 is
`53c6f7dfbd774839d9f37e6c5022ba952108aba9a0e556a56f292a9eb605d7cf`.

The disabled build repository requires signed metadata and RPMs over HTTPS and
is restricted to the `tailscale` package. Installed clients receive Tailscale
updates only through signed Zeus OS images; no vendor self-updater is enabled.
