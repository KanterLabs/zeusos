# Google Chrome

Zeus selects Google's official **Chrome Stable** RPM as its browser, replacing
Firefox without removing any existing Firefox profiles, bookmarks or downloads.
This continues **0.1.0-preview.2**; each built image has its own Git build ID.
Installed qualification is recorded with that iteration's evidence.

## Behavior

- Chrome appears in the dock and application search. HTTP, HTTPS, HTML and XHTML
  open in Chrome. PDF and other unrelated application associations are unchanged.
- New downloads default to `~/Temp`; its existing cleanup schedule and **Keep…**
  action apply. Recommended Chrome preferences allow the owner to choose a
  different download folder. Existing explicit destinations stay unchanged.
- Background apps default off, so closing the browser can release its processes.
  Chrome is not started at login. This default remains editable in Chrome.
- A short, one-time login migration replaces the old Firefox dock entry while
  preserving other pinned apps. Firefox MIME overrides receive Chrome first and
  Firefox as a fallback for a retained OS. Later owner changes are not reset.
- Chrome updates arrive with **Zeus Updates**. The RPM's separate repository
  maintenance cron job is omitted. No guest package-manager timer is added.
  New browser security fixes require building and publishing a fresh Zeus image;
  a reliable servicing cadence remains a laptop-readiness requirement.

## Trust and preservation

The image build uses Google's HTTPS repository, with both package and metadata
signature checks required, and the reviewed [Google key](../../image/keys/README.md).
The repository is restricted to Chrome Stable and disabled on the installed OS.
The package's native sandbox, executables, branding and license remain in place.
There is no `--no-sandbox`, first-login browser download or runtime binary patch.

The browser migration does not read or rewrite browser databases. Old Firefox
data remains available for an explicit import or rollback; no automatic profile
conversion is attempted. Populated fixture tests cover retry behavior, unrelated
preferences and unsafe configuration paths. Deployment requires a verified
backup and preserved-file comparison.

The previous OS still contains Firefox and can read its original data. Chrome
profile data stays on `/var/home` through OS rollback. Chrome-specific features
are available again after returning to a Chrome-containing image. A native
rollback qualification record must distinguish this from restoring user data.

## Qualification

The deployed [September 9 Chrome iteration](../iterations/git-9c2cfbdcb703/README.md)
records the installed package, native defaults, policies, sandbox and Temp checks.
It also records a VM-specific login-keyring repair after the earlier account
password change; the original keyring was preserved. This is not an automatic
keyring reset or a migration policy for populated laptops.

- Verify image package inventory: official Chrome present, Firefox absent.
- Confirm desktop launch and default HTTP/HTTPS/HTML handlers in a native session.
- Inspect effective Chrome policies and verify a real download lands in Temp.
- Use a disposable Temp fixture for active-download cleanup, completion and Keep
  tests; never run deletion tests against the populated owner Temp folder.
- Verify Chrome exits after its last window closes, then measure closed-desktop
  idle separately from browser-open memory/CPU. Record native boot samples and
  explain workload differences; VM numbers do not measure laptop battery life.
- Exercise signed installation, retained-OS compatibility and populated-data
  preservation. Keep the same product version and publish dated evidence.

The recommended-policy behavior follows Chromium's
[Linux policy setup](https://www.chromium.org/administrators/linux-quick-start/)
and [preference precedence](https://www.chromium.org/administrators/configuring-other-preferences/).
Google documents its [RPM signing keys](https://www.google.com/linuxrepositories/)
and [Linux installation support](https://support.google.com/chrome/answer/95346).
