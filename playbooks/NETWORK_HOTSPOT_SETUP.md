# Raspberry Pi Hotspot/Router Setup

This documents the current hotspot/router configuration discovered on `10.27.1.64` and the matching Ansible automation.

## Captured live configuration (baseline)

Captured via Ansible ad-hoc inspection on 2026-06-09.

### Topology

- Uplink/WAN: `eth0` (DHCP from `10.27.1.0/24`)
- AP/LAN: `wlan0`
- AP gateway IP: `10.210.1.1/24`
- DHCP pool (clients): `10.210.1.50` - `10.210.1.150`
- NAT: `10.210.1.0/24` masqueraded out `eth0`

### Wireless AP

- Service: `hostapd` (`enabled`, `active`)
- Config file: `/etc/hostapd/hostapd.conf`
- SSID: `FranklinLapCounter`
- Channel: `7` (`2.4GHz`, `hw_mode=g`)
- Security: WPA2-PSK
- Passphrase: `lapcounter`

### DHCP/DNS

- Service: `dnsmasq` (`enabled`, `active`)
- Config file: `/etc/dnsmasq.conf`
- Interface: `wlan0`
- DHCP range: `10.210.1.50,10.210.1.150,255.255.255.0,24h`
- Local domain: `wlan`
- Static DNS answer: `burke.local -> 10.210.1.1`

### IP assignment on wlan0

Current live setup includes a custom systemd unit:

- `/etc/systemd/system/wlan0-static-ip.service`
- `ExecStart=/sbin/ip addr add 10.210.1.1/24 dev wlan0`
- Ordered before `hostapd.service` and `dnsmasq.service`

(`dhcpcd` is inactive; this custom oneshot unit is what makes the AP address assignment deterministic.)

### Routing/firewall

- `net.ipv4.ip_forward=1` in `/etc/sysctl.conf`
- Service: `nftables` (`enabled`, `active`)
- Rules in `/etc/nftables.conf`:
  - NAT masquerade: `ip saddr 10.210.1.0/24 oifname "eth0" masquerade`
  - Forward allow: `wlan0 -> eth0` and established/related return traffic
  - Input allow: loopback, all from `wlan0`, SSH on `eth0`
  - Drop all other inbound traffic

### Wi-Fi stack control

- `NetworkManager` is enabled/active, but does not own AP config.
- `wpa_supplicant` is enabled/active with drop-in override:
  - `/etc/systemd/system/wpa_supplicant.service.d/override.conf`
  - Forces `-i wlan0 -D nl80211,wext`

## Ansible implementation

The above setup is reproduced by:

- Playbook: `playbooks/45-network-hotspot.yml`
- Included in full setup: `playbooks/site.yml`
- Tunables: `playbooks/group_vars/all.yml` (`franklin_ap_*` and `franklin_uplink_interface`)
  - `franklin_uplink_interface: auto` (default) resolves to the current default-route interface.
  - `franklin_uplink_interface_fallback: eth0` is used when no default route exists (offline/no uplink), so AP setup still applies cleanly.

### Apply only hotspot/router config

```bash
devbox run -- ansible-playbook -i playbooks/inventory.ini playbooks/45-network-hotspot.yml
```

### Apply full Pi setup (includes hotspot/router)

```bash
devbox run setup-pi
```

## DHCP + portability behavior

- The playbook does **not** assign a static IP to the uplink Ethernet interface.
- Uplink addressing/routing stays DHCP-driven by the host network stack (typically NetworkManager on Raspberry Pi OS).
- Only the AP-side interface (`wlan0` by default) is pinned to a static subnet for clients.
- AP clients are explicitly handed Pi-local router/DNS settings via DHCP (`option:router` and `option:dns-server`), so local web apps remain reachable without internet.
- Additional local DNS aliases can be set with `franklin_ap_dns_aliases`.

## Notes

- AP passphrase is currently stored in plain text for parity with existing machine state. Consider moving `franklin_ap_passphrase` to Ansible Vault.
- The playbook uses `ip addr replace` in `wlan0-static-ip.service` so service restarts are idempotent.
