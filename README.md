# WCLogs Eye — Companion (source)

This is the **open source of the WCLogs Eye companion app** (`WCLogsEyeCompanion.exe`) — the small
Windows tool that fetches WarcraftLogs parses **under your own free API key** and writes them into
the [WCLogs Eye](https://www.curseforge.com/wow/addons/wclogs-eye) WoW addon's database.

**Download:** grab `WCLogsEyeCompanion.exe` from the [latest release](../../releases/latest) (built
by CI from this repo), or from [wclogseye.top](https://wclogseye.top). Both sync the community
parse database out of the box.

It's published so you can **read exactly what the .exe does** and, if you want, **build it yourself**
instead of trusting a pre-built binary. The companion:

- talks only to `warcraftlogs.com` (with the key *you* enter) and to the project's community hub;
- never sends your API key anywhere except WarcraftLogs;
- writes the addon's `Data.lua` and a small cache next to itself.

The backend (community hub, website) lives in a separate private repo and is **not** required to
build or run this — without a baked-in hub the companion simply works fully locally.

## What's here

| File | Role |
|---|---|
| `companion.py` | The app: tray window, watches the addon's SavedVariables, fetches, writes `Data.lua`. |
| `fetch_parses.py` | WarcraftLogs GraphQL client (OAuth, zone/encounter rankings, realm-slug handling). |
| `lua_export.py` | Serializes the character DB to `Data.lua`. |
| `hub_defaults.example.json` | Optional baked hub config (copy to `hub_defaults.json`; empty = local-only). |
| `icon.ico`, `success.wav` | Bundled assets. |

## Build it yourself

```
pip install -r requirements.txt
build.bat            # or run the PyInstaller line inside it
```

The build drops `WCLogsEyeCompanion.exe` in `dist/`. A GitHub Actions workflow
(`.github/workflows/build.yml`) builds it on every push and **publishes it to
[Releases](../../releases)**, so you can download a CI-built binary or compare it to the published one.

> Note: CI/official builds bake the **public hub URL**, so they read the shared community DB out of
> the box (reading needs no credential). *Uploading* your results to the pool is gated by a token
> that is intentionally not in this repo — a self-build without it still fetches and syncs, it just
> doesn't contribute back. A fully manual `build.bat` with no `hub_defaults.json` runs local-only.

## Privacy

Your WarcraftLogs Client ID/Secret are stored locally in `%APPDATA%\WarcraftLogsTips\config.json`
and used only to call the WarcraftLogs API. Results (parse %) may be shared with the community hub;
your key is never transmitted.

## License

MIT — see [LICENSE](LICENSE).
