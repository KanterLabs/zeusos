#!/usr/bin/env python3
"""Fail the image build if the native Chrome replacement is incomplete."""
import configparser
import json
from pathlib import Path
import subprocess


def command(*args):
    return subprocess.check_output(args, text=True, timeout=20).strip()


def main():
    package = command('rpm', '-q', '--qf', '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}', 'google-chrome-stable')
    version = command('/usr/bin/google-chrome-stable', '--version')
    assert version.startswith('Google Chrome '), 'The default browser must be official Google Chrome'
    assert subprocess.run(['rpm', '-q', 'firefox'], capture_output=True, timeout=10).returncode == 1, 'Firefox remains installed'
    assert Path('/opt/google/chrome/chrome').is_file(), 'Missing Chrome payload'
    sandbox = Path('/opt/google/chrome/chrome-sandbox').stat()
    assert sandbox.st_uid == 0 and sandbox.st_mode & 0o4000, 'Chrome sandbox permissions changed'
    assert not Path('/etc/cron.daily/google-chrome').exists(), 'Browser repo maintenance must not run on the immutable host'
    repo = configparser.ConfigParser()
    repo.read('/etc/yum.repos.d/google-chrome.repo')
    assert not repo.getboolean('google-chrome', 'enabled'), 'Chrome repo must be build-only'
    for field in ('gpgcheck', 'repo_gpgcheck', 'sslverify'):
        assert repo.getboolean('google-chrome', field), f'Chrome repository lost {field}'
    policy = json.loads(Path('/etc/opt/chrome/policies/recommended/zeus.json').read_text())
    assert policy == {'DownloadDirectory': '${user_home}/Temp', 'BackgroundModeEnabled': False}
    app = configparser.ConfigParser(interpolation=None)
    app.read('/usr/share/applications/google-chrome.desktop')
    assert 'google-chrome-stable' in app['Desktop Entry']['Exec']
    assert '--no-sandbox' not in app['Desktop Entry']['Exec']
    assert not app.getboolean('Desktop Entry', 'NoDisplay', fallback=False)
    print(json.dumps({'package': package, 'version': version, 'firefox_installed': False,
                      'chrome_repository': 'signed metadata and packages; enabled only during image builds',
                      'browser_updates': 'signed Zeus OS updates', 'defaults': policy,
                      'sandbox': 'packaged root-owned setuid helper retained; no sandbox bypass'}))


if __name__ == '__main__':
    main()
