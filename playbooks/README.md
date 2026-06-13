# Ansible Playbooks for Raspberry Pi Setup

These playbooks provide modular, idempotent infrastructure/setup and deployment for Franklin.

## Playbook layout

- `00-preflight.yml` - SSH/connectivity check
- `10-system-packages.yml` - apt update + required system packages
- `15-franklin-user.yml` - create `franklin` runtime user, default `zsh`, Ghostty terminfo
- `20-python-venv.yml` - app dir, `.venv`, pip + Python deps (owned by `franklin`)
- `30-tmuxinator.yml` - Ruby + tmuxinator gem
- `40-redis.yml` - enable/start `redis-server`
- `45-network-hotspot.yml` - configure Pi hotspot/router stack (`hostapd`, `dnsmasq`, `nftables`, static `wlan0`, IPv4 forwarding)
- `50-startup-script.yml` - copy startup scripts and tmuxinator project configs to target dir
- `55-autologin-startup.yml` - configure boot autologin (`tty1`) and login-shell Franklin autostart logic
- `56-wayland-sway.yml` - configure sway (Wayland) session to auto-start Franklin GUI stack
- `57-hdmi-hotplug.yml` - ensure `hdmi_force_hotplug=1` in firmware config for monitor detection reliability
- `58-wayvnc.yml` - install/configure WayVNC for SSH-tunneled remote GUI access
- `60-system-info.yml` - print OS/Python/glibc/Redis info
- `61-health-check.yml` - verifies Caddy + health-check app and fetches the health report JSON
- `62-bounce-web-apps.yml` - respawn/create tmux web windows (`web_scoreboard`, `web_referee`, `web_healthcheck`)
- `63-reboot.yml` - reboot target host and wait for reconnect
- `64-reset-database.yml` - stop Franklin tmux sessions and remove the SQLite database so it is recreated empty
- `site.yml` - runs setup playbooks in order
- `deploy-franklin.yml` - deploy app artifacts (`franklin-hardware-monitor`, Python apps, static/, tmuxinator/, etc.)

## Files

- `inventory.example.ini` - committed inventory example (`raspberrypi.local`, `pi`)
- `inventory.ini` - local inventory used by Ansible (gitignored)
- `group_vars/all.yml` - defaults such as `franklin_user`, `pi_dest_dir`, autologin toggles, package lists
- `ansible.cfg` - local project Ansible config

## Usage

From the project root (first copy the example inventory):

```bash
cp playbooks/inventory.example.ini playbooks/inventory.ini
ansible-playbook -i playbooks/inventory.ini playbooks/site.yml
```

Run one logical part only:

```bash
ansible-playbook -i playbooks/inventory.ini playbooks/20-python-venv.yml
```

Recreate only the Pi hotspot/router setup:

```bash
ansible-playbook -i playbooks/inventory.ini playbooks/45-network-hotspot.yml
```

Deploy Franklin artifacts:

```bash
ansible-playbook -i playbooks/inventory.ini playbooks/deploy-franklin.yml
```

Override host/user/destination directory at runtime:

```bash
ansible-playbook -i playbooks/inventory.ini playbooks/site.yml \
  -e ansible_user=pi \
  -e ansible_host=raspberrypi.local \
  -e pi_dest_dir=/home/pi/franklin-lap-counter
```

## Notes

- Most tasks are idempotent; rerunning should be safe.
- `15-franklin-user.yml` creates a dedicated runtime user (`franklin` by default), sets shell to zsh, and installs Ghostty terminfo for that user.
- Ghostty terminfo install uses `infocmp -x xterm-ghostty` from the control machine when missing on the target.
- Boot behavior is configurable with `franklin_enable_autologin`, `franklin_enable_autostart`, `franklin_enable_wayland_boot`, and `franklin_autologin_tty` in `group_vars/all.yml`.
- Hotspot/router behavior is configurable with `franklin_ap_*` vars in `group_vars/all.yml`.
- Uplink interface selection is portable by default: `franklin_uplink_interface: auto` resolves from the Pi's default route and falls back to `franklin_uplink_interface_fallback` for offline setups.
- WayVNC behavior is configurable with `franklin_enable_wayvnc`, `franklin_wayvnc_bind_address`, `franklin_wayvnc_port`, `franklin_wayvnc_enable_auth`, `franklin_wayvnc_username`, and `franklin_wayvnc_password` in `group_vars/all.yml`.
- Firmware display setting uses `pi_firmware_config_path` (defaults to `/boot/firmware/config.txt`) and enforces `hdmi_force_hotplug=1`.
- `30-tmuxinator.yml` installs tmuxinator only if it is missing.
- This setup stage prepares the target machine; deployment of app binaries/files remains in your existing deploy flow.
- `deploy-franklin.yml` does not copy `.env`; host/runtime settings should be managed in Ansible vars.
- Offline mode is supported: AP + DHCP + local DNS continue working even without internet/uplink.

---

## Raspberry Pi Hotspot/Router Architecture & Setup

This documents the captured live baseline hotspot/router configuration and the matching Ansible automation.

### Captured Live Configuration (Baseline)

#### Topology
- **Uplink/WAN:** `eth0` (DHCP from `10.27.1.0/24`)
- **AP/LAN:** `wlan0`
- **AP gateway IP:** `10.210.1.1/24`
- **DHCP pool (clients):** `10.210.1.50` - `10.210.1.150`
- **NAT:** `10.210.1.0/24` masqueraded out `eth0`

#### Wireless AP
- **Service:** `hostapd` (`enabled`, `active`)
- **Config file:** `/etc/hostapd/hostapd.conf`
- **SSID:** `FranklinLapCounter`
- **Channel:** `7` (`2.4GHz`, `hw_mode=g`)
- **Security:** WPA2-PSK
- **Passphrase:** `lapcounter`

#### DHCP/DNS
- **Service:** `dnsmasq` (`enabled`, `active`)
- **Config file:** `/etc/dnsmasq.conf`
- **Interface:** `wlan0`
- **DHCP range:** `10.210.1.50,10.210.1.150,255.255.255.0,24h`
- **Local domain:** `wlan`
- **Static DNS answer:** `burke.local -> 10.210.1.1`

#### IP assignment on wlan0
A custom systemd unit ensures a deterministic AP address assignment:
- File: `/etc/systemd/system/wlan0-static-ip.service`
- Command: `ExecStart=/sbin/ip addr add 10.210.1.1/24 dev wlan0`
- Ordered before `hostapd.service` and `dnsmasq.service` (while `dhcpcd` is disabled).

#### Routing/firewall
- `net.ipv4.ip_forward=1` in `/etc/sysctl.conf`
- **Service:** `nftables` (`enabled`, `active`)
- Rules in `/etc/nftables.conf`:
  - NAT masquerade: `ip saddr 10.210.1.0/24 oifname "eth0" masquerade`
  - Forward allow: `wlan0 -> eth0` and established/related return traffic
  - Input allow: loopback, all from `wlan0`, SSH on `eth0`
  - Drop all other inbound traffic

#### Wi-Fi stack control
- `NetworkManager` is enabled/active, but does not own AP config.
- `wpa_supplicant` is enabled/active with drop-in override `/etc/systemd/system/wpa_supplicant.service.d/override.conf` forcing `-i wlan0 -D nl80211,wext`.

### DHCP + Portability Behavior

- The playbook does **not** assign a static IP to the uplink Ethernet interface.
- Uplink addressing/routing stays DHCP-driven by the host network stack (typically NetworkManager on Raspberry Pi OS).
- Only the AP-side interface (`wlan0` by default) is pinned to a static subnet for clients.
- AP clients are explicitly handed Pi-local router/DNS settings via DHCP (`option:router` and `option:dns-server`), so local web apps remain reachable without internet.
- Additional local DNS aliases can be set with `franklin_ap_dns_aliases`.
- Current local app aliases include `scoreboard.frank`, `referee.frank`, `healthcheck.frank`, and `racer.frank`.

### Notes

- AP passphrase is currently stored in plain text for parity with existing machine state. Consider moving `franklin_ap_passphrase` to Ansible Vault.
- The playbook uses `ip addr replace` in `wlan0-static-ip.service` so service restarts are idempotent.
