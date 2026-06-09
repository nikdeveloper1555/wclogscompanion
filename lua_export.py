"""
lua_export.py — serialize the character DB to a Data.lua string.

Kept dependency-free (no `requests`) and separate from the WCL client so the hub can produce
Data.lua without pulling the whole fetcher's dependencies.
"""


def lua_escape(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def render_data_lua(data, updated):
    """Serialize the character DB to a Data.lua string. Each entry may carry its own
    "updated" date (used by the hub, where data comes from many users at different times);
    otherwise the passed-in `updated` is used."""
    lines = ["-- AUTO-GENERATED. Do not edit by hand.",
             "WarcraftLogsTipsData = {"]
    for key, entry in data.items():
        lines.append(f'    ["{lua_escape(key)}"] = {{')
        lines.append(f'        updated = "{lua_escape(entry.get("updated") or updated)}",')
        if entry.get("slug"):  # WCL realm slug, for building the character URL in-game
            lines.append(f'        slug = "{lua_escape(entry["slug"])}",')
        lines.append('        zones = {')
        for zone in entry["zones"]:
            lines.append('            {')
            lines.append(f'                label = "{lua_escape(zone["label"])}",')
            lines.append(f'                kind  = "{lua_escape(zone["kind"])}",')
            lines.append('                blocks = {')
            for blk in zone["blocks"]:
                lines.append('                    {')
                lines.append(f'                        metric = "{lua_escape(blk["metric"])}",')
                if blk.get("diff"):
                    lines.append(f'                        diff   = "{lua_escape(blk["diff"])}",')
                lines.append(f'                        best   = {blk["best"]},')
                lines.append(f'                        median = {blk["median"]},')
                lines.append('                        bosses = {')
                for b in blk["bosses"]:
                    parts = ['name = "%s"' % lua_escape(b["name"]), "pct = %d" % b["pct"]]
                    for fld in ("key", "score", "amount", "kills", "rank"):
                        if fld in b:
                            parts.append("%s = %d" % (fld, b[fld]))
                    lines.append("                            { " + ", ".join(parts) + " },")
                lines.append('                        },')
                lines.append('                    },')
            lines.append('                },')
            lines.append('            },')
        lines.append('        },')
        lines.append('    },')
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_data_lua(path, data, updated):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_data_lua(data, updated))
