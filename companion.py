#!/usr/bin/env python3
"""
companion.py — background watcher (test version of the companion app).

Watches the addon's SavedVariables file. When you /reload in-game (which flushes the
queue to disk), it picks up newly queued characters, fetches their parses from WCL,
and merges them into a growing local database that it writes to the addon's Data.lua.

Flow for the user:
  1. Run this once:  python companion.py   (leave it running in the background)
  2. In-game: right-click a player -> "WCL: добавить в очередь"
  3. /reload  (flushes the queue to disk)
  4. Companion fetches within ~1-2s, rewrites Data.lua
  5. /reload  -> the new player's parses show on hover

The DB accumulates: every character ever fetched stays in cache.json and is rewritten
into Data.lua each time, so your local database only grows.

(The real companion will be a tray app and will also sync with the central server.)
"""

import os
import re
import sys
import time
import json
import queue
import datetime
import urllib.request
import fetch_parses as F

POLL_SECONDS = 2          # how often to check the SavedVariables file
RETRY_SECONDS = 180       # also re-run periodically so budget-deferred chars resume after reset
TTL_HOURS = 6             # don't re-fetch a character more often than this
NODATA_TTL_DAYS = 7       # a character with no logs isn't re-fetched for this long
DATA_TTL_DAYS = 7         # drop a character's data if it hasn't refreshed within this
HERE = F._base_dir()      # next to the .exe when frozen, else next to the script
CACHE_PATH = os.path.join(HERE, "cache.json")

APP_NAME = "WCLogs Eye"
APP_VERSION = "1.5.15"
WCL_CLIENTS_URL = "https://www.warcraftlogs.com/api/clients/"
SITE_URL = "https://wclogseye.top"
EXE_URL = SITE_URL + "/WCLogsEyeCompanion.exe"


def _ver_tuple(v):
    """'1.5.11' -> (1, 5, 11) for comparison; non-numeric -> ()."""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def latest_version():
    """The version published on the site, read from the .exe's Content-Disposition filename
    (e.g. 'WCLogsEyeCompanion v1.5.11.exe'). None on any error — update check is best-effort."""
    try:
        req = urllib.request.Request(EXE_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            cd = r.headers.get("Content-Disposition") or ""
        m = re.search(r"v(\d+(?:\.\d+)+)", cd)
        return m.group(1) if m else None
    except Exception:
        return None


def cleanup_old_exe():
    """Remove the previous .exe left behind by a self-update (renamed aside, can't delete while
    it was running). Safe no-op when not frozen / nothing to clean."""
    if getattr(sys, "frozen", False):
        old = sys.executable + ".old"
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            pass


def self_update():
    """Download the newest .exe and swap it in for the running one, then relaunch. Windows can't
    overwrite a running .exe, but it CAN rename it: move ourselves to .old, drop the new build in
    our place, start it, and exit. The fresh instance deletes the .old on startup. Frozen-only;
    returns True if a relaunch was started (caller should quit). Best-effort — never crashes."""
    if not getattr(sys, "frozen", False):
        return False
    exe = sys.executable
    newp, oldp = exe + ".new", exe + ".old"
    try:
        urllib.request.urlretrieve(EXE_URL, newp)
        if os.path.getsize(newp) < 1_000_000:  # sanity: a real build is many MB, not an error page
            os.remove(newp)
            return False
    except Exception as e:
        print(f"update download failed ({e})")
        try:
            os.path.exists(newp) and os.remove(newp)
        except OSError:
            pass
        return False
    try:
        if os.path.exists(oldp):
            os.remove(oldp)
        os.rename(exe, oldp)   # rename the running exe aside (allowed on Windows)
        os.rename(newp, exe)   # put the new build in our place
    except OSError as e:
        print(f"update swap failed ({e})")
        return False
    try:
        import subprocess
        subprocess.Popen([exe], close_fds=True)
        print("updated — relaunching the new version…")
        return True
    except Exception as e:
        print(f"relaunch failed ({e})")
        return False


def _lang():
    """UI language: English by default, Russian only when the OS locale is Russian."""
    import locale as _loc
    try:
        code = (_loc.getdefaultlocale()[0] or "").lower()
    except Exception:
        code = ""
    return "ru" if code.startswith("ru") else "en"


def _ui_strings():
    """All companion UI text. English is the default; Russian overrides when detected."""
    s = dict(
        prompt_heading="Enter your WarcraftLogs API key",
        setup_sub="The key is free. Redirect URL when creating it: http://localhost",
        create_key="Create a key on the website  ↗",
        fill_both="Fill in both fields.",
        save_run="Save & start",
        console_setup="=== First-time setup ===",
        no_key="No WCL key entered. Start again and paste your Client ID + Secret.",
        start_failed="Couldn't start:\n{e}\n\nCheck your WCL key.",
        db="In DB: {n}", queued="  •  queue: {q}", left="  •  ~{left}/hr left",
        key_id="key: {k}",
        fetch="Fetch queue", stop="Stop & Clean", paused="  •  paused", keys="Enter key…", notif="Notifications",
        data="Data folder", log="Open log", quit="Quit",
        ap_heading="Where is the addon installed?",
        ap_guide=("Couldn't find WoW automatically. Point to the WCLogs Eye ADDON folder "
                  "(the folder named WCLogsEye), e.g.:\n"
                  "…\\World of Warcraft\\_retail_\\Interface\\AddOns\\WCLogsEye\n"
                  "Not the WoW root and not the WTF folder."),
        ap_browse="Browse…", ap_set="Addon folder…",
        ap_invalid="That folder doesn't exist — pick the WCLogsEye addon folder.",
        update_avail="⬆ Update  v{cur} → v{new}",
        check_upd="Check updates", checking="checking for updates…",
        up_to_date="you're on the latest version (v{v}).", check_failed="update check failed (offline?).",
        show="Show window", min_hint="WCLogs Eye keeps running in the tray.",
        sound="Sound on done",
        key_updated="Key updated",
        notify="Fetched {n} — /reload in-game",
        close="\nPress Enter to close this window...",
    )
    if _lang() == "ru":
        s.update(
            prompt_heading="Введи свой ключ WarcraftLogs API",
            setup_sub="Ключ бесплатный. Redirect URL при создании: http://localhost",
            create_key="Создать ключ на сайте  ↗",
            fill_both="Заполни оба поля.",
            save_run="Сохранить и запустить",
            console_setup="=== Первая настройка ===",
            no_key="WCL-ключ не введён. Запусти заново и вставь Client ID + Secret.",
            start_failed="Не удалось запуститься:\n{e}\n\nПроверь ключ WCL.",
            db="В базе: {n}", queued="  •  очередь: {q}", left="  •  осталось ~{left}/час",
            key_id="ключ: {k}",
            fetch="Проверить очередь", stop="Стоп и очистить", paused="  •  пауза", keys="Ввести ключ…", notif="Уведомления",
            data="Папка с данными", log="Открыть лог", quit="Выход",
            ap_heading="Где установлен аддон?",
            ap_guide=("WoW не нашёлся автоматически. Укажи папку АДДОНА WCLogs Eye "
                      "(папка с именем WCLogsEye), например:\n"
                      "…\\World of Warcraft\\_retail_\\Interface\\AddOns\\WCLogsEye\n"
                      "Не корень WoW и не папку WTF."),
            ap_browse="Обзор…", ap_set="Папка аддона…",
            ap_invalid="Папка не найдена — выбери папку аддона WCLogsEye.",
            update_avail="⬆ Обновить  v{cur} → v{new}",
            check_upd="Обновления", checking="проверяю обновления…",
            up_to_date="у тебя последняя версия (v{v}).", check_failed="не удалось проверить (нет сети?).",
            show="Открыть окно", min_hint="WCLogs Eye свёрнут в трей и работает.",
            sound="Звук по готовности",
            key_updated="Ключ обновлён",
            notify="Загружено {n} — сделай /reload в игре",
            close="\nНажми Enter, чтобы закрыть окно...",
        )
    return s


S = _ui_strings()


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _parse_dt(ts):
    """Parse an ISO timestamp/date to a NAIVE local datetime. Hub timestamps are tz-aware
    (UTC); our own are naive — normalizing avoids 'offset-naive vs offset-aware' subtraction."""
    if not ts:
        return None
    try:
        t = datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)  # -> naive local
    return t


def is_stale(ts):
    t = _parse_dt(ts)
    if t is None:
        return True
    return (datetime.datetime.now() - t).total_seconds() > TTL_HOURS * 3600


def _age_days(ts):
    """Age of an ISO timestamp/date in days, or None if missing/unparseable."""
    t = _parse_dt(ts)
    if t is None:
        return None
    return (datetime.datetime.now() - t).total_seconds() / 86400.0


def _nodata_cooling(cache, key):
    """True while a no-data character is on its cooldown (don't re-fetch yet)."""
    a = _age_days(cache.get("nodata", {}).get(key))
    return a is not None and a < NODATA_TTL_DAYS


def _prune_old(cache):
    """Drop characters whose data hasn't refreshed within DATA_TTL_DAYS."""
    data, ts = cache.get("data", {}), cache.get("ts", {})
    for k in list(data.keys()):
        a = _age_days(ts.get(k))
        if a is not None and a > DATA_TTL_DAYS:
            data.pop(k, None)
            ts.pop(k, None)
            cache.get("nodata", {}).pop(k, None)


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            return json.load(open(CACHE_PATH, encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"data": {}, "ts": {}}


def save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)


def read_full_queue(cfg):
    queue = [(c["name"], c["realm"], c.get("region", "eu")) for c in cfg.get("manual_characters", [])]
    sv = cfg.get("savedvariables_path", "")
    if sv and "YOURACCOUNT" not in sv:
        queue.extend(F.read_queue(sv))
    return list(dict.fromkeys(queue))


def budget_snapshot(affordable, rl):
    """Compact, addon-readable view of this key's hourly budget. reset_at is an absolute Unix
    epoch so the addon can show a live countdown via GetServerTime()."""
    rl = rl or {}
    limit = rl.get("limitPerHour")
    spent = rl.get("pointsSpentThisHour")
    return {
        "chars": int(affordable),
        "limit": int(limit) if limit else None,
        "reset_at": int(time.time()) + int(rl.get("pointsResetIn") or 0),
        "ts": int(time.time()),
    }


def _status_lua(b):
    """Render the budget snapshot as a WarcraftLogsTipsStatus Lua global for the addon."""
    if not b:
        return ""
    out = ["\nWarcraftLogsTipsStatus = {",
           f"    chars_left = {b.get('chars', 0)},",
           f"    reset_at = {b.get('reset_at', 0)},",
           f"    updated = {b.get('ts', 0)},"]
    if b.get("limit"):
        out.append(f"    limit_per_hour = {b['limit']},")
    out.append("}\n")
    return "\n".join(out)


def flush(cfg, cache):
    """Write Data.lua (DB + budget status) from the full cache and persist cache.json."""
    _prune_old(cache)  # data TTL: forget characters not refreshed within DATA_TTL_DAYS
    save_cache(cache)  # cache.json is independent of the addon folder -> always persist
    ap = cfg.get("addon_path")
    if not ap or not os.path.isdir(ap):
        print("[!] addon folder not set — skipping Data.lua. Use “Addon folder…” to set it.")
        return
    out = os.path.join(ap, "Data.lua")
    text = F.render_data_lua(cache["data"], datetime.date.today().isoformat())
    text += _status_lua(cache.get("budget"))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)


def upload_to_hub(cfg, results):
    """Send freshly-fetched results to the central hub for aggregation. We send only RESULT
    data (parse %), never the WCL key -- so the hub pools data, not credentials. Best-effort:
    a hub outage never blocks local fetching."""
    url = cfg.get("hub_url")
    if not url or not results:
        return
    today = datetime.date.today().isoformat()
    # ts = precise fetch time so the hub keeps the NEWEST parse and an older one never
    # overwrites a newer one; `updated` (date) is what the addon shows in tooltips.
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    chars = {k: dict({"zones": v["zones"], "updated": today, "ts": ts},
                     **({"slug": v["slug"]} if v.get("slug") else {})) for k, v in results.items()}
    payload = {"chars": chars}
    who = F.read_scanner(cfg.get("savedvariables_path", ""))  # for the top-scanners leaderboard
    if who:
        payload["by"] = who
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/submit", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if cfg.get("hub_token"):
        req.add_header("X-Auth-Token", cfg["hub_token"])
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        print(f"    -> hub: +{r.get('added', 0)} new, {r.get('updated', 0)} updated "
              f"({r.get('total', '?')} total in pool).")
    except Exception as e:
        print(f"    -> hub upload skipped ({e}).")


def sync_from_hub(cfg, cache):
    """Seed/refresh the local DB from the community pool on the hub, so the addon shows the WHOLE
    pool (not just this user's own fetches) and the companion never overwrites the bundled DB
    with less. Adds pool characters that aren't already in the local cache. Returns count added."""
    url = cfg.get("hub_url")
    if not url:
        return 0
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/db", timeout=25) as r:
            pool = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"    hub sync skipped ({e})")
        return 0
    added = 0
    for k, e in (pool or {}).items():
        if isinstance(e, dict) and "zones" in e and k not in cache["data"]:
            cache["data"][k] = {"zones": e["zones"], "updated": e.get("updated")}
            cache["ts"][k] = e.get("ts") or e.get("updated") or ""  # so the data TTL can age it
            added += 1
    return added


def process(state):
    """Fetch new/stale queued characters (concurrently, within the key's hourly budget) and
    rewrite Data.lua from the full cache. Never raises -- a bad pass just retries next tick."""
    cfg, cache = state["cfg"], state["cache"]
    state["last_added"] = 0  # set per-pass; the tray notifies when > 0
    if state.get("stopped"):
        return  # paused via Stop & Clear; resume with the Fetch button
    queue = read_full_queue(cfg)
    stamps = F.read_queue_stamps(cfg.get("savedvariables_path", "")) if cfg.get("savedvariables_path") else {}
    qts_cache = cache.setdefault("qts", {})

    def needs(n, r):
        key = F.norm_key(n, r)
        # An explicit (re)queue — /wcl updateguild, queueguild, right-click "refresh" — is an
        # intentional "fetch this now": it overrides BOTH the freshness TTL and the no-data
        # cooldown. (Without this, characters once marked no-data are skipped for 7 days and
        # never update in bulk, even though a single right-click would fetch them.)
        qts = stamps.get(key)
        if qts is not None and qts_cache.get(key) != qts:
            return True
        if _nodata_cooling(cache, key):
            return False
        return is_stale(cache["ts"].get(key))

    todo = [(n, r, reg) for (n, r, reg) in queue if needs(n, r)]
    state["queue_total"] = len(queue)
    state["pending"] = len(todo)  # not-yet-fetched; shown in the window status
    if not todo:
        return

    # Only fetch what this client can afford this hour; defer the rest to a later pass.
    try:
        affordable, rl = F.affordable_chars(state["token"])
    except Exception:
        affordable, rl = len(todo), None
    cache["budget"] = budget_snapshot(affordable, rl)  # surfaced in-game via /reload
    if affordable <= 0:
        wait = int((rl or {}).get("pointsResetIn") or 0)
        print(f"[{now_iso()}] budget spent; {len(todo)} queued, waiting ~{wait}s for reset.")
        flush(cfg, cache)  # still update the in-game status (0 left, reset in N)
        return
    batch = todo[:affordable]
    extra = len(todo) - len(batch)
    print(f"[{now_iso()}] fetching {len(batch)} character(s)"
          + (f" (+{extra} deferred for budget)" if extra else "")
          + f" [concurrency={state['concurrency']}]...")

    def _on_each(n, r, ok):  # live: tick the queue counter down as each character completes
        print(f"    {n}-{r}: {'ok' if ok else 'no data'}")
        state["pending"] = max(0, state.get("pending", 0) - 1)

    try:
        new = F.fetch_roster(state["token"], state["zones"], state["metrics"], batch,
                             concurrency=state["concurrency"], on_each=_on_each,
                             abort=lambda: state.get("stopped"))
    except Exception as e:  # token expiry / network blip -> re-auth once, else retry next pass
        print(f"    fetch error ({e}); re-authenticating...")
        try:
            state["token"] = F.get_token(cfg)
            new = F.fetch_roster(state["token"], state["zones"], state["metrics"], batch,
                                 concurrency=state["concurrency"], on_each=_on_each,
                             abort=lambda: state.get("stopped"))
        except Exception as e2:
            print(f"    retry failed ({e2}); will try again next pass.")
            return

    # How many characters we just fetched this pass (the queued ones the user requested). The
    # tray toasts "fetched N — /reload" whenever this is > 0, even if they were already in the
    # pooled DB (the data was refreshed, so a /reload shows the newer parses).
    state["last_added"] = len(new)

    stamp = now_iso()
    cache.setdefault("nodata", {})
    for key, entry in new.items():
        cache["data"][key] = entry
        cache["ts"][key] = stamp
        cache["nodata"].pop(key, None)  # it has data now
    # Stamp EVERY attempted char (OVERWRITE, not setdefault) so it isn't re-fetched next pass.
    # Chars that returned no logs go on a NODATA_TTL_DAYS cooldown -> we stop hammering players
    # who simply have no parses. (Deferred-for-budget chars aren't in `batch`, so they retry.)
    for (n, r, reg) in batch:
        key = F.norm_key(n, r)
        cache["ts"][key] = stamp
        if key in stamps:
            qts_cache[key] = stamps[key]  # mark this queue-stamp handled (so we don't re-loop it)
        if key not in new:
            cache["nodata"][key] = stamp

    flush(cfg, cache)
    print(f"    -> DB now {len(cache['data'])} characters. /reload in-game to see new ones.")
    upload_to_hub(cfg, new)  # share results with the central pool (best-effort)
    print()


def discover_zones_resilient(cfg, token, cache):
    """Discover active zones, surviving an exhausted-budget cold start: reuse cached zones if
    the API is 429'd, else wait for the points reset and retry. Caches zones for next start."""
    while True:
        try:
            zones = F.discover_zones(token)
            cache["zones"] = zones  # remember so a future cold start works even at 0 budget
            save_cache(cache)
            return zones, token
        except F.RateLimited as e:
            if cache.get("zones"):
                print("    budget spent; using cached zones from last run.")
                return cache["zones"], token
            wait = max(30, min(int(e.reset_in or 300), 1800)) + 5
            print(f"[{now_iso()}] zone discovery blocked by budget; retrying in ~{wait}s...")
            time.sleep(wait)
            token = F.get_token(cfg)  # refresh in case it expired during the wait


def _pause():
    """Keep the console window open (so double-click users can read the message)."""
    try:
        input(S["close"])
    except EOFError:
        pass


def update_config(updates):
    """Merge `updates` into config.json in the per-user config dir, preserving other keys."""
    path = os.path.join(F.HERE, "config.json")
    data = {}
    if os.path.exists(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    data.update(updates)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path


def save_keys(cfg, cid, secret):
    """Persist the WCL key to the per-user config dir (NOT next to the .exe)."""
    cfg["client_id"], cfg["client_secret"] = cid, secret
    path = update_config({"client_id": cid, "client_secret": secret})
    print(f"keys saved -> {path}")
    return path


def prompt_keys_ctk(cfg):
    """Modern dark key-entry window (customtkinter). Returns True/False, or None if ctk is
    unavailable so the caller can fall back to plain Tk."""
    try:
        import customtkinter as ctk
        import webbrowser
    except Exception:
        return None

    import tkinter as _tk
    ACCENT, ACCENT_HOVER = "#8856ff", "#7445ee"
    out = {}
    ctk.set_appearance_mode("dark")
    # If the main window already exists, open a child Toplevel (a 2nd CTk root + nested mainloop
    # breaks Tk: entries/save misfire, so the key silently never saves). First launch has no root.
    _root = getattr(_tk, "_default_root", None)
    app = ctk.CTkToplevel(_root) if _root is not None else ctk.CTk()
    app.title(APP_NAME)
    app.geometry("460x440")
    app.resizable(False, False)
    try:  # gold "100" window icon (beat customtkinter's default, which is set on a timer)
        ico = F._bundled_path("icon.ico")
        if os.path.exists(ico):
            app.after(300, lambda: app.iconbitmap(ico))
    except Exception:
        pass

    frame = ctk.CTkFrame(app, fg_color="transparent")
    frame.pack(fill="both", expand=True, padx=28, pady=24)

    ctk.CTkLabel(frame, text=APP_NAME, font=ctk.CTkFont(size=22, weight="bold"),
                 text_color=ACCENT).pack(anchor="w")
    ctk.CTkLabel(frame, text=S["prompt_heading"],
                 font=ctk.CTkFont(size=13), text_color="#9aa0ad").pack(anchor="w", pady=(2, 14))

    ctk.CTkButton(frame, text=S["create_key"], height=34,
                  fg_color="transparent", border_width=1, border_color="#3a3f4b",
                  text_color="#cdd2da", hover_color="#23262e",
                  command=lambda: webbrowser.open(WCL_CLIENTS_URL)).pack(fill="x", pady=(0, 8))
    ctk.CTkLabel(frame, text=S["setup_sub"], font=ctk.CTkFont(size=12), justify="left",
                 text_color="#9aa0ad", wraplength=404).pack(anchor="w", pady=(0, 16))

    e_id = ctk.CTkEntry(frame, placeholder_text="Client ID", height=40,
                        corner_radius=10, border_color="#3a3f4b")
    e_id.pack(fill="x", pady=6)
    e_sec = ctk.CTkEntry(frame, placeholder_text="Client Secret", height=40, show="•",
                         corner_radius=10, border_color="#3a3f4b")
    e_sec.pack(fill="x", pady=6)

    err = ctk.CTkLabel(frame, text="", font=ctk.CTkFont(size=12), text_color="#ff6b6b")
    err.pack(anchor="w", pady=(2, 0))

    def do_save():
        cid, sec = e_id.get().strip(), e_sec.get().strip()
        if not (cid and sec):
            err.configure(text=S["fill_both"])
            return
        out["id"], out["secret"] = cid, sec
        app.destroy()

    ctk.CTkButton(frame, text=S["save_run"], height=42, corner_radius=10,
                  font=ctk.CTkFont(size=14, weight="bold"),
                  fg_color=ACCENT, hover_color=ACCENT_HOVER, command=do_save).pack(
        fill="x", pady=(16, 0))

    e_id.focus_set()
    app.bind("<Return>", lambda _e: do_save())
    app.attributes("-topmost", True)
    try:
        app.after(80, lambda: app.attributes("-topmost", False))
    except Exception:
        pass
    if _root is not None:
        app.transient(_root)
        app.after(120, app.grab_set)   # modal over the main window; no nested mainloop
        app.wait_window()
    else:
        app.mainloop()
    if out.get("id") and out.get("secret"):
        save_keys(cfg, out["id"], out["secret"])
        return True
    return False


def prompt_keys_tk(cfg):
    """Plain-Tk fallback key window. Returns True if saved, None if Tk unavailable."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        import webbrowser
    except Exception:
        return None
    out = {}
    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    tk.Label(root, text=S["prompt_heading"],
             font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2,
                                                  padx=14, pady=(14, 2), sticky="w")
    tk.Label(root, text=S["setup_sub"],
             fg="#555").grid(row=1, column=0, columnspan=2, padx=14, sticky="w")
    tk.Button(root, text=S["create_key"],
              command=lambda: webbrowser.open(WCL_CLIENTS_URL)).grid(
        row=2, column=0, columnspan=2, padx=14, pady=8, sticky="we")
    tk.Label(root, text="Client ID:").grid(row=3, column=0, padx=14, pady=4, sticky="e")
    e_id = tk.Entry(root, width=46)
    e_id.grid(row=3, column=1, padx=(0, 14), pady=4)
    tk.Label(root, text="Client Secret:").grid(row=4, column=0, padx=14, pady=4, sticky="e")
    e_sec = tk.Entry(root, width=46, show="•")
    e_sec.grid(row=4, column=1, padx=(0, 14), pady=4)

    def do_save():
        cid, sec = e_id.get().strip(), e_sec.get().strip()
        if not (cid and sec):
            messagebox.showwarning(APP_NAME, S["fill_both"])
            return
        out["id"], out["secret"] = cid, sec
        root.destroy()

    tk.Button(root, text=S["save_run"], command=do_save).grid(
        row=5, column=0, columnspan=2, padx=14, pady=(8, 14), sticky="we")
    e_id.focus_set()
    root.bind("<Return>", lambda _e: do_save())
    try:
        root.eval("tk::PlaceWindow . center")
    except Exception:
        pass
    root.attributes("-topmost", True)
    root.mainloop()
    if out.get("id") and out.get("secret"):
        save_keys(cfg, out["id"], out["secret"])
        return True
    return False


def prompt_for_keys(cfg):
    """Ask for the WCL key — modern window, then plain-Tk, then console. Returns True if saved."""
    for ui in (prompt_keys_ctk, prompt_keys_tk):
        try:
            res = ui(cfg)
        except Exception as e:
            print(f"key UI ({ui.__name__}) failed: {e}")
            res = None
        if res is not None:
            return res
    print("\n" + S["console_setup"])
    print("WCL key: " + WCL_CLIENTS_URL)
    try:
        cid = input("Client ID: ").strip()
        secret = input("Client Secret: ").strip()
    except EOFError:
        return False
    if not (cid and secret):
        return False
    save_keys(cfg, cid, secret)
    return True


def prompt_addon_path(cfg):
    """Ask the user to point at the WCLogsEye addon folder (used when auto-detect fails). Saves
    addon_path (and derives the SavedVariables path) into config.json. Returns True on success."""
    try:
        import customtkinter as ctk
        import tkinter as _tk
        from tkinter import filedialog
    except Exception:
        return False
    ACCENT = "#8856ff"
    out = {}
    ctk.set_appearance_mode("dark")
    _root = getattr(_tk, "_default_root", None)
    app = ctk.CTkToplevel(_root) if _root is not None else ctk.CTk()
    app.title(APP_NAME)
    app.geometry("580x320")
    app.resizable(False, False)
    try:
        ico = F._bundled_path("icon.ico")
        if os.path.exists(ico):
            app.after(300, lambda: app.iconbitmap(ico))
    except Exception:
        pass
    frame = ctk.CTkFrame(app, fg_color="transparent")
    frame.pack(fill="both", expand=True, padx=24, pady=20)
    ctk.CTkLabel(frame, text=S["ap_heading"], font=ctk.CTkFont(size=18, weight="bold"),
                 text_color=ACCENT).pack(anchor="w")
    ctk.CTkLabel(frame, text=S["ap_guide"], font=ctk.CTkFont(size=12), justify="left",
                 text_color="#9aa0ad", wraplength=528).pack(anchor="w", pady=(8, 14))
    e = ctk.CTkEntry(frame, height=38, corner_radius=10, border_color="#3a3f4b")
    if cfg.get("addon_path"):
        e.insert(0, cfg["addon_path"])
    e.pack(fill="x")
    err = ctk.CTkLabel(frame, text="", font=ctk.CTkFont(size=12), text_color="#ff6b6b")
    err.pack(anchor="w", pady=(4, 0))

    def browse():
        d = filedialog.askdirectory(title=S["ap_heading"])
        if d:
            e.delete(0, "end")
            e.insert(0, d)

    def save():
        p = e.get().strip().strip('"')
        if not p or not os.path.isdir(p):
            err.configure(text=S["ap_invalid"])
            return
        out["path"] = p
        app.destroy()

    row = ctk.CTkFrame(frame, fg_color="transparent")
    row.pack(fill="x", pady=(14, 0))
    ctk.CTkButton(row, text=S["ap_browse"], width=130, fg_color="transparent", border_width=1,
                  border_color="#3a3f4b", text_color="#cdd2da", hover_color="#23262e",
                  command=browse).pack(side="left")
    ctk.CTkButton(row, text=S["save_run"], fg_color=ACCENT, hover_color="#7445ee",
                  command=save).pack(side="right")
    app.attributes("-topmost", True)
    try:
        app.after(80, lambda: app.attributes("-topmost", False))
    except Exception:
        pass
    if _root is not None:
        app.transient(_root)
        app.after(120, app.grab_set)
        app.wait_window()
    else:
        app.mainloop()
    if out.get("path"):
        ap = out["path"]
        cfg["addon_path"] = ap
        updates = {"addon_path": ap}
        sv = F.sv_from_addon(ap)
        if sv:
            cfg["savedvariables_path"] = sv
            updates["savedvariables_path"] = sv
        update_config(updates)
        print(f"addon folder set -> {ap}" + (f"  (SavedVariables: {sv})" if sv else ""))
        return True
    return False


def build_state(cfg):
    """Authenticate, discover zones, build the shared state dict, and write an initial budget
    snapshot. Assumes the WCL key is already present in cfg. May raise on auth/network errors."""
    token = F.get_token(cfg)
    cache = load_cache()
    synced = sync_from_hub(cfg, cache)  # seed the local DB from the community pool
    if synced:
        print(f"synced {synced} characters from the community pool")
    zones, token = discover_zones_resilient(cfg, token, cache)
    state = {
        "cfg": cfg, "token": token, "zones": zones,
        "metrics": cfg.get("metrics") or ["dps", "hps"], "cache": cache,
        "concurrency": int(cfg.get("concurrency", F.DEFAULT_CONCURRENCY)),
    }
    try:  # write Data.lua (pool + budget status) so the in-game DB is restored right away
        aff, rl = F.affordable_chars(token)
        cache["budget"] = budget_snapshot(aff, rl)
    except Exception:
        pass
    flush(cfg, cache)
    return state


def watch_loop(state, stop_event=None, on_pass=None):
    """Process immediately, then re-process on queue-file change or every RETRY_SECONDS until
    stop_event is set. on_pass() (if given) is called after each process() for UI updates."""
    sv = state["cfg"].get("savedvariables_path", "")

    def cur_mtime():
        try:
            return os.path.getmtime(sv) if sv and os.path.exists(sv) else None
        except OSError:
            return None

    process(state)
    if on_pass:
        on_pass()
    last_mtime, last_run = cur_mtime(), time.time()
    while not (stop_event and stop_event.is_set()):
        mtime = cur_mtime()
        now = time.time()
        changed = (mtime != last_mtime)
        if changed or (now - last_run) >= RETRY_SECONDS:
            if changed:
                state["stopped"] = False  # /reload in-game resumes a paused companion
            last_mtime, last_run = mtime, now
            process(state)
            if on_pass:
                on_pass()
        time.sleep(POLL_SECONDS)


def main():
    """Console mode (dev / `python companion.py`)."""
    cfg = F.load_config()
    print(f"=== {APP_NAME} companion ===")
    if not (cfg.get("client_id") and cfg.get("client_secret")):
        prompt_for_keys(cfg)
    if not (cfg.get("client_id") and cfg.get("client_secret")):
        print("\n" + S["no_key"])
        _pause()
        return
    print("Authenticating to WarcraftLogs...")
    state = build_state(cfg)
    print("Zones:", ", ".join(z["label"] for z in state["zones"]))
    print(f"DB loaded: {len(state['cache']['data'])} characters cached.")
    print(f"Watching: {cfg.get('savedvariables_path')}   (concurrency={state['concurrency']})")
    print("In-game: right-click a player -> get data -> /reload. Ctrl+C to quit.\n")
    watch_loop(state)


# ---------------------------------------------------------------------------------------------
# Tray app (the packaged .exe): no console, system-tray icon, GUI key entry.
# ---------------------------------------------------------------------------------------------

def _play_success():
    """Play the 'done' chime (bundled success.wav, falling back to a system sound)."""
    try:
        import winsound
        p = F._bundled_path("success.wav")
        if os.path.exists(p):
            winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def _tray_image():
    """Tray icon: a bold gold "100" on a dark tile (matches the app icon)."""
    from PIL import Image, ImageDraw, ImageFont
    s, txt = 64, "100"
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, s - 2, s - 2), radius=12, fill=(24, 20, 34, 255),
                        outline=(120, 100, 40, 255), width=2)
    f = None
    for p in ("C:/Windows/Fonts/seguibl.ttf", "C:/Windows/Fonts/segoeuib.ttf",
              "C:/Windows/Fonts/arialbd.ttf"):
        try:
            f = ImageFont.truetype(p, 20)
            break
        except OSError:
            continue
    if f is None:
        f = ImageFont.load_default()
    bb = d.textbbox((0, 0), txt, font=f)
    d.text(((s - (bb[2] - bb[0])) / 2 - bb[0], (s - (bb[3] - bb[1])) / 2 - bb[1]),
           txt, font=f, fill=(255, 199, 26, 255))
    return img


_LOG_Q = queue.Queue()


class _QStream:
    """Write stream that mirrors output to the log file AND queues lines for the UI window."""
    def __init__(self, fh):
        self.fh = fh

    def write(self, d):
        if self.fh:
            try:
                self.fh.write(d)
                self.fh.flush()
            except Exception:
                pass
        if d:
            _LOG_Q.put(d)

    def flush(self):
        if self.fh:
            try:
                self.fh.flush()
            except Exception:
                pass


def run_app():
    """The packaged companion: a dark window with a live log, status line, a /reload banner and
    action buttons. The X button hides it to the system tray; the watcher runs in a thread."""
    import threading
    logpath = os.path.join(F._base_dir(), "companion.log")
    try:
        fh = open(logpath, "a", encoding="utf-8", buffering=1)
    except OSError:
        fh = None
    sys.stdout = sys.stderr = _QStream(fh)  # window has no console; capture output for the log
    print(f"\n=== {APP_NAME} v{APP_VERSION} — start {now_iso()} ===")

    import customtkinter as ctk
    test_mode = bool(os.environ.get("WCLOGS_UI_TEST"))

    cfg = F.load_config()
    if not test_mode and not (cfg.get("client_id") and cfg.get("client_secret")):
        if not prompt_for_keys(cfg):
            return
    # WoW not auto-found -> ask for the addon folder (otherwise Data.lua can't be written).
    if not test_mode and (not cfg.get("addon_path") or not os.path.isdir(cfg.get("addon_path", ""))):
        prompt_addon_path(cfg)

    ACCENT = "#ffc71a"
    ctk.set_appearance_mode("dark")
    win = ctk.CTk()
    win.title(f"{APP_NAME}  v{APP_VERSION}")
    win.geometry("960x520")
    win.minsize(940, 420)  # fits all 5 action buttons (140px each) + the sound checkbox, no clip
    try:
        ico = F._bundled_path("icon.ico")
        if os.path.exists(ico):
            # set after a beat — customtkinter installs its own default icon on a timer
            win.after(300, lambda: win.iconbitmap(ico))
    except Exception:
        pass

    top = ctk.CTkFrame(win, fg_color="transparent")
    top.pack(fill="x", padx=16, pady=(14, 0))
    ctk.CTkLabel(top, text=APP_NAME, font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=ACCENT).pack(side="left")
    ctk.CTkLabel(top, text=f"v{APP_VERSION}", font=ctk.CTkFont(size=12),
                 text_color="#7d7791").pack(side="left", padx=(8, 0), pady=(6, 0))
    # small "check updates" link next to the version (command wired once do_check_now exists)
    check_btn = ctk.CTkButton(top, text=S["check_upd"], width=1, height=24, corner_radius=7,
                              font=ctk.CTkFont(size=12), fg_color="transparent", border_width=1,
                              border_color="#3a3548", text_color="#9a93b0", hover_color="#23202e")
    check_btn.pack(side="left", padx=(12, 0), pady=(5, 0))
    status = ctk.CTkLabel(top, text="…", font=ctk.CTkFont(size=13), text_color="#9aa0ad")
    status.pack(side="right")

    keyline = ctk.CTkLabel(win, text="", font=ctk.CTkFont(family="Consolas", size=11),
                           text_color="#6b7080")
    keyline.pack(anchor="w", padx=16, pady=(2, 0))

    banner = ctk.CTkLabel(win, text="", font=ctk.CTkFont(size=14, weight="bold"),
                          fg_color="#2a2410", text_color=ACCENT, corner_radius=10, height=40)
    logbox = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
    logbox.pack(fill="both", expand=True, padx=16, pady=12)
    logbox.configure(state="disabled")
    bar = ctk.CTkFrame(win, fg_color="transparent")
    bar.pack(fill="x", padx=16, pady=(0, 14))

    stop = threading.Event()
    cmds = queue.Queue()                 # tray-thread -> UI-thread commands
    holder = {"state": None}
    ui = {"n": 0, "at": 0.0}

    def on_pass():
        st = holder["state"]
        if st and st.get("last_added", 0) > 0:
            ui["n"], ui["at"] = st["last_added"], time.time()
            if cfg.get("sound", True):
                _play_success()

    def do_fetch():
        st = holder["state"]
        if st:
            st["stopped"] = False  # resume if it was stopped
            threading.Thread(target=lambda: (process(st), on_pass()), daemon=True).start()

    def do_stop():
        st = holder["state"]
        if not st:
            return
        st["stopped"] = True  # soft pause: stops the current/auto fetching, NOT a hard clear
        st["pending"] = 0     # counter -> 0 right away; /reload re-reads the queue and resumes
        print(f"[{now_iso()}] Stopped — fetching paused. /reload in-game (or Fetch) resumes it. "
              f"To wipe the queue entirely, type /wcl clearqueue in-game.")

    def do_keys():
        if not prompt_for_keys(cfg):
            return
        st = holder["state"]
        try:
            tok = F.get_token(cfg)
        except Exception as e:
            print(f"re-auth failed: {e}")
            return
        if st:
            st["token"] = tok
            st["stopped"] = False                       # resume with the new key
            try:  # refresh the budget snapshot for the NEW key so the in-game count updates
                aff, rl = F.affordable_chars(tok)
                st["cache"]["budget"] = budget_snapshot(aff, rl)
                flush(st["cfg"], st["cache"])
            except Exception:
                pass
            threading.Thread(target=lambda: (process(st), on_pass()), daemon=True).start()
        print("key updated.")

    def do_data():
        try:
            os.startfile(F._base_dir())
        except Exception:
            pass

    def do_addon():
        if prompt_addon_path(cfg) and holder["state"]:
            flush(cfg, holder["state"]["cache"])  # write Data.lua to the new folder right away
            on_pass()
            print("addon folder updated; Data.lua written.")

    def mkbtn(text, cmd, primary=False):
        b = ctk.CTkButton(bar, text=text, command=cmd, height=34, corner_radius=9,
                          fg_color=(ACCENT if primary else "#2a2440"),
                          text_color=("#1a1305" if primary else "#e9e6f0"),
                          hover_color=("#e6b317" if primary else "#332b4d"))
        b.pack(side="left", padx=(0, 8))
        return b
    mkbtn(S["fetch"], do_fetch, primary=True)
    mkbtn(S["stop"], do_stop)
    mkbtn(S["keys"], do_keys)
    mkbtn(S["ap_set"], do_addon)
    mkbtn(S["data"], do_data)

    sound_var = ctk.BooleanVar(value=bool(cfg.get("sound", True)))
    def on_sound():
        cfg["sound"] = bool(sound_var.get())
        update_config({"sound": cfg["sound"]})
    ctk.CTkCheckBox(bar, text=S["sound"], variable=sound_var, command=on_sound,
                    onvalue=True, offvalue=False, checkbox_width=20, checkbox_height=20,
                    fg_color=ACCENT, hover_color="#e6b317").pack(side="right")

    # ---- "update available" button (hidden until a newer version is published; click to update) ----
    def do_update():
        print("updating…")
        if self_update():            # frozen: download, swap, relaunch -> close this instance
            quit_all()
        else:                        # dev / self-update failed -> open the download in a browser
            import webbrowser
            webbrowser.open(EXE_URL)
    # Own full-width row (not in the button bar) so it never crowds/overlaps the action buttons.
    update_btn = ctk.CTkButton(win, text="", command=do_update, height=36, corner_radius=9,
                               font=ctk.CTkFont(size=13, weight="bold"),
                               fg_color="#33d17a", text_color="#0a1f12", hover_color="#2bb869")
    upd = {"shown": False}

    def show_update(v):
        if upd["shown"]:
            return
        upd["shown"] = True
        update_btn.configure(text=S["update_avail"].format(cur=APP_VERSION, new=v))
        update_btn.pack(fill="x", padx=16, pady=(0, 8), before=logbox)
        print(f"update available: you have v{APP_VERSION}, latest is v{v} — click the green button to update.")

    def check_update():
        v = latest_version()
        if v and _ver_tuple(v) > _ver_tuple(APP_VERSION):
            try:
                win.after(0, lambda: show_update(v))   # just notify; user clicks to update
            except Exception:
                pass
        try:  # re-check every 30 min so a fresh release is noticed soon
            win.after(30 * 60 * 1000,
                      lambda: threading.Thread(target=check_update, daemon=True).start())
        except Exception:
            pass

    def do_check_now():  # manual "Check updates" button -> check now with explicit feedback
        print(S["checking"])

        def run():
            v = latest_version()
            if v and _ver_tuple(v) > _ver_tuple(APP_VERSION):
                win.after(0, lambda: show_update(v))
            else:
                print(S["up_to_date"].format(v=APP_VERSION) if v else S["check_failed"])
        threading.Thread(target=run, daemon=True).start()
    check_btn.configure(command=do_check_now)  # wire the header button now that the fn exists

    # ---- system tray (pystray, its own thread) ----
    tray = {"icon": None}
    try:
        import pystray
        from pystray import MenuItem as Item, Menu
        tray["icon"] = pystray.Icon("wclogseye", _tray_image(), f"{APP_NAME} v{APP_VERSION}", menu=Menu(
            Item(S["show"], lambda i=None, it=None: cmds.put("show"), default=True),
            Item(S["fetch"], lambda i=None, it=None: cmds.put("fetch")),
            Item(S["quit"], lambda i=None, it=None: cmds.put("quit")),
        ))
        tray["icon"].run_detached()
    except Exception as e:
        print(f"tray unavailable ({e}); closing the window will exit.")

    def hide_to_tray():
        if tray["icon"] is None:
            quit_all()
            return
        win.withdraw()
        try:
            tray["icon"].notify(S["min_hint"], APP_NAME)
        except Exception:
            pass

    def quit_all():
        stop.set()
        if tray["icon"]:
            try:
                tray["icon"].stop()
            except Exception:
                pass
        try:
            win.destroy()
        except Exception:
            pass

    win.protocol("WM_DELETE_WINDOW", hide_to_tray)  # X hides to tray instead of quitting

    def pump():
        chunk = []
        try:
            while True:
                chunk.append(_LOG_Q.get_nowait())
        except queue.Empty:
            pass
        if chunk:
            logbox.configure(state="normal")
            logbox.insert("end", "".join(chunk))
            try:
                if int(logbox.index("end-1c").split(".")[0]) > 800:
                    logbox.delete("1.0", "300.0")
            except Exception:
                pass
            logbox.see("end")
            logbox.configure(state="disabled")
        st = holder["state"]
        if st:
            b = st["cache"].get("budget") or {}
            n = len(st["cache"].get("data", {}))
            left = b.get("chars")
            q = st.get("pending", 0)
            status.configure(text=S["db"].format(n=n) + S["queued"].format(q=q) +
                             (S["left"].format(left=left) if left is not None else "") +
                             (S["paused"] if st.get("stopped") else ""))
            keyline.configure(text=S["key_id"].format(k=cfg.get("client_id") or "—"))
        if ui["at"] and time.time() - ui["at"] < 45:
            banner.configure(text="✅ " + S["notify"].format(n=ui["n"]))
            if not banner.winfo_manager():
                banner.pack(fill="x", padx=16, before=logbox)
        elif banner.winfo_manager():
            banner.pack_forget()
        try:
            while True:
                c = cmds.get_nowait()
                if c == "show":
                    win.deiconify(); win.lift()
                elif c == "fetch":
                    do_fetch()
                elif c == "quit":
                    quit_all(); return
        except queue.Empty:
            pass
        win.after(500, pump)

    def worker():
        if test_mode:
            holder["state"] = {"cache": {"data": {}, "budget": {}}}
            return
        # Retry startup instead of dying — WCL OAuth / network can blip (e.g. 502 Bad Gateway).
        while not stop.is_set():
            try:
                holder["state"] = build_state(cfg)
                break
            except Exception as e:
                print(f"startup error ({e}); retrying in 30s…")
                stop.wait(30)
        if holder["state"] and not stop.is_set():
            watch_loop(holder["state"], stop, on_pass=on_pass)

    threading.Thread(target=worker, daemon=True).start()
    if not test_mode:
        cleanup_old_exe()  # remove a leftover .old from a prior self-update
        threading.Thread(target=check_update, daemon=True).start()  # auto-update on a newer build
    if test_mode:
        win.after(1500, quit_all)
    pump()
    win.mainloop()
    quit_all()


if __name__ == "__main__":
    if getattr(sys, "frozen", False) or os.environ.get("WCLOGS_UI"):
        # Packaged .exe: windowed app (log + tray), no console.
        try:
            run_app()
        except Exception as e:
            print(f"\nfatal: {e}")
    else:
        # Dev: console watcher.
        try:
            main()
        except KeyboardInterrupt:
            print("\nCompanion stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"\nError: {e}")
            _pause()
            sys.exit(1)
