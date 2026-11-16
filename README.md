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

## Several computers

If you run Bria on more than one computer (for example a desktop and a laptop), add them all to one Bria entry: **Settings → Devices & services → Bria VoIP → Configure → Add a computer**. Home Assistant stays connected to every computer at the same time and combines them into one device:

- **On Call**, **Active Calls** and the caller sensors cover calls on any computer; a ringing call comes first.
- Actions go to the computer you are using: the one with the current call, otherwise the one that last had a call or presence change. `answer_call` goes to the computer where the call is ringing.
- Presence changes go to every connected computer.
- Voicemail and missed calls are not double-counted when the same account is signed in on both.
- Every service accepts an optional `machine` field (the computer's name) to pick a computer explicitly. `sign_in`, `sign_out`, `exit` and `check_voicemail` require it when there is more than one computer.
- `get_contact`, `get_call_history` and `get_call_history_item` return their results as response data (and still fire the `bria_contact_lookup` / `bria_call_history` events).

Each computer also gets its own small device (linked to the main one) with **Connected**, **Address** (attribute `via`: `lan` or `tailscale`) and **Bria Version**. The main device has **Active Computer** (which computer actions go to) and a **Reconnect** button; `bria.reconnect` does the same from automations.

Give each computer all the addresses that reach it, one per line, LAN first and then Tailscale (for example `192.168.1.50` and `100.64.12.34`). An address may include a port (`host:9003`) if that Bria uses a different one. Port and certificate verification for the whole entry are under **⋮ → Reconfigure**.

## Network access

Bria must accept connections on TCP 9002 from Home Assistant. Allow only Home Assistant's addresses (for example its LAN IP and its Tailscale IP) on each Bria machine, in an admin PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Bria API from HA" -Direction Inbound -Protocol TCP -LocalPort 9002 -RemoteAddress <ha-lan-ip>,<ha-tailscale-ip> -Action Allow
```

**Why this matters:** once access is allowed, Bria gives every client full control (place calls, change presence, sign out, quit) with no per-client check. If Bria is set to allow API access without prompting, any device that can reach TCP 9002 has that control, so the firewall rule is the only thing standing between your network and your phone line.

## Safety options

**Settings → Devices & services → Bria VoIP → Configure** has a second page, **Safety**:

| Option | Default | Effect |
|---|---|---|
| Allow placing and transferring calls | on | `place_call`, `transfer_call` and `start_attended_transfer` are refused when off. |
| Allowed numbers (regular expression) | empty | When set, those services only accept numbers that fully match, e.g. `1\d{2}` for three-digit extensions starting with 1. |
| Allow call recording | off | `start_call_recording` is refused when off. Recordings are saved on the Bria computer; filenames containing `..` are rejected. |
| Allow sign in, sign out and exit | off | `sign_in`, `sign_out` and `exit` are refused when off. They are also limited to Home Assistant administrators. |

`sign_in` takes a password in plain service data, which is visible in automation YAML and traces. Store it with `!secret`.

## Known limitations

- **The voicemail count only updates when the integration connects to Bria.** Bria sends a voicemail event about once a minute. Answering each one with a voicemail status request stopped Bria from alerting on incoming calls: the first unanswered call rang, and later calls never reached Bria. The integration therefore ignores those events, as the original version did. The count is read on connect; reloading the integration refreshes it. Seen with Bria 6.8.7.1 on Windows.

## Upgrading from 1.x

The device is now identified by the config entry instead of the Bria machine's address, so the integration can follow Bria across addresses. Existing devices, entity IDs and history are moved over automatically on first start. Entities now show as unavailable while Bria is disconnected. The Call History sensor's state is now the time of the most recent call; the number of calls is in its `count` attribute. Screen share is off by default for new installs, because Bria 6.8 rejects screen share status requests. Call recording, sign in, sign out and exit are now refused until you allow them under the Safety options.

## Development

```bash
uv sync
uv run pytest
```

Tests use a scriptable fake Bria server (`tests/fake_bria.py`).
