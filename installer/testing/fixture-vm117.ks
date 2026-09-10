#version=F43
# VM117 is a disposable Fedora 43 storage rehearsal fixture.  Keep this file
# free of passwords and private keys; the public homelab key below is the only
# login credential intentionally installed in the guest.
text
url --url="https://download.fedoraproject.org/pub/fedora/linux/releases/43/Everything/x86_64/os/"
lang en_US.UTF-8
keyboard us
timezone UTC --utc
network --bootproto=dhcp --device=ens18 --activate --hostname=zeusos-dualboot-test
rootpw --lock
firewall --enabled --service=ssh
selinux --enforcing
services --enabled=sshd,qemu-guest-agent
firstboot --disable
bootloader --boot-drive=sda --timeout=5
zerombr
ignoredisk --only-use=sda,sdb
clearpart --all --initlabel --drives=sda,sdb

# Fedora-like source disk: 600 MiB ESP, 1 GiB ext4 /boot, and one Btrfs
# filesystem carrying independent root and home subvolumes.
part /boot/efi --fstype=efi --size=600 --ondisk=sda --label=FEDORA-ESP
part /boot --fstype=ext4 --size=1024 --ondisk=sda --label=FEDORA-BOOT
part btrfs.01 --size=1024 --grow --ondisk=sda
btrfs none --label=FEDORA-BTRFS btrfs.01
btrfs / --subvol --name=root LABEL=FEDORA-BTRFS
btrfs /home --subvol --name=home LABEL=FEDORA-BTRFS

# The independent one-GiB disk is deliberately kept outside the target disk
# so storage experiments can assert that unrelated data remains untouched.
part /var/lib/zeus-sentinel --fstype=ext4 --size=900 --grow --ondisk=sdb --label=ZEUS-SENTINEL

%packages
@core
openssh-server
qemu-guest-agent
btrfs-progs
curl
jq
git
rsync
%end

%post --log=/root/ks-post.log
set -eux

install -d -m 0700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'EOF'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDhT//5O60qUWTn3L4tbrxUxe1ykZkK1YnC200wyOAyZ homelab-mesh
EOF
chmod 0600 /root/.ssh/authorized_keys
chown -R root:root /root/.ssh

install -d -m 0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-zeus-fixture.conf <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitEmptyPasswords no
EOF
chmod 0644 /etc/ssh/sshd_config.d/99-zeus-fixture.conf

install -d -m 0755 /var/lib/zeus-fixture
cat > /var/lib/zeus-fixture/root-sentinel.txt <<'EOF'
VM117 baseline root sentinel; preserve across storage rehearsal.
EOF
cat > /home/zeus-fixture-home-sentinel.txt <<'EOF'
VM117 baseline home sentinel; preserve across storage rehearsal.
EOF
install -d -m 0755 /var/lib/zeus-sentinel
cat > /var/lib/zeus-sentinel/independent-disk-sentinel.txt <<'EOF'
VM117 independent one-GiB disk sentinel; preserve across storage rehearsal.
EOF
cat > /etc/zeus-dualboot-fixture.json <<'EOF'
{"vmid":117,"purpose":"zeus-dualboot-rehearsal"}
EOF
chown root:root /etc/zeus-dualboot-fixture.json
chmod 0600 /etc/zeus-dualboot-fixture.json

systemctl enable qemu-guest-agent.service || true
%end

reboot
