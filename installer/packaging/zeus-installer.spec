%global zeus_version 0.1.0-preview.2
# Keep the noarch Python payload in the same path used by the fixed helper and
# launcher on both 32-bit and 64-bit Fedora installations.
%global zeus_libdir %{_prefix}/lib
%{!?zeus_build_id:%global zeus_build_id local}
%{!?zeus_git_commit:%global zeus_git_commit unknown}
%{!?zeus_source_dirty:%global zeus_source_dirty unknown}
%{!?zeus_rpm_release:%global zeus_rpm_release 0.preview.2.local}

Name:           zeus-installer
Version:        0.1.0
Release:        %{zeus_rpm_release}%{?dist}
Summary:        Fedora launcher for the Zeus OS dual-boot installer
License:        MIT
URL:            https://github.com/KanterLabs/zeusos
BuildArch:      noarch

Requires:       python3
Requires:       python3-gobject
Requires:       gtk4
Requires:       polkit
Requires:       btrfs-progs
Requires:       util-linux
Requires:       util-linux-core
Requires:       efibootmgr
Requires:       openssh-clients
Requires:       openssl
Requires:       podman
Requires:       dosfstools
Requires:       e2fsprogs
Requires:       grub2-tools

%description
An unprivileged GTK4 and CLI launcher that reviews a Fedora layout and
prepares a qualified Zeus OS dual-boot installation. Storage mutation and
privileged installation remain owned by the separately qualified backend.

%prep

%build

%install
install -D -m 0755 %{_sourcedir}/installer/zeus-installer \
    %{buildroot}%{_bindir}/zeus-installer
install -D -m 0755 %{_sourcedir}/installer/zeus-installer-helper \
    %{buildroot}%{_libexecdir}/zeus-installer-helper
install -d -m 0755 %{buildroot}%{zeus_libdir}/zeus-installer/zeus_installer
cp -a %{_sourcedir}/installer/zeus_installer/. \
    %{buildroot}%{zeus_libdir}/zeus-installer/zeus_installer/
find %{buildroot}%{zeus_libdir}/zeus-installer -type d -name __pycache__ -prune -exec rm -rf {} +
find %{buildroot}%{zeus_libdir}/zeus-installer -type f -name '*.pyc' -delete
find %{buildroot}%{zeus_libdir}/zeus-installer -type d -exec chmod 0755 {} \;
find %{buildroot}%{zeus_libdir}/zeus-installer -type f -name '*.py' -exec chmod 0644 {} \;
install -D -m 0644 %{_sourcedir}/installer/packaging/org.zeus.Installer.desktop \
    %{buildroot}%{_datadir}/applications/org.zeus.Installer.desktop
install -D -m 0644 %{_sourcedir}/installer/README.md \
    %{buildroot}%{_docdir}/%{name}/README.md
install -D -m 0644 %{_sourcedir}/LICENSE \
    %{buildroot}%{_docdir}/%{name}/LICENSE
cat > %{buildroot}%{_docdir}/%{name}/BUILD-INFO <<EOF
product_version=%{zeus_version}
build_id=%{zeus_build_id}
rpm_release=%{zeus_rpm_release}
git_commit=%{zeus_git_commit}
source_dirty=%{zeus_source_dirty}
EOF

%files
%license %{_docdir}/%{name}/LICENSE
%doc %{_docdir}/%{name}/README.md
%doc %{_docdir}/%{name}/BUILD-INFO
%{_bindir}/zeus-installer
%{_libexecdir}/zeus-installer-helper
%{zeus_libdir}/zeus-installer/
%{_datadir}/applications/org.zeus.Installer.desktop

%changelog
* Thu Sep 10 2026 Zeus OS contributors <zeus@example.invalid> - 0.1.0-0.preview.2
- Preview Fedora graphical dual-boot launcher.
