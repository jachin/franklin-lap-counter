# Ansible Playbooks for Raspberry Pi Setup

These playbooks replace the behavior from `scripts/setup-pi.sh` in a modular, idempotent way.

## Playbook layout

- `00-preflight.yml` - SSH/connectivity check
- `10-system-packages.yml` - apt update + required system packages
- `15-franklin-user.yml` - create `franklin` runtime user, default `zsh`, Ghostty terminfo
- `20-python-venv.yml` - app dir, `.venv`, pip + Python deps (owned by `franklin`)
- `30-tmuxinator.yml` - Ruby + tmuxinator gem
- `40-redis.yml` - enable/start `redis-server`
- `50-startup-script.yml` - copy `scripts/start-franklin.sh` to target dir
- `60-system-info.yml` - print OS/Python/glibc/Redis info
- `site.yml` - runs setup playbooks in order
- `deploy-franklin.yml` - deploy app artifacts to Pi (replacement for `scripts/deploy-to-pi.sh`)

## Files

- `inventory.example.ini` - committed inventory example (`raspberrypi.local`, `pi`)
- `inventory.ini` - local inventory used by Ansible (gitignored)
- `group_vars/all.yml` - defaults such as `franklin_user`, `pi_dest_dir`, package lists
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
- `30-tmuxinator.yml` installs tmuxinator only if it is missing.
- This setup stage prepares the target machine; deployment of app binaries/files remains in your existing deploy flow.
