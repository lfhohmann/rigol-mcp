# rigol-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Code, Claude Desktop,
Cursor, and other MCP clients) read and control a **Rigol DS1000Z** oscilloscope over the network.

Ask your agent things like *"take a screenshot of the scope and tell me what's on CH1"* or
*"what's the ripple on CH2?"*. It reads measurements, downloads waveforms, captures the screen,
and changes settings when you ask.

- **No drivers.** Talks SCPI over the scope's raw TCP socket (port 5555). No VISA, no NI software.
- **Two ways to run.** Locally over stdio for one agent, or in Docker over HTTP as a shared
  bridge for several agents and machines.
- **Screenshot history.** Every screenshot and waveform is saved with a UTC timestamp. The last
  N are kept, and a web gallery shows them.
- **Careful by default.** Settings change only through one explicit tool. Reset is blocked, and
  a read-only mode is available.

![Claude Code taking a scope screenshot and describing CH1 and CH2](docs/images/claude-code-example.png)

*Claude Code asked to "take a screenshot of the scope and tell me what's on CH1 and CH2". It reads
the screen and the measurements, then explains the event: a 24 V signal switching off, and a
logic signal responding 5.86 ms later.*

### Things to ask

- "Take a screenshot of the scope and tell me what's on each channel."
- "What's the frequency and duty cycle on CH1?"
- "Measure the delay between the CH2 falling edge and the CH1 rising edge."
- "Download CH1 and check how much noise is on the high level."
- "Show me the screenshot from 10 minutes ago and compare it with the scope now."
- "Set CH1 to 1 V/div and arm a single trigger on CH2's rising edge." *(changes settings)*

Tested on a DS1104Z (firmware 00.04.04.SP3). Other DS1000Z and MSO1000Z models (DS1054Z,
DS1074Z, DS1102Z-E, MSO1104Z and so on) use the same command set and should work. Reports welcome.

## Contents

- [Tools](#tools)
- [Before you start](#before-you-start)
- [Option 1: Local (stdio)](#option-1-local-stdio)
- [Option 2: Docker (HTTP bridge)](#option-2-docker-http-bridge)
- [Option 3: HTTP without Docker](#option-3-http-without-docker)
- [Capturing an event and the full memory](#capturing-an-event-and-the-full-memory)
- [Setups, logging and the audit log](#setups-logging-and-the-audit-log)
- [Screenshot history and gallery](#screenshot-history-and-gallery)
- [Configuration](#configuration)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Tools

| Tool | What it does | Touches the scope |
|---|---|---|
| **Reading** | | |
| `scope_status` | Model, run state, trigger, timebase, memory depth; per channel V/div, Vpp, mean, RMS, frequency (about 1 s) | reads |
| `scope_measure` | Any of the scope's 37 built-in measurements on one channel (rise time, duty cycle, ...) | reads |
| `scope_measure_between` | Delay and phase between two channels (rising or falling edges) | reads |
| `scope_measure_stats` | Repeats a measurement N times: mean, min, max, stdev (jitter, drift) | reads |
| `scope_waveform` | The 1200 on-screen samples: stats, clipping warning, optional full CSV | reads |
| `scope_export_memory` | The **full acquisition memory** (up to 24M points) streamed to a `.bin` or `.csv` file | reads* |
| `scope_screenshot` | Captures the display as a PNG (the agent sees the image) and saves it | reads |
| `scope_query` | Any read-only SCPI query (must end in `?`) | reads |
| **History** | | |
| `scope_screenshot_history` | Lists saved screenshots, newest first | no |
| `scope_screenshot_get` | Returns a saved screenshot (`latest` or by id) | no |
| `scope_waveform_history` | Lists saved waveform CSVs or memory exports | no |
| **Control** | | |
| `scope_run_control` | Run, stop, single, autoscale, force trigger, clear | **writes** |
| `scope_set_channel` | On/off, V/div, offset, coupling, probe ratio, 20 MHz limit, invert | **writes** |
| `scope_set_timebase` | Time/div, trigger position, main/XY/roll mode | **writes** |
| `scope_set_trigger` | Edge trigger: source, level, slope, sweep (auto/normal/single), coupling, holdoff | **writes** |
| `scope_set_acquire` | Normal, averaging, peak detect, high-res; memory depth | **writes** |
| `scope_capture` | Arms a single trigger, waits for the event, returns a screenshot and per-channel summary | **writes** |
| `scope_write` | Any other SCPI setting command | **writes** |
| **Setups, logging, audit** | | |
| `scope_setup_save` | Saves every setting (4 channels, timebase, acquisition, trigger, run/stop) to a named file | no |
| `scope_setup_list` | Lists saved setups | no |
| `scope_setup_restore` | Restores a setup: sends only what differs, paced, and saves an undo point first; `dry_run` just shows the differences | **writes** |
| `scope_log_start` | Logs measurements every N seconds to a CSV in the background (up to 24 h) | no* |
| `scope_log_status` | Progress, last values and min/max/mean of a running log | no |
| `scope_log_stop` | Stops a log early | no |
| `scope_audit_log` | Recent setting changes: time, client, tool, SCPI commands, scope reply | no |

\* `scope_export_memory` needs the scope stopped. With `stop=true` it stops the scope itself.
Measurement tools (`scope_measure*`, `scope_log_start`) add their items to the scope's on-screen
measurement bar.

**Measurements show up on the scope.** When asked for a measurement, the DS1000Z also adds
it to the measurement bar at the bottom of its screen (it keeps the last 5). The reading
tools change nothing else.

Every setter reads back the values the scope actually applied (it rounds to the steps it
supports) and returns the scope's error queue, so the agent can see when a value was refused.

Invalid measurements (the scope's `9.9E37` or `measure error!`, e.g. the frequency of a DC
signal) come back as `null` instead of a misleading number.

## Before you start

1. Connect the scope to your network and note its IP address: **Utility > IO Setting > LAN Conf**.
2. Check that your machine can reach it:

   ```bash
   echo '*IDN?' | nc -w 3 192.168.1.50 5555
   ```

   You should see something like `RIGOL TECHNOLOGIES,DS1104Z,DS1ZA...,00.04.04.SP3`.

3. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) for the local option,
   or Docker for the bridge.

Replace `192.168.1.50` with your scope's address everywhere below.

## Option 1: Local (stdio)

Best for one person and one agent on a machine that can reach the scope. Your MCP client starts
the server as a subprocess, so nothing listens on the network.

**Claude Code**

```bash
claude mcp add --scope user rigol -e RIGOL_HOST=192.168.1.50 -- \
  uvx --from git+https://github.com/lfhohmann/rigol-mcp rigol-mcp
```

**Claude Desktop, Cursor, and other clients**: add this to the client's MCP config
(for Claude Desktop, `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "rigol": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/lfhohmann/rigol-mcp", "rigol-mcp"],
      "env": { "RIGOL_HOST": "192.168.1.50" }
    }
  }
}
```

Screenshots and waveforms are saved in `~/.local/share/rigol-mcp/` (set `RIGOL_DATA_DIR` to
change it). There is no gallery page in this mode, but the files are there to browse.

> The scope handles only one network client well. Every agent session in stdio mode starts its
> own server process, so with several agents at once, use the Docker bridge instead.

## Option 2: Docker (HTTP bridge)

Best when several agents or machines share one scope. The container is the scope's only client
and takes turns for everyone. It serves MCP over HTTP with a token and hosts the screenshot gallery.

```text
 Claude Code (laptop) ─┐
 Claude Code (server) ─┼─ HTTP + token ──► rigol-mcp container ── SCPI :5555 ──► scope
 other MCP clients ────┘     :8790/mcp       (gallery, history)
```

The prebuilt image runs on amd64 and arm64 (including the Raspberry Pi 4 and 5) and is
published to both [Docker Hub](https://hub.docker.com/r/lfhohmann/rigol-mcp)
(`lfhohmann/rigol-mcp`) and GitHub (`ghcr.io/lfhohmann/rigol-mcp`). Both are the same image,
so use whichever you prefer. You don't need to clone the repo.

| Tag | Meaning |
|---|---|
| `latest` | newest release (recommended) |
| `0.2.1`, `0.2` | a specific release, or the newest patch of 0.2 |
| `edge` | newest commit on `main`, untested on hardware |

1. Make a folder with the compose file and the settings template:

   ```bash
   mkdir rigol-mcp && cd rigol-mcp
   curl -fsSLO https://raw.githubusercontent.com/lfhohmann/rigol-mcp/main/compose.yaml
   curl -fsSL https://raw.githubusercontent.com/lfhohmann/rigol-mcp/main/.env.example -o .env
   ```

2. Edit `.env`: set `RIGOL_HOST`, a token (`openssl rand -hex 24`), and `RIGOL_PUBLIC_URL`
   (how clients reach this machine, e.g. `http://192.168.1.10:8790`). Set `UID` and `GID` to
   the output of `id -u` and `id -g` so the saved files belong to you.

3. Create the data folder and start the container:

   ```bash
   mkdir -p data
   docker compose up -d
   ```

   `compose.yaml` pulls from GHCR; to use Docker Hub instead, change its `image:` line to
   `lfhohmann/rigol-mcp:latest`. To update later, run `docker compose pull && docker compose up -d`. To build from source
   instead, clone the repo and run
   `docker compose -f compose.yaml -f compose.build.yaml up -d --build`.

4. Check that it's up (no token needed):

   ```bash
   curl http://localhost:8790/health
   ```

5. Connect each client. For Claude Code:

   ```bash
   claude mcp add --scope user --transport http rigol http://192.168.1.10:8790/mcp \
     --header "Authorization: Bearer <your-token>"
   ```

   Other clients that support streamable HTTP use the same URL and header.

Screenshots and waveforms are written to `./data/screenshots` and `./data/waveforms` on the host.
The container restarts automatically (`restart: unless-stopped`).

Without compose, a single command does the same:

```bash
mkdir -p data
docker run -d --name rigol-mcp --restart unless-stopped -p 8790:8790 \
  -u "$(id -u):$(id -g)" -v "$PWD/data:/data" \
  -e RIGOL_HOST=192.168.1.50 -e RIGOL_MCP_TOKEN=<your-token> \
  -e RIGOL_PUBLIC_URL=http://192.168.1.10:8790 \
  lfhohmann/rigol-mcp:latest
```

## Option 3: HTTP without Docker

Same as the bridge, but run directly with uv:

```bash
RIGOL_HOST=192.168.1.50 RIGOL_MCP_TOKEN=<your-token> RIGOL_PUBLIC_URL=http://192.168.1.10:8790 \
  uvx --from git+https://github.com/lfhohmann/rigol-mcp rigol-mcp --http --port 8790
```

## Capturing an event and the full memory

A typical flow for a one-off event, such as a power-on glitch or a relay switching:

1. Ask *"capture the next rising edge on CH2 at 1.5 V"*. The agent calls `scope_capture` with
   `source="CHAN2", level_v=1.5, slope="POS"`, which arms a single trigger and waits (10 s by
   default, up to 120 s). Afterwards the scope stays stopped on the capture, and the trigger
   sweep goes back to what it was (e.g. NORM), so **RUN** resumes as before.
2. Cause the event. The agent gets the screenshot and a per-channel summary as soon as the
   scope triggers.
3. Ask *"save the full record of CH1"*. `scope_export_memory` reads the whole acquisition
   memory (not just the 1200 points on screen) in 250k-point chunks straight to disk. It
   returns stats, a frequency estimate and a min/max preview, so the agent never has to load
   millions of points.

Memory exports go to `<data dir>/exports/`, with a `.json` file next to each one that holds
the scaling (`volts = (byte - yorigin - yreference) * yincrement`). The last
`RIGOL_KEEP_EXPORTS` (default 10) are kept. The DS1000Z sends about 0.5 M points/s (measured
on a DS1104Z), so 3M points take ~6 s and 24M points ~50 s, and other calls wait meanwhile.

> **Why the full memory matters.** The 1200-point screen data keeps the peaks of narrow
> pulses when it shrinks millions of points down to 1200, and the scope's own measurements
> use that screen data too. On a bursty signal tested here, the screen data put 30% of
> points above 0.3 V while the full record had 0.21%. Screen-based RMS read 0.317 V; the
> true RMS from the full record was 0.034 V.

## Setups, logging and the audit log

- **Setups.** Ask *"save the scope setup as bench"* before an experiment and *"restore bench"*
  afterwards. `scope_setup_restore` reads the scope first and sends only the settings that
  differ, one at a time with a pause and a confirmation each (a fast burst of commands can hang
  the DS1000Z's network interface). It saves the current state as `auto-before-restore`
  first, so a restore can be undone. Setups are JSON files in `<data dir>/setups/`.
- **Measurement logging.** *"Log Vpp and frequency on CH1 every second for 10 minutes"* starts
  a background job that writes `<data dir>/logs/log_<time>.csv`. Other tools keep working
  meanwhile; each reading holds the scope only briefly. The gallery shows a small chart per log.
  The last `RIGOL_KEEP_LOGS` (default 50) are kept.
- **Audit log.** Every setting change made through the server is appended to
  `<data dir>/audit.jsonl` with the UTC time, the client (IP address and program for HTTP
  clients), the tool, the exact SCPI commands and the scope's reply. `scope_audit_log` and the
  gallery show the most recent ones.
- **Long calls don't block the bridge.** Tools run in worker threads, so a 50 s memory export
  or a capture waiting for a trigger doesn't stop other agents from listing screenshots or
  checking a log; calls that need the scope simply wait their turn.

## Screenshot history and gallery

- Every `scope_screenshot` call saves a PNG named with its UTC time, e.g.
  `screenshot_20261124T173058.934Z.png`. The last `RIGOL_KEEP_SCREENSHOTS` (default 100) are
  kept and older ones are deleted. Waveforms work the same way (`RIGOL_KEEP_WAVEFORMS`).
- Agents can look back without touching the scope: `scope_screenshot_history` lists them and
  `scope_screenshot_get` returns one (`latest` by default).
- In HTTP mode, open the gallery in a browser:

  ```text
  http://192.168.1.10:8790/screenshots?token=<your-token>
  ```

  ![The screenshot gallery: newest first, with UTC timestamps and download links](docs/images/gallery.png)

  The token in the URL is stored as a cookie, so later visits to `/screenshots` work without it.
  Single files are at `/screenshots/<file name>`, `/waveforms/<file name>` and
  `/exports/<file name>` (plus `.json` for an export's metadata), and
  `/screenshots/latest` always gives the newest screenshot.

## Configuration

All settings are environment variables. In Docker they go in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `RIGOL_HOST` | (required) | Scope IP address or host name (or pass `--scope`) |
| `RIGOL_PORT` | `5555` | Scope SCPI port |
| `RIGOL_TIMEOUT` | `10` | Seconds to wait for the scope |
| `RIGOL_DATA_DIR` | `~/.local/share/rigol-mcp` (`/data` in Docker) | Where captures are saved |
| `RIGOL_KEEP_SCREENSHOTS` | `100` | Screenshots to keep; `0` keeps all |
| `RIGOL_KEEP_WAVEFORMS` | `100` | Waveform CSVs to keep; `0` keeps all |
| `RIGOL_KEEP_EXPORTS` | `10` | Full-memory exports to keep; `0` keeps all |
| `RIGOL_KEEP_LOGS` | `50` | Measurement logs to keep; `0` keeps all |
| `RIGOL_READ_ONLY` | off | `1` blocks every tool that changes the scope |
| `RIGOL_TRANSPORT` | `stdio` | `http` is the same as `--http` |
| `RIGOL_HTTP_HOST` | `0.0.0.0` | HTTP bind address (or `--bind`) |
| `RIGOL_HTTP_PORT` | `8790` | HTTP port (or `--port`) |
| `RIGOL_MCP_TOKEN` | (required for HTTP) | Token clients must send |
| `RIGOL_ALLOW_NO_AUTH` | off | `1` allows HTTP without a token (trusted networks only) |
| `RIGOL_PUBLIC_URL` | none | Base URL used in links to screenshots and the gallery |
| `RIGOL_HOST_PORT` | `8790` | Docker only: port published on the host |

## Security

- **Writes are explicit.** Only the control tools change settings, and their descriptions tell
  agents to use them only when you ask. `*RST` is blocked. `RIGOL_READ_ONLY=1` turns every
  write off, including `scope_capture` and `scope_export_memory(stop=true)`.
  Real circuits may be connected to the probes, so keep it that way.
- **HTTP mode needs a token.** It's checked in constant time on every request except `/health`.
- **The traffic is plain HTTP.** On an untrusted network, put the server behind a VPN
  (WireGuard, NetBird, Tailscale) or an HTTPS reverse proxy such as Caddy or Traefik.
- **The scope itself has no authentication.** Anyone who can reach port 5555 can control it.
  Keep it on a network you trust.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `cannot connect to scope` | Wrong IP, or the scope's LAN is off. Check **Utility > IO Setting**, then run the `nc` test above. |
| Works, then hangs or times out | Another program (the scope's web page, UltraScope, a second server) is connected. Use one client at a time. |
| The scope beeps | Its beeper sounds when a command is rejected (e.g. `-220 Parameter error`). The `error` field of each control tool shows why. Turn it off in **Utility > Sound** or with `scope_write(":SYST:BEEP OFF")`. |
| Scope stops answering, front panel still works | Its network command service hung (it can happen after a burst of commands or two clients at once). Restarting LAN in **Utility > IO Setting** may not help; power-cycle the scope. Settings are kept. |
| A measurement is `null` | The scope can't measure it: no edges, or the signal is off screen. Look at the screenshot. |
| `clipped_samples > 0` | The trace goes off screen, so min, max and Vpp are wrong. Change V/div or offset. |
| HTTP clients get 401 | Missing or wrong `Authorization: Bearer <token>` header. |
| Files in `./data` owned by root, or permission errors | Create `data/` before the first start and set `UID`/`GID` in `.env`. |

Need a command the tools don't cover? `scope_query` and `scope_write` accept any SCPI command
from Rigol's *MSO1000Z/DS1000Z Programming Guide* (on Rigol's website).
[docs/direct-scpi.md](docs/direct-scpi.md) shows how to talk to the scope without this server.

## Development

```bash
git clone https://github.com/lfhohmann/rigol-mcp.git
cd rigol-mcp
uv sync
uv run pytest
```

The tests use a fake scope (`tests/fake_scope.py`), so no hardware is needed. To try your changes
against a real scope:

```bash
RIGOL_HOST=192.168.1.50 uv run rigol-mcp            # stdio
RIGOL_HOST=192.168.1.50 RIGOL_MCP_TOKEN=dev uv run rigol-mcp --http
```

## License

[MIT](LICENSE)
