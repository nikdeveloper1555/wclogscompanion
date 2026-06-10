"""
lua_export.py — serialize the character DB to a Data.lua string.

Kept dependency-free (no `requests`) and separate from the WCL client so the hub can produce
Data.lua without pulling the whole fetcher's dependencies.
"""


def lua_escape(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def render_data_lua(data, updated):
    """Serialize the character DB to a Data.lua string — COMPACT (one line per character, no
    indentation) to keep the addon small at scale. Lua parses it identically to a pretty-printed
    table, so the addon needs no changes. Field names are preserved (best/median/metric/name/pct/
    key/kills/rank/diff/label/kind/zones/blocks/bosses/slug/updated). `amount` and `score` are
    intentionally dropped — the addon never reads them (dead weight at thousands of entries).
    Each entry may carry its own "updated" date; otherwise the passed-in `updated` is used."""
    out = ["-- AUTO-GENERATED. Do not edit by hand.", "WarcraftLogsTipsData = {"]
    for key, entry in data.items():
        parts = ['updated="%s"' % lua_escape(entry.get("updated") or updated)]
        if entry.get("slug"):  # WCL realm slug, for building the character URL in-game
            parts.append('slug="%s"' % lua_escape(entry["slug"]))
        zbits = []
        for zone in entry["zones"]:
            blkbits = []
            for blk in zone["blocks"]:
                bosses = []
                for b in blk["bosses"]:
                    bp = ['name="%s"' % lua_escape(b["name"]), "pct=%d" % b["pct"]]
                    for fld in ("key", "kills", "rank"):  # dropped: amount, score (unused by addon)
                        if fld in b:
                            bp.append("%s=%d" % (fld, b[fld]))
                    bosses.append("{" + ",".join(bp) + "}")
                blkparts = ['metric="%s"' % lua_escape(blk["metric"])]
                if blk.get("diff"):
                    blkparts.append('diff="%s"' % lua_escape(blk["diff"]))
                blkparts.append("best=%s" % blk["best"])
                blkparts.append("median=%s" % blk["median"])
                blkparts.append("bosses={%s}" % ",".join(bosses))
                blkbits.append("{" + ",".join(blkparts) + "}")
            zbits.append('{label="%s",kind="%s",blocks={%s}}'
                         % (lua_escape(zone["label"]), lua_escape(zone["kind"]), ",".join(blkbits)))
        parts.append("zones={%s}" % ",".join(zbits))
        out.append('["%s"]={%s},' % (lua_escape(key), ",".join(parts)))
    out.append("}")
    return "\n".join(out) + "\n"


def write_data_lua(path, data, updated):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_data_lua(data, updated))
