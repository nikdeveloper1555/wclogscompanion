#!/usr/bin/env python3
"""
fetch_parses.py — pulls WarcraftLogs parse data for every character queued by the
in-game addon and writes Data.lua back into the addon folder.

Flow:
  1. Read the addon's SavedVariables file -> extract the fetch queue (name/realm/region).
  2. Authenticate to the WarcraftLogs API (OAuth client_credentials).
  3. For each character, query zoneRankings (current zone).
  4. Write Data.lua keyed by "name-realm" (lowercase) so the addon can look it up.

Setup:
  pip install requests
  Edit config.json (copy from config.example.json) with your client_id / client_secret
  and the path to your WoW SavedVariables + AddOns folders.

Run:
  python fetch_parses.py
"""

import json
import os
import re
import sys
import time
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# Force UTF-8 stdout/stderr so non-ASCII character names (Cyrillic, accented Latin like
# "Nâylá") don't crash print() on Windows, whose legacy codepage (cp1251 on a Russian
# locale) can't encode them. Without this a single accented name aborts a whole fetch batch.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

def _base_dir():
    """Per-user dir for config.json / cache.json. For the packaged .exe this is
    %APPDATA%\\WarcraftLogsTips (NOT next to the .exe, which may sit in Downloads/Program
    Files); in dev it's the source dir so the repo's config.json is used as-is."""
    if getattr(sys, "frozen", False):
        root = (os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
                or os.path.expanduser("~"))
        d = os.path.join(root, "WarcraftLogsTips")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            return os.path.dirname(sys.executable)  # fallback if APPDATA isn't writable
    return os.path.dirname(os.path.abspath(__file__))


def _bundled_path(name):
    """A data file baked into the build (PyInstaller --add-data lands it in sys._MEIPASS),
    falling back to the source dir for non-frozen runs."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


HERE = _base_dir()


def read_scanner(savedvars_path):
    """The player's own Name-Realm (the addon stores it as WarcraftLogsTipsDB.me) so the hub
    can attribute scans for the 'top scanners' leaderboard. None if not found."""
    if not savedvars_path or not os.path.exists(savedvars_path):
        return None
    with open(savedvars_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'\[?"?me"?\]?\s*=\s*"([^"]*)"', text)
    return m.group(1).strip() if m and m.group(1).strip() else None


def read_wcl_keys(savedvars_path):
    """Pull WCL API keys the user entered in-game (stored under WarcraftLogsTipsDB.wcl in the
    addon SavedVariables). Returns {"client_id":..., "client_secret":...} or {}."""
    if not savedvars_path or not os.path.exists(savedvars_path):
        return {}
    with open(savedvars_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    block = re.search(r'\[?"?wcl"?\]?\s*=\s*{(.*?)}', text, re.S)
    scope = block.group(1) if block else ""
    out = {}
    for field in ("client_id", "client_secret"):
        m = re.search(r'\[?"?' + field + r'"?\]?\s*=\s*"([^"]*)"', scope)
        if m and m.group(1).strip():
            out[field] = m.group(1).strip()
    return out


def _wow_retail_dirs():
    """Candidate '..._retail_' folders: Battle.net registry install path first, then common
    drive locations. Windows-focused (that's where the packaged .exe runs)."""
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Blizzard Entertainment\World of Warcraft")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(winreg.QueryValueEx(k, "InstallPath")[0])
            except OSError:
                pass
    except ImportError:
        pass
    for drive in "CDEFGH":
        for base in (r"{0}:\World of Warcraft\_retail_", r"{0}:\Program Files (x86)\World of Warcraft\_retail_"):
            roots.append(base.format(drive))
    # registry InstallPath usually already points at _retail_; normalize + de-dup, keep existing
    seen, out = set(), []
    for r in roots:
        r = os.path.normpath(r)
        if r.lower() not in seen and os.path.isdir(r):
            seen.add(r.lower())
            out.append(r)
    return out


def detect_wow_paths():
    """Find (savedvariables_path, addon_path) by locating the WoW install and the account that
    actually has our SavedVariables. Returns {} if nothing usable is found."""
    import glob
    for retail in _wow_retail_dirs():
        addon = os.path.join(retail, "Interface", "AddOns", "WCLogsEye")
        svs = glob.glob(os.path.join(retail, "WTF", "Account", "*",
                                     "SavedVariables", "WCLogsEye.lua"))
        if svs:
            sv = max(svs, key=os.path.getmtime)  # most recently written account
            return {"savedvariables_path": sv, "addon_path": addon}
        if os.path.isdir(addon):  # addon installed but never run yet -> still usable for output
            return {"addon_path": addon}
    return {}


def sv_from_addon(addon_path):
    """Given the addon folder (...\\Interface\\AddOns\\WCLogsEye), find its SavedVariables file,
    so a manually-set addon path also fixes the queue/keys read. '' if not found."""
    import glob
    if not addon_path:
        return ""
    retail = os.path.abspath(os.path.join(addon_path, "..", "..", ".."))  # -> ..._retail_
    svs = glob.glob(os.path.join(retail, "WTF", "Account", "*", "SavedVariables", "WCLogsEye.lua"))
    return max(svs, key=os.path.getmtime) if svs else ""


def ensure_keys(cfg):
    """Stop with a friendly message if no WCL key is available (config or in-game)."""
    if not (cfg.get("client_id") and cfg.get("client_secret")):
        sys.exit("No WCL API key found.\n"
                 "  In WoW: /wcl config -> paste your Client ID + Secret, Save, then /reload.\n"
                 "  (Get keys at https://www.warcraftlogs.com/api/clients/)")


def load_config():
    # config.json is optional: the packaged .exe works with zero config (keys come from the
    # addon's in-game settings, WoW folders are auto-detected).
    cfg = {}
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)

    # Built-in hub defaults baked into the build (so testers needn't configure a hub_url/token).
    # hub_defaults.json is gitignored and bundled via PyInstaller --add-data; config.json wins.
    try:
        dp = _bundled_path("hub_defaults.json")
        if os.path.exists(dp):
            with open(dp, "r", encoding="utf-8") as fh:
                defaults = json.load(fh)
            for k in ("hub_url", "hub_token"):
                if defaults.get(k) and not cfg.get(k):
                    cfg[k] = defaults[k]
    except (OSError, ValueError):
        pass

    # Auto-detect WoW folders when not set in config (so the packaged .exe just works).
    if not cfg.get("addon_path") or not os.path.isdir(cfg.get("addon_path", "")):
        found = detect_wow_paths()
        for k, v in found.items():
            cfg.setdefault(k, v)
            cfg[k] = cfg.get(k) or v
        if found:
            print(f"Auto-detected WoW: {found.get('addon_path', '?')}")

    # If config.json has no real keys, fall back to the ones the user entered in-game
    # (the addon's settings panel writes them into SavedVariables).
    cid = cfg.get("client_id") or ""
    if not cid or "PUT_YOUR" in cid:
        keys = read_wcl_keys(cfg.get("savedvariables_path", ""))
        if keys.get("client_id") and keys.get("client_secret"):
            cfg["client_id"] = keys["client_id"]
            cfg["client_secret"] = keys["client_secret"]
            print("Using WCL keys from in-game settings (SavedVariables).")
    return cfg


def get_token(cfg):
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(cfg["client_id"], cfg["client_secret"]),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# One full character fetch (raid mythic + M+ zone/encounter rankings, dps+hps, combined into a
# single request) costs ~37 points; budget at 40 for safety. The hard ceiling is per client_id.
# ~37 pts for the single combined query; chars whose Mythic raid is empty need a 2nd
# (Heroic/Normal) request, pushing the measured average to ~45/char. Budget at 50 for headroom.
POINTS_PER_CHAR = 50
# In-flight requests per key. WCL documents only a points/hour ceiling, but it ALSO enforces an
# undocumented request-rate cap (users get 429 after a few hundred requests, with Retry-After) —
# it's per IP, so swapping keys doesn't help. Keep concurrency low + pace request starts to stay
# under it. Tune via config.json "concurrency".
DEFAULT_CONCURRENCY = 4
PACE_SECONDS = 0.25  # min spacing between request starts (~4/s) to dodge the burst/IP 429


class RateLimited(Exception):
    """Raised when WCL returns HTTP 429. Carries seconds until the points bucket resets."""
    def __init__(self, reset_in=None):
        self.reset_in = reset_in
        super().__init__("WCL rate limit (429)")


RATE_QUERY = "{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }"


def get_rate_limit(token):
    resp = requests.post(API_URL, json={"query": RATE_QUERY},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code == 429:
        # When the hourly budget is fully spent, even this meta-query is 429'd -> bucket empty.
        reset = resp.headers.get("X-RateLimit-Reset") or resp.headers.get("Retry-After")
        raise RateLimited(int(reset) if reset and str(reset).isdigit() else None)
    resp.raise_for_status()
    return resp.json()["data"]["rateLimitData"]


def affordable_chars(token, safety=0.9):
    """How many characters this client can fetch right now without busting its hourly ceiling.
    Returns (count, rate_limit_dict). A 429 on the budget query itself means the bucket is
    empty -> (0, {...}) with the reset time, so callers can defer instead of hammering."""
    try:
        rl = get_rate_limit(token)
    except RateLimited as e:
        return 0, {"limitPerHour": None, "pointsSpentThisHour": None,
                   "pointsResetIn": e.reset_in or 0}
    remaining = (rl.get("limitPerHour") or 0) - (rl.get("pointsSpentThisHour") or 0)
    return max(0, int((remaining * safety) // POINTS_PER_CHAR)), rl


def read_queue(savedvars_path):
    """Extract queued players from the addon SavedVariables Lua file.

    The addon stores queue entries as table keys: ["Name\tRealm\tregion"] = true
    We don't need a full Lua parser -- just pull those keys out.
    """
    if not os.path.exists(savedvars_path):
        print(f"[!] SavedVariables not found yet: {savedvars_path}")
        print("    Log into the character once (and hover someone) so the game writes it.")
        return []
    with open(savedvars_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    # Only look inside the queue = { ... } block to avoid grabbing 'seen' keys.
    # Match the queue table up to its closing "}," line; order-independent (seen may come
    # before OR after queue). Queue values are plain `true`, so there are no nested braces.
    qblock = re.search(r'\[?"?queue"?\]?\s*=\s*{(.*?)\n\s*},?', text, re.S)
    scope = qblock.group(1) if qblock else text

    # Keys are serialized by WoW as ["Name<TAB>Realm<TAB>region"] with REAL tab chars (0x09),
    # so match an actual tab, not a literal backslash-t.
    entries = []
    for m in re.finditer(r'\["([^"]*?)\t([^"]*?)\t([^"]*?)"\]', scope):
        name, realm, region = m.group(1), m.group(2), m.group(3)
        if "|" in name or "|" in realm:
            continue  # WoW escape token (e.g. protected LFG name "|Kj18|k") -> not a real char
        entries.append((name, realm, region))
    return entries


def read_queue_stamps(savedvars_path):
    """{norm_key: queue_timestamp} for queued players. The companion uses this to notice an
    explicit (re)queue — e.g. /wcl updateguild — and re-fetch even when the cached data still
    looks fresh (queue value is GetServerTime(); legacy `true` entries have no stamp)."""
    out = {}
    if not os.path.exists(savedvars_path):
        return out
    try:
        text = open(savedvars_path, encoding="utf-8").read()
    except OSError:
        return out
    qblock = re.search(r'\[?"?queue"?\]?\s*=\s*{(.*?)\n\s*},?', text, re.S)
    scope = qblock.group(1) if qblock else text
    for m in re.finditer(r'\["([^"]*?)\t([^"]*?)\t[^"]*?"\]\s*=\s*(\d+)', scope):
        out[norm_key(m.group(1), m.group(2))] = int(m.group(3))
    return out


RAID_DIFFICULTIES = [(5, "Mythic"), (4, "Heroic"), (3, "Normal")]  # high -> low


def build_query(zones, metrics, raid_diffs=None):
    """One request per character: every (zone[, difficulty], metric) as a GraphQL alias.

    `raid_diffs` = list of raid difficulty ids to probe (default just Mythic). We default
    to Mythic only because zoneRankings always defaults to Mythic anyway and does NOT fall
    back to lower modes; lower difficulties are fetched lazily only when Mythic is empty."""
    if raid_diffs is None:
        raid_diffs = [5]  # Mythic first; cascade to Heroic/Normal only if needed
    fields = []
    for z in zones:
        if z["kind"] == "Raid":
            for d in raid_diffs:
                for m in metrics:
                    fields.append(
                        f'z{z["id"]}_d{d}_{m}: zoneRankings(zoneID: {z["id"]}, difficulty: {d}, metric: {m})')
        else:
            # M+: byBracket:true ranks each result WITHIN its keystone level — this matches the
            # WarcraftLogs character page. WITHOUT it, rankPercent compares a +20 run against ALL
            # keys (mostly lower) and inflates to ~99. zoneRankings = headline avg; encounterRankings
            # = per-dungeon runs (highest logged key + its parse).
            for m in metrics:
                fields.append(f'z{z["id"]}_{m}: zoneRankings(zoneID: {z["id"]}, metric: {m}, byBracket: true)')
                for enc in z["encounters"]:
                    fields.append(
                        f'e{enc["id"]}_{m}: encounterRankings(encounterID: {enc["id"]}, metric: {m}, byBracket: true)')
    inner = "\n      ".join(fields)
    return (
        "query($name: String!, $server: String!, $region: String!) {\n"
        "  characterData {\n"
        "    character(name: $name, serverSlug: $server, serverRegion: $region) {\n"
        "      name classID\n"
        f"      {inner}\n"
        "    }\n  }\n}"
    )

ZONES_QUERY = """
query($exp: Int!) {
  worldData {
    expansions { id }
    expansion(id: $exp) {
      name
      zones { id name frozen encounters { id name } }
    }
  }
}
"""


def discover_zones(token):
    """Find the active raid + Mythic+ zones for the CURRENT expansion automatically,
    so we don't hardcode IDs that change every season/patch.

    Returns a list of dicts: {"id": int, "label": str}.
    Active = not frozen, and not a PTR/Beta test zone.
    """
    def _zones_query(exp_id):
        resp = requests.post(API_URL, json={"query": ZONES_QUERY, "variables": {"exp": exp_id}},
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code == 429:  # budget spent -> even worldData is 429'd
            reset = resp.headers.get("X-RateLimit-Reset") or resp.headers.get("Retry-After")
            raise RateLimited(int(reset) if reset and str(reset).isdigit() else None)
        resp.raise_for_status()
        return resp.json()["data"]["worldData"]

    # First find the highest expansion id (= current expansion).
    exp_ids = [e["id"] for e in _zones_query(0)["expansions"]]
    current = max(exp_ids)
    exp = _zones_query(current)["expansion"]

    zones = []
    for z in exp["zones"]:
        name = z["name"]
        if z["frozen"]:
            continue
        if "PTR" in name or "Beta" in name:
            continue
        kind = "Mythic+" if "Mythic+" in name else "Raid"
        zones.append({"id": z["id"], "label": name, "kind": kind,
                      "encounters": z.get("encounters") or []})
    # Raid first, then M+
    zones.sort(key=lambda z: 0 if z["kind"] == "Raid" else 1)
    return zones


def _deaccent(s):
    """Strip diacritics for the WCL slug — Latin AND Cyrillic. WCL folds accents to base letters
    in realm slugs: Pozzo dell'Eternità -> pozzo-delleternita, and Ревущий фьорд -> ревущии-фьорд
    (й->и) and ё->е. NFKD splits the diacritic off the base letter, then we drop combining marks."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def slugify_realm(realm):
    # WCL realm slug: lowercase, diacritics stripped (Latin + Cyrillic), words joined by hyphens,
    # apostrophes dropped. In-world realms arrive spaced ("Argent Dawn"); LFG gives them
    # CamelCased ("ArgentDawn") -> insert a hyphen before each interior uppercase.
    s = _deaccent(realm.strip())
    if " " not in s:
        s = re.sub(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])", "-", s)
    return s.lower().replace(" ", "-").replace("'", "")


def norm_key(name, realm):
    """Canonical addon key: lowercase, strip spaces/apostrophes/hyphens/parens, join with '-'.
    MUST stay identical to MakeKey() in Core.lua so in-game lookups match."""
    def n(s):
        return re.sub(r"[ '\-()]", "", (s or "").lower())
    return f"{n(name)}-{n(realm)}"


def _slug_candidates(realm):
    """Plausible WCL realm slugs, best-guess first. Cyrillic/connected-realm slugs are NOT
    reliably derivable (spaced realms use 'a-b'; connected realms arrive concatenated CamelCase;
    WCL may fold й->и/ё->е OR keep them), so we try a few forms and use whichever WCL accepts."""
    r = realm.strip()
    out, seen = [], set()

    def add(s):
        s = s.lower().replace("'", "").replace("’", "")  # drop straight + curly apostrophes
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    for variant in (_deaccent(r), r):                       # accent-folded first, then literal
        camel = re.sub(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])", "-", variant)  # ArgentDawn -> Argent-Dawn
        add(camel.replace(" ", "-"))                        # hyphenated (covers spaced + CamelCase)
        add(variant.replace(" ", "-"))                      # spaced -> hyphen
        add(variant.replace(" ", ""))                       # concatenated (no hyphen)
    return out


def fetch_character(token, name, realm, region, query):
    last_err = None
    for server in _slug_candidates(realm):
        resp = requests.post(
            API_URL,
            json={"query": query, "variables": {"name": name, "server": server, "region": region}},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code == 429:
            reset = resp.headers.get("X-RateLimit-Reset") or resp.headers.get("Retry-After")
            raise RateLimited(int(reset) if reset and str(reset).isdigit() else None)
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            last_err = payload["errors"]
            continue
        char = payload.get("data", {}).get("characterData", {}).get("character")
        if char:
            char["_slug"] = server  # the slug WCL accepted -> stored for the in-game URL
            return char
    if last_err:
        print(f"    [api error] {name}-{realm}: {last_err}")
    else:
        print(f"    [no data] {name}-{realm} ({region}) not found on WCL")
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# Role is derivable from the WCL spec NAME (ambiguous names like "Holy"/"Restoration"/
# "Protection" share the same role across classes, so this is safe for role detection).
HEALER_SPECS = {"Holy", "Discipline", "Restoration", "Mistweaver", "Preservation"}
TANK_SPECS = {"Protection", "Blood", "Guardian", "Brewmaster", "Vengeance"}


def _is_dps_spec(spec):
    return bool(spec) and spec not in HEALER_SPECS and spec not in TANK_SPECS


def _bosses(zr, mplus=False, role_metric=None):
    import math
    out = []
    for r in (zr.get("rankings") or []):
        if not _num(r.get("totalKills")):
            continue  # never killed -> shown as "-" on the site, skip
        # Map WCL's logged spec to the role this metric is for: keep a boss row only if the
        # parse was set by a matching spec. Drops a healer/tank's damage parse AND a pure
        # DPS's incidental healing (e.g. a Subtlety rogue's self-heal ranked as HPS).
        if role_metric and not _role_match(r.get("bestSpec") or r.get("spec"), role_metric):
            continue
        enc = r.get("encounter") or {}
        b = {
            "name": enc.get("name", "?"),
            "pct": int(math.floor(_num(r.get("rankPercent")))),  # site floors percentiles
            "amount": int(round(_num(r.get("bestAmount")))),
            "kills": int(_num(r.get("totalKills"))),
            "rank": int(_num((r.get("allStars") or {}).get("rank"))),
        }
        if mplus:
            br = r.get("bestRank") or {}
            b["key"] = int(_num(br.get("ilvl")))      # keystone level of the best run
            b["score"] = int(round(_num(br.get("score"))))
        out.append(b)
    return out


def build_raid_block(zr, metric, diff_label):
    """Raid block for one metric at one difficulty. None if no kills at this difficulty,
    or if no boss row was set by a spec matching this metric's role (so a pure DPS gets no
    HPS block, and a healer gets no off-role DPS block)."""
    zr = zr or {}
    bosses = _bosses(zr, role_metric=metric)
    if not bosses:
        return None
    return {
        "metric": metric,
        "diff": diff_label,
        "best": round(zr.get("bestPerformanceAverage") or 0, 1),
        "median": round(zr.get("medianPerformanceAverage") or 0, 1),
        "bosses": bosses,
    }


def _role_match(spec, metric):
    """hps block -> healer specs. dps block -> every NON-healer (damage dealers AND tanks):
    WCL has no separate tank metric, it ranks tanks under `dps` (low %), so excluding tank specs
    here dropped tanks entirely ('no data' for a Guardian/Blood/etc.). Include them."""
    if metric == "hps":
        return spec in HEALER_SPECS
    return bool(spec) and spec not in HEALER_SPECS


def build_mplus_block(zone, char, metric):
    """Mythic+ block for one metric, built from per-dungeon encounterRankings.

    Per dungeon we take the HIGHEST LOGGED keystone level run done in this role and show its
    parse. The block best/median are the AVERAGE of the per-dungeon Best%/Median% (across the
    runs at that highest key) — exactly how the WCL character page computes them. We do NOT use
    zoneRankings' bestPerformanceAverage: for M+ it aggregates differently and reads way too high
    (e.g. 97 where the per-dungeon average is 66). Dungeons never run in this role show "-"."""
    import math
    import statistics
    bosses = []
    best_pcts, med_pcts = [], []
    for enc in zone["encounters"]:
        er = char.get(f"e{enc['id']}_{metric}") or {}
        ranks = [r for r in (er.get("ranks") or [])
                 if _role_match(r.get("bestSpec") or r.get("spec"), metric)
                 and _num(r.get("bracketData")) > 0]
        if ranks:
            maxkey = max(_num(r.get("bracketData")) for r in ranks)  # highest key completed
            at = [r for r in ranks if _num(r.get("bracketData")) == maxkey]
            pcts = [_num(r.get("rankPercent")) for r in at]
            top = max(at, key=lambda r: _num(r.get("rankPercent")))  # best run at that key
            bosses.append({
                "name": enc["name"],
                "key": int(maxkey),
                "pct": int(math.floor(max(pcts))),       # per-dungeon Best%
                "score": int(round(_num(top.get("score")))),
            })
            best_pcts.append(max(pcts))
            med_pcts.append(statistics.median(pcts))     # per-dungeon Median%
        else:
            bosses.append({"name": enc["name"], "key": 0, "pct": -1})  # "-" : no role log
    if not best_pcts:
        return None
    return {
        "metric": metric,
        "best": round(sum(best_pcts) / len(best_pcts), 1),
        "median": round(sum(med_pcts) / len(med_pcts), 1),
        "bosses": bosses,
    }


def build_raid_zone_blocks(char, zone_id, metrics, diffs):
    """Per metric, take the highest difficulty (in `diffs` order) that has data."""
    label_of = dict(RAID_DIFFICULTIES)
    blocks = []
    for metric in metrics:
        for d in diffs:
            blk = build_raid_block(char.get(f"z{zone_id}_d{d}_{metric}"), metric, label_of[d])
            if blk:
                blocks.append(blk)
                break  # stop at the highest mode with data
    return blocks


def build_mplus_zone_blocks(char, zone, metrics):
    blocks = []
    for metric in metrics:
        blk = build_mplus_block(zone, char, metric)
        if blk:
            blocks.append(blk)
    return blocks


# The Data.lua serializer lives in a dependency-free module so the hub can use it without
# pulling the WCL client's `requests` dependency. Re-export for existing F.* call sites.
from lua_export import lua_escape, render_data_lua, write_data_lua  # noqa: E402,F401


def fetch_one(token, name, realm, region, zones, query_mythic, fallback_query, metrics):
    """Fetch a single character -> entry dict {"zones": [...]} or None."""
    char = fetch_character(token, name, realm, region, query_mythic)
    if not char:
        return None
    zone_blocks = {}
    raid_need = []
    for z in zones:
        if z["kind"] == "Raid":
            blocks = build_raid_zone_blocks(char, z["id"], metrics, [5])
            zone_blocks[z["id"]] = blocks
            if not blocks:
                raid_need.append(z)
        else:
            zone_blocks[z["id"]] = build_mplus_zone_blocks(char, z, metrics)

    if raid_need and fallback_query:
        char2 = fetch_character(token, name, realm, region, fallback_query)
        if char2:
            for z in raid_need:
                zone_blocks[z["id"]] = build_raid_zone_blocks(char2, z["id"], metrics, [4, 3])

    char_zones = [
        {"label": z["label"], "kind": z["kind"], "blocks": zone_blocks[z["id"]]}
        for z in zones if zone_blocks.get(z["id"])
    ]
    # slug = the slug WCL actually accepted (from the candidate probe), so the addon builds a
    # working character URL. Falls back to the computed slug if somehow absent.
    slug = char.get("_slug") or slugify_realm(realm)
    return {"zones": char_zones, "slug": slug} if char_zones else None


def make_queries(zones, metrics):
    """Build the (mythic-first) query + raid Heroic/Normal fallback query for a zone set."""
    query_mythic = build_query(zones, metrics, raid_diffs=[5])
    raid_zones = [z for z in zones if z["kind"] == "Raid"]
    fallback_query = build_query(raid_zones, metrics, raid_diffs=[4, 3]) if raid_zones else None
    return query_mythic, fallback_query


def fetch_roster(token, zones, metrics, queue, on_each=None,
                 concurrency=DEFAULT_CONCURRENCY, budget=None, abort=None):
    """Fetch `queue` concurrently (up to `concurrency` requests in flight) to push one WCL
    client toward its hourly points ceiling. Returns {key: entry}.

    `budget` (max chars affordable this hour) clamps the queue so we never blow past the limit.
    On a 429 we stop launching new work and return what completed -- unfetched chars stay stale
    and are retried next pass. on_each(name, realm, ok) fires per character (main thread)."""
    query_mythic, fallback_query = make_queries(zones, metrics)
    if budget is not None and budget < len(queue):
        queue = queue[:max(0, budget)]
    data = {}
    stop = threading.Event()  # tripped on the hourly ceiling so queued workers bail out fast

    # Global pacer: never start requests closer than PACE_SECONDS apart, across all workers, so
    # we stay under WCL's undocumented per-IP burst cap even at higher concurrency.
    pace_lock = threading.Lock()
    next_start = [0.0]

    def pace():
        with pace_lock:
            now = time.monotonic()
            wait = next_start[0] - now
            if wait > 0:
                time.sleep(wait)
            next_start[0] = max(now, next_start[0]) + PACE_SECONDS

    def work(item):
        name, realm, region = item
        # A 429 can mean two different things: the hourly points ceiling (hard -> stop the
        # whole batch) OR a momentary burst/concurrency cap (transient -> short backoff + retry
        # the SAME char). The batch is already clamped to the affordable count, so a 429 here is
        # most likely burst; we retry a few times before giving up and treating it as the cap.
        delay = 1.0
        for attempt in range(4):
            if stop.is_set() or (abort and abort()):  # ceiling hit, or user pressed Stop
                return name, realm, None, False
            try:
                pace()  # throttle request starts to stay under the burst/IP cap
                entry = fetch_one(token, name, realm, region, zones,
                                  query_mythic, fallback_query, metrics)
                return name, realm, entry, False
            except RateLimited as rl:
                if attempt < 3:
                    time.sleep(rl.reset_in if (rl.reset_in and rl.reset_in <= 5) else delay)
                    delay *= 2
                    continue
                stop.set()  # repeated 429 -> hourly ceiling reached, stop launching more work
                return name, realm, None, True
        return name, realm, None, False

    limited = False
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(work, it) for it in queue]
        for fut in as_completed(futures):
            name, realm, entry, hit = fut.result()
            if hit:
                limited = True
                continue
            if stop.is_set():
                continue  # batch aborted by a 429; skip stragglers
            if entry:
                data[norm_key(name, realm)] = entry
            if on_each:
                on_each(name, realm, bool(entry))
    if limited:
        print("    [rate limit] 429 hit -> stopped early; "
              f"{len(data)} fetched, rest retried next pass.")
    return data


def main():
    cfg = load_config()
    metrics = cfg.get("metrics") or [cfg.get("metric", "dps")]

    # Merge: characters listed manually in config + the in-game hover queue.
    queue = []
    for c in cfg.get("manual_characters", []):
        queue.append((c["name"], c["realm"], c.get("region", "eu")))
    sv_path = cfg.get("savedvariables_path", "")
    if sv_path and "YOURACCOUNT" not in sv_path:
        queue.extend(read_queue(sv_path))

    queue = list(dict.fromkeys(queue))  # dedupe
    if not queue:
        print("Nothing to fetch. Add manual_characters in config.json or hover players in-game.")
        return

    ensure_keys(cfg)
    print("Authenticating to WarcraftLogs...")
    token = get_token(cfg)
    zones = discover_zones(token)
    print("Active zones: " + ", ".join(f'{z["label"]} (#{z["id"]})' for z in zones))

    concurrency = int(cfg.get("concurrency", DEFAULT_CONCURRENCY))
    affordable, rl = affordable_chars(token)
    if affordable < len(queue):
        print(f"Budget: {rl['pointsSpentThisHour']:.0f}/{rl['limitPerHour']} pts spent; "
              f"can fetch {affordable} of {len(queue)} now (resets in ~{rl['pointsResetIn']}s).")

    print(f"Fetching up to {min(affordable, len(queue))} character(s) "
          f"[metrics={metrics}, concurrency={concurrency}]...")
    data = fetch_roster(token, zones, metrics, queue, concurrency=concurrency, budget=affordable,
                        on_each=lambda n, r, ok: print(f"  - {n}-{r} {'ok' if ok else 'NO DATA'}"))

    out = os.path.join(cfg["addon_path"], "Data.lua")
    write_data_lua(out, data, datetime.date.today().isoformat())
    print(f"\nWrote {len(data)} entries -> {out}")
    print("Now run /reload in-game to see the data.")


if __name__ == "__main__":
    main()
