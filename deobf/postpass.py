"""
deobf post passes on the finished devirtualized text (run by finish_text):

  1. Luarmor string decryption: the Luarmor loader wraps every string as
     cache[decrypt("<cipher>", key)] with a PRNG-keyed stream cipher defined in
     the script itself. The pass finds that decryptor in the lifted code,
     reads its constants, decodes every call site offline and inlines the
     plain strings.
  2. t["name"] -> t.name, { ["name"] = v } -> { name = v }.
  3. `for k, v in t, nil, nil do` -> `for k, v in t do` (Luau generalized iteration).
  4. StyLua formatting (bin/stylua, Luau syntax) when available.
Each pass is skipped with a warning if it fails.
"""
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KEYWORDS = {"and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in", "local",
            "nil", "not", "or", "repeat", "return", "then", "true", "until", "while", "continue", "goto"}
STR = r'"((?:[^"\\\n]|\\.)*)"'

def parse_lua_str(s):
    b = s.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    esc = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11, "\\": 92, '"': 34, "'": 39, "\n": 10}
    while i < len(b):
        c = b[i]
        if c != 92:
            out.append(c)
            i += 1
            continue
        i += 1
        d = chr(b[i])
        if d.isdigit():
            j = i
            while j < len(b) and j < i + 3 and chr(b[j]).isdigit():
                j += 1
            out.append(int(b[i:j]))
            i = j
        elif d == "x":
            out.append(int(b[i + 1:i + 3], 16))
            i += 3
        elif d == "u":
            j = b.index(b"}", i)
            out += chr(int(b[i + 2:j], 16)).encode("utf-8")
            i = j + 1
        elif d == "z":
            i += 1
            while i < len(b) and chr(b[i]).isspace():
                i += 1
        else:
            out.append(esc[d])
            i += 1
    return bytes(out)

def lua_lit(b):
    try:
        u = b.decode("utf-8")
    except UnicodeDecodeError:
        u = None
    out = []
    if u is not None:
        for ch in u:
            o = ord(ch)
            if ch == '"':
                out.append('\\"')
            elif ch == "\\":
                out.append("\\\\")
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                out.append("\\r")
            elif o < 32 or o == 127:
                out.append("\\%d" % o)
            elif o >= 128 and not ch.isprintable():
                out.append("".join("\\%d" % x for x in ch.encode("utf-8")))
            else:
                out.append(ch)
    else:
        for c in b:
            if c == 34:
                out.append('\\"')
            elif c == 92:
                out.append("\\\\")
            elif c == 10:
                out.append("\\n")
            elif 32 <= c < 127:
                out.append(chr(c))
            else:
                out.append("\\%d" % c)
    return '"' + "".join(out) + '"'

class LuarmorCipher:
    def __init__(self, a, c, m, mul2, mod2, seed, add2):
        self.a, self.c, self.m, self.mul2, self.mod2, self.seed, self.add2 = a, c, m, mul2, mod2, seed, add2

    def decode(self, data, key):
        n = float(key % self.m)
        n2 = key % 255 + self.add2
        q = []
        out = bytearray()
        acc = self.seed
        for ch in data:
            if not q:
                n = (n * self.a + self.c) % self.m
                while True:
                    n2 = n2 * self.mul2 % self.mod2
                    if n2 != 1:
                        break
                n3 = n2 % 32
                n4 = math.floor(n / 2 ** (13 - (n2 - n3) / 32)) % 4294967296 / 2 ** n3
                n5 = math.floor(n4 % 1 * 4294967296) + math.floor(n4)
                n6 = n5 % 65536
                n7 = (n5 - n6) / 65536
                n8 = n6 % 256
                n9 = n7 % 256
                q = [n8, (n6 - n8) / 256, n9, (n7 - n9) / 256]
            acc = int((ch + q.pop() + acc) % 256)
            out.append(acc)
        return bytes(out)

def luarmor_strings(text):
    m_lcg = re.search(r"\n\s*(\w+) = \(\1 \* (\d+) \+ (\d+)\) % (\d+)\n", text)
    m_n2 = re.search(r"\n\s*(\w+) = \1 \* (\d+) % (\d+)\n\s*until \1 ~= 1", text)
                                                                                                    
    m_fn = re.search(r"(?:local function (?P<f1>\w+)|local (?P<f2>\w+) = function)\((?P<a>\w+), (?P<k>\w+)\)\n"
                     r"(?:\s*local (?P<l>\w+) = (?P<c>\w+)\n)?\s*if not (?P<t>\w+)\[(?P=k)\] then\n"
                     r"(?:(?!\nend\n)[\s\S])*?(?P=k) % 255 \+ (?P<add>\d+)\n(?:(?!\nend\n)[\s\S])*?local \w+ = (?P<seed>\d+)\n"
                     r"(?:(?!\nend\n)[\s\S])*?string\.byte\((?P=a), \w+\)", text)
    if not (m_lcg and m_n2 and m_fn):
        return text, 0, 0
    fname = m_fn.group("f1") or m_fn.group("f2")
    ciph = LuarmorCipher(int(m_lcg.group(2)), int(m_lcg.group(3)), int(m_lcg.group(4)),
                         int(m_n2.group(2)), int(m_n2.group(3)), int(m_fn.group("seed")), int(m_fn.group("add")))
                                                                           
    pat = re.compile(r"\b\w+\[\s*%s\(\s*%s,\s*(\d+)\s*\)\s*\]" % (re.escape(fname), STR))
    count = [0]

    def rep(m):
        count[0] += 1
        return "\x01" + lua_lit(ciph.decode(parse_lua_str(m.group(1)), int(m.group(2)))) + "\x02"
    text = pat.sub(rep, text)
                                                
    text = re.sub(r'\x01("(?:[^"\\\n]|\\.)*")\x02([:.\[("]?)',
                  lambda mm: ("(%s)%s" if mm.group(2) else "%s%s") % (mm.group(1), mm.group(2)), text)
    left = len(re.findall(r"\b%s\(" % re.escape(fname), text))
    if count[0]:
        text = re.sub(r"(\n[ \t]*)(local function %s\(|local %s = function\()" % (re.escape(fname), re.escape(fname)),
                      lambda mm: "%s-- Luarmor string decryptor: every call site was decoded by the deobfuscator (%d strings)%s%s"
                      % (mm.group(1), count[0], mm.group(1), mm.group(2)), text, count=1)
    return text, count[0], left

def _ast(text):
    import json
    import tempfile
    with tempfile.NamedTemporaryFile("wb", suffix=".luau", delete=False) as f:
        f.write(text.encode("utf-8", "surrogateescape"))
        path = f.name
    try:
        exe = os.path.join(HERE, "bin", "luau-ast.exe" if os.name == "nt" else "luau-ast")
        out = subprocess.run([exe, path], capture_output=True).stdout
    finally:
        os.remove(path)
    return json.loads(out.decode("latin-1"))["root"]

def _loc(s):
    a, b = s.split(" - ")
    return tuple(int(x) for x in a.split(",")), tuple(int(x) for x in b.split(","))

def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            yield x
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
        elif isinstance(x, list):
            stack.extend(x)

CONST_POOL_NAME = "serverConsts"

def encfunc_consts(text):
    """The script's LPH_ENCFUNC function (decrypted at run time with a key the
    license server sends) fills a local with a constant pool; the rest of the
    script reads it as pool[n]. Name that local and inline its single-use
    `local x = pool[n]` copies (the pool is never written after it is filled)."""
    m = None
    for m in re.finditer(r"\n[ \t]*(\w+) = nil\n(?:(?!\bluraph_runtime\d+\()[\s\S]){0,4000}?\bluraph_runtime\d+\(", text):
        break
    if m is None or CONST_POOL_NAME in text:
        return text, 0
    name = m.group(1)
    line_no = text.count("\n", 0, m.start()) + 1                                      
    root = _ast(text)
    decl = None
    for n in _walk(root):
        if n.get("type") == "AstStatAssign" and len(n["vars"]) == 1 and n["vars"][0].get("type") == "AstExprLocal" \
                and n["vars"][0]["local"]["name"] == name and _loc(n["location"])[0][0] == line_no:
            decl = n["vars"][0]["local"]["location"]
            break
    if decl is None:
        return text, 0
    refs = {}                                         
    assigned = set()
    pool_reads = []                                         
    for n in _walk(root):
        t = n.get("type")
        if t == "AstExprLocal":
            refs.setdefault(n["local"]["location"], []).append(_loc(n["location"]))
        elif t in ("AstStatAssign", "AstStatCompoundAssign"):
            for v in (n["vars"] if t == "AstStatAssign" else [n["var"]]):
                if v.get("type") == "AstExprLocal":
                    assigned.add(v["local"]["location"])
        elif t == "AstStatLocal" and len(n["vars"]) == 1 and len(n.get("values", [])) == 1:
            v = n["values"][0]
            if v.get("type") == "AstExprIndexExpr" and v["expr"].get("type") == "AstExprLocal" \
                    and v["expr"]["local"]["location"] == decl and v["index"].get("type") == "AstExprConstantNumber":
                pool_reads.append((n, n["vars"][0]["location"], v["index"]["value"]))
    lines = text.split("\n")
    edits = []                               
    dels = set()
    inlined = 0
    for st, vloc, idx in pool_reads:
        r = refs.get(vloc, [])
        (a, b) = _loc(st["location"])
        if len(r) != 1 or vloc in assigned or a[0] != b[0]:
            continue
        (u0, u1) = r[0]
        edits.append((u0[0], u0[1], u1[1], "%s[%s]" % (CONST_POOL_NAME, idx if idx != int(idx) else int(idx))))
        dels.add(a[0])
        inlined += 1
    for (a, b) in refs.get(decl, []):
        edits.append((a[0], a[1], b[1], CONST_POOL_NAME))
    per = {}
    for ln, c0, c1, new in edits:
        per.setdefault(ln, []).append((c0, c1, new))
    for ln, lst in per.items():
        bs = lines[ln].encode("utf-8", "surrogateescape")
        for c0, c1, new in sorted(set(lst), reverse=True):
            bs = bs[:c0] + new.encode() + bs[c1:]
        lines[ln] = bs.decode("utf-8", "surrogateescape")
    note = ("-- LPH_ENCFUNC: the function decrypted below (with the key from the Luarmor server) fills %s,",
            "-- the script's constant pool. Without that key its values are unknown offline: they are",
            "-- left as %s[n].")
    ind = re.match(r"[ \t]*", lines[line_no]).group(0)
    lines[line_no:line_no] = [ind + note[0] % CONST_POOL_NAME, ind + note[1], ind + note[2] % CONST_POOL_NAME]
    out = [l for i, l in enumerate(lines) if i not in {d if d < line_no else d + 3 for d in dels}]
    return "\n".join(out), inlined

PURE = {"AstExprLocal", "AstExprConstantNil", "AstExprConstantBool", "AstExprConstantNumber",
        "AstExprConstantString", "AstExprGlobal", "AstExprFunction", "AstExprVarargs"}

def dead_locals(text):
    """`local x = <local/constant/global/function>` that nothing reads or
    writes: VM leftovers (e.g. aliases of the Luarmor string cache once every
    string is decoded). Repeated until nothing changes."""
    total = 0
    for _ in range(6):
        root = _ast(text)
        refs = set()
        stats = []
        for n in _walk(root):
            t = n.get("type")
            if t == "AstExprLocal":
                refs.add(n["local"]["location"])
            elif t == "AstStatLocal":
                stats.append(n)
        lines = text.split("\n")
        dels = set()
        for st in stats:
            if any(v["location"] in refs for v in st["vars"]):
                continue
            if not all(v.get("type") in PURE for v in st.get("values", [])):
                continue
            (a, b) = _loc(st["location"])
            if lines[a[0]][:a[1]].strip() or lines[b[0]][b[1]:].strip().rstrip(";"):
                continue
            dels.update(range(a[0], b[0] + 1))
        if not dels:
            break
        total += len(dels)
        text = "\n".join(l for i, l in enumerate(lines) if i not in dels)
    return text, total

def dot_access(text):
    ident = r'"([A-Za-z_][A-Za-z0-9_]*)"'

    def idx(m):
        return m.group(1) + "." + m.group(2) if m.group(2) not in KEYWORDS else m.group(0)
    text = re.sub(r"([\w)\]])\[%s\]" % ident, idx, text)

    def key(m):
        return m.group(1) + m.group(2) + " =" if m.group(2) not in KEYWORDS else m.group(0)
    text = re.sub(r"((?:^|[{,])[ \t]*)\[%s\] =" % ident, key, text, flags=re.M)
    return text

def method_syntax(text):
    """`T.Name = function(self, a)` -> `function T:Name(a)` (the same function)."""
    pat = re.compile(r"^([ \t]*)([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*) = function\(self(?:, ([^)\n]*))?\)$", re.M)
    n = [0]

    def rep(m):
        n[0] += 1
        return "%sfunction %s:%s(%s)" % (m.group(1), m.group(2), m.group(3), m.group(4) or "")
    return pat.sub(rep, text), n[0]

def generic_for(text):
    return re.sub(r"(\bfor\b[^\n]*?\bin\b(?:(?!\bdo\b)[\s\S])*?),\s*nil,\s*nil(\s+do\b)", r"\1\2", text)

def stylua(text):
    exe = os.path.join(HERE, "bin", "stylua.exe" if os.name == "nt" else "stylua")
    if not os.path.exists(exe):
        return text
    r = subprocess.run([exe, "--syntax", "Luau", "--indent-type", "Tabs", "--column-width", "120", "-"],
                       input=text.encode("utf-8", "surrogateescape"), capture_output=True)
    if r.returncode != 0:
        print("[!] stylua failed: " + r.stderr.decode("utf-8", "replace")[:300], file=sys.stderr)
        return text
    return r.stdout.decode("utf-8", "surrogateescape")

def pre_naming(text):
    """Passes that run before the naming pass (names come out better on plain strings)."""
    if os.environ.get("DEOBF_DUMP_PRE"):
        open(os.environ["DEOBF_DUMP_PRE"], "w", encoding="utf-8", errors="surrogateescape").write(text)
    return _run(text, (("luarmor strings", luarmor_strings), ("dot access", dot_access), ("dead locals", dead_locals)))

def run(text):
    return _run(text, (("luarmor strings", luarmor_strings), ("dot access", dot_access),
                       ("generic for", generic_for), ("encfunc consts", encfunc_consts),
                       ("method syntax", method_syntax),
                       ("stylua", stylua)))

def _run(text, passes):
    for name, fn in passes:
        if os.environ.get("DEOBF_NO_POST_" + name.upper().replace(" ", "_")):
            continue
        try:
            r = fn(text)
            if isinstance(r, tuple):
                text = r[0]
                if len(r) > 1:
                    print("[*] post pass %s: %s" % (name, r[1:]), file=sys.stderr)
            else:
                text = r
        except Exception as ex:                
            print("[!] post pass %s failed: %s" % (name, ex), file=sys.stderr)
    return text

if __name__ == "__main__":
    src = open(sys.argv[1], encoding="utf-8", errors="surrogateescape").read()
    out = run(src)
    open(sys.argv[2] if len(sys.argv) > 2 else sys.argv[1], "w", encoding="utf-8", errors="surrogateescape",
         newline="\n").write(out)
