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
