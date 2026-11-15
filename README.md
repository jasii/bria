# Bria VoIP for Home Assistant

Custom integration that connects Home Assistant to the Bria Desktop softphone (Windows/Mac) through the Bria Desktop API WebSocket on port 9002. It exposes call, presence, audio and account state as entities and offers services to place, answer, hold, transfer and end calls.

Work in progress: version 2.0 is being reworked for correctness, security and multi-address (LAN + Tailscale) support.

## Install

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/jasii/bria`, type **Integration**.
2. Install **Bria VoIP**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → **Bria VoIP**.
4. In Bria, open the **Notifications** tab and click **Allow Access** the first time Home Assistant connects.

### Manual (Samba share)

1. Mount the Home Assistant `config` share (Samba add-on), e.g. `smb://homeassistant.local/config`.
2. `scripts/deploy.sh` (copies `custom_components/bria/` to the share; pass another mount point as the first argument if needed).
3. Restart Home Assistant, then add the **Bria VoIP** integration.
4. In Bria, open the **Notifications** tab and click **Allow Access** the first time Home Assistant connects.

## Network access

Bria must accept connections on TCP 9002 from Home Assistant. Allow only Home Assistant's addresses (for example its LAN IP and its Tailscale IP) on each Bria machine, in an admin PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Bria API from HA" -Direction Inbound -Protocol TCP -LocalPort 9002 -RemoteAddress <ha-lan-ip>,<ha-tailscale-ip> -Action Allow
```

**Why this matters:** once access is allowed, Bria gives every client full control (place calls, change presence, sign out, quit) with no per-client check. If Bria is set to allow API access without prompting, any device that can reach TCP 9002 has that control, so the firewall rule is the only thing standing between your network and your phone line.

## Upgrading from 1.x

The device is now identified by the config entry instead of the Bria machine's address, so the integration can follow Bria across addresses. Existing devices, entity IDs and history are moved over automatically on first start. Entities now show as unavailable while Bria is disconnected. The Call History sensor's state is now the time of the most recent call; the number of calls is in its `count` attribute. Screen share is off by default for new installs, because Bria 6.8 rejects screen share status requests.

## Development

```bash
uv sync
uv run pytest
```

Tests use a scriptable fake Bria server (`tests/fake_bria.py`).
