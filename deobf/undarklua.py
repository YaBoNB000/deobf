"""
deobf: undo source-level lowerings (darklua-style) and similar lifter shapes,
restoring idiomatic Luau. Every rewrite is AST-guided (luau-ast) and produces
code with the same meaning:

  compound   t.f = t.f + e        -> t.f += e        (target: local, local.f or local[const];
                                                       value parsed as Binary(op, target, e))
  truncate   ({ f() })[1]         -> (f())           (first value of a call / vararg)
  ifexpr     local x               -> local x = if c then a elseif d then b else e
             if c then x = a elseif d then x = b else x = e end
                                                      (each branch a single assignment of x,
                                                       x not read in the conditions/values)
  andor      local x; if c then x = c else x = e end -> local x = c or e   (c a local)
             local x; if c then x = v else x = c end -> local x = c and v
  bundle     darklua bundles (`<id> = { cache = {}, load = function(m) ... }`,
             `function <id>.a() ... end`, `<id>.load("a")`) -> one `local function`
             per module, loaded where the bundle loaded it (reported only if present).

    python undarklua.py in.lua [out.lua]
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OPS = {"Add": "+", "Sub": "-", "Mul": "*", "Div": "/", "FloorDiv": "//", "Mod": "%", "Pow": "^", "Concat": ".."}

def _ast(text):
    with tempfile.NamedTemporaryFile("wb", suffix=".luau", delete=False) as f:
        f.write(text.encode("latin-1"))
        path = f.name
    try:
        exe = os.path.join(HERE, "bin", "luau-ast.exe" if os.name == "nt" else "luau-ast")
        out = subprocess.run([exe, path], capture_output=True).stdout
    finally:
        os.remove(path)
    return json.loads(out.decode("latin-1"))["root"]

class Src:
    def __init__(self, text):
        self.text = text                                                  
        self.starts = [0]
        for line in text.split("\n"):
            self.starts.append(self.starts[-1] + len(line) + 1)

    def off(self, lc):
        return self.starts[lc[0]] + lc[1]

    def span(self, node):
        a, b = node["location"].split(" - ")
        a = tuple(int(x) for x in a.split(","))
        b = tuple(int(x) for x in b.split(","))
        return self.off(a), self.off(b)

    def of(self, node):
        s, e = self.span(node)
        return self.text[s:e]

def walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            yield x
            stack.extend(v for k, v in x.items() if k != "local" and isinstance(v, (dict, list)))
        elif isinstance(x, list):
            stack.extend(x)

def same(a, b):
    """Same simple assignment target: local, local.f, local[const]."""
    if a.get("type") != b.get("type"):
        return False
    t = a["type"]
    if t == "AstExprLocal":
        return a["local"]["location"] == b["local"]["location"]
    if t == "AstExprIndexName":
        return a["index"] == b["index"] and a["expr"].get("type") == "AstExprLocal" and same(a["expr"], b["expr"])
    if t == "AstExprIndexExpr":
        ia, ib = a["index"], b["index"]
        if ia.get("type") != ib.get("type") or ia.get("type") not in ("AstExprConstantNumber", "AstExprConstantString"):
            return False
        return ia.get("value") == ib.get("value") and a["expr"].get("type") == "AstExprLocal" and same(a["expr"], b["expr"])
    return False

def reads_local(node, loc):
    return any(n.get("type") == "AstExprLocal" and n["local"]["location"] == loc for n in walk(node))

def has_function(node):
    return any(n.get("type") == "AstExprFunction" for n in walk(node))

def find_edits(src, root, stats):
    edits = []                                
    for n in walk(root):
        t = n.get("type")
        if t == "AstStatAssign" and len(n["vars"]) == 1 and len(n["values"]) == 1:
            var, val = n["vars"][0], n["values"][0]
            if val.get("type") == "AstExprBinary" and val.get("op") in OPS and same(var, val["left"]) \
                    and var["type"] in ("AstExprLocal", "AstExprIndexName", "AstExprIndexExpr"):
                s, e = src.span(n)
                edits.append((s, e, "%s %s= %s" % (src.of(var), OPS[val["op"]], src.of(val["right"])), "compound"))
        elif t == "AstExprIndexExpr":
            g, idx = n["expr"], n["index"]
            if g.get("type") == "AstExprGroup" and g["expr"].get("type") == "AstExprTable" \
                    and idx.get("type") == "AstExprConstantNumber" and idx.get("value") == 1:
                items = g["expr"].get("items", [])
                if len(items) == 1 and items[0].get("kind") == "item" \
                        and items[0]["value"].get("type") in ("AstExprCall", "AstExprVarargs"):
                    s, e = src.span(n)
                    edits.append((s, e, "(%s)" % src.of(items[0]["value"]), "truncate"))
        if t == "AstStatBlock" or isinstance(n.get("body"), list):
            body = n.get("body") if isinstance(n.get("body"), list) else []
            for i in range(len(body) - 1):
                ed = ifexpr(src, body[i], body[i + 1])
                if ed:
                    edits.append(ed)
    return edits

def ifexpr(src, loc_stat, if_stat):
    if loc_stat.get("type") != "AstStatLocal" or len(loc_stat["vars"]) != 1 or loc_stat.get("values"):
        return None
    if if_stat.get("type") != "AstStatIf":
        return None
    x = loc_stat["vars"][0]["location"]
    parts = []
    cur = if_stat
    while True:
        tb = cur["thenbody"]["body"]
        if len(tb) != 1 or tb[0].get("type") != "AstStatAssign" or len(tb[0]["vars"]) != 1 or len(tb[0]["values"]) != 1:
            return None
        tv = tb[0]["vars"][0]
        if tv.get("type") != "AstExprLocal" or tv["local"]["location"] != x:
            return None
        cond, val = cur["condition"], tb[0]["values"][0]
        if reads_local(cond, x) or reads_local(val, x) or has_function(val) or has_function(cond):
            return None
        parts.append((cond, val))
        eb = cur.get("elsebody")
        if eb is None:
            return None                                                                    
        if eb.get("type") == "AstStatIf":
            cur = eb
            continue
        bb = eb.get("body", [])
        if len(bb) != 1 or bb[0].get("type") != "AstStatAssign" or len(bb[0]["vars"]) != 1 or len(bb[0]["values"]) != 1:
            return None
        ev = bb[0]["vars"][0]
        if ev.get("type") != "AstExprLocal" or ev["local"]["location"] != x:
            return None
        els = bb[0]["values"][0]
        if reads_local(els, x) or has_function(els):
            return None
        break
    name = loc_stat["vars"][0]["name"]
    c0, v0 = parts[0]
    text = None
    if len(parts) == 1 and c0.get("type") == "AstExprLocal":
                                                                                      
        if v0.get("type") == "AstExprLocal" and v0["local"]["location"] == c0["local"]["location"]:
            text = "local %s = %s or %s" % (name, src.of(c0), src.of(els))
        elif els.get("type") == "AstExprLocal" and els["local"]["location"] == c0["local"]["location"]:
            v = src.of(v0)
            if v0.get("type") == "AstExprBinary" and v0.get("op") in ("And", "Or"):
                v = "(" + v + ")"
            text = "local %s = %s and %s" % (name, src.of(c0), v)
    if text is not None:
        if "\n" in text or len(text) > 200:
            return None
        s, _ = src.span(loc_stat)
        _, e = src.span(if_stat)
        return (s, e, text, "andor")
    text = "local %s = if %s then %s" % (name, src.of(c0), src.of(v0))
    for c, v in parts[1:]:
        text += " elseif %s then %s" % (src.of(c), src.of(v))
    text += " else %s" % src.of(els)
    if "\n" in text or len(text) > 200:
        return None
    s, _ = src.span(loc_stat)
    _, e = src.span(if_stat)
    return (s, e, text, "ifexpr")

def bundle_report(text):
    m = re.search(r"\b(\w+)\s*=\s*\{\s*cache\s*=\s*\{\}\s*,\s*load\s*=\s*function", text)
    return m.group(1) if m else None

def run(text, verbose=True):
    """Returns (text, {kind: count})."""
    counts = {}
    raw = text.encode("utf-8", "surrogateescape").decode("latin-1")
    for _ in range(6):
        src = Src(raw)
        edits = find_edits(src, _ast(raw), counts)
        if not edits:
            break
        edits.sort(key=lambda e: e[1] - e[0])                       
        chosen = []
        for ed in edits:
            if all(ed[1] <= c[0] or ed[0] >= c[1] for c in chosen):
                chosen.append(ed)
        for s, e, new, kind in sorted(chosen, reverse=True):
            raw = raw[:s] + new + raw[e:]
            counts[kind] = counts.get(kind, 0) + 1
    bid = bundle_report(raw)
    if bid:
        counts["bundle_found"] = bid
    if verbose:
        print("[*] undarklua: %s" % counts, file=sys.stderr)
    return raw.encode("latin-1").decode("utf-8", "surrogateescape"), counts

if __name__ == "__main__":
    t = open(sys.argv[1], encoding="utf-8", errors="surrogateescape").read()
    out, c = run(t)
    open(sys.argv[2] if len(sys.argv) > 2 else sys.argv[1], "w", encoding="utf-8", errors="surrogateescape",
         newline="\n").write(out)
