"""
Variable names for devirtualized output (text pass, scope-aware).

The lifter names locals after VM registers (r12, s3_1, ...). This pass parses
the finished Luau with luau-ast, infers a name for every such local from how
it is defined, and rewrites exactly the declaration and reference positions:

  Instance.new("Frame")          -> frame          GetService("Players") -> Players
  x:FindFirstChild("Humanoid")   -> humanoid       x.Character           -> character
  x:GetChildren()                -> children       pcall(...)            -> ok, result
  for _ in ipairs(players)       -> i, player      for _ in pairs(t)     -> k, v
  numeric for                    -> i              require(a.Foo)        -> Foo

A name is taken only if it cannot change what any reference resolves to: no
reference of another local with that name lies inside this local's scope
(shadowing), and names of globals are never used.

    python names.py file.luau [-o out.luau]
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GENERATED = re.compile(r"^(?:[a-z]\d+(?:_\d+)*|arg\d+_*)$")
KEYWORDS = {"and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in", "local",
            "nil", "not", "or", "repeat", "return", "then", "true", "until", "while", "continue", "type",
            "export", "self"}
DATATYPES = {"Color3": "color", "Vector3": "vector", "Vector2": "vector2", "UDim2": "udim2", "UDim": "udim",
             "CFrame": "cframe", "TweenInfo": "tweenInfo", "ColorSequence": "colorSequence",
             "NumberSequence": "numberSequence", "NumberRange": "numberRange", "Rect": "rect",
             "RaycastParams": "raycastParams", "OverlapParams": "overlapParams", "Ray": "ray",
             "BrickColor": "brickColor", "Font": "font", "PhysicalProperties": "physicalProperties"}
METHOD_NAMES = {"GetChildren": "children", "GetDescendants": "descendants", "GetPlayers": "players",
                "Clone": "clone", "Connect": "connection", "Once": "connection", "Create": "tween",
                "GetPropertyChangedSignal": "signal", "GetMouse": "mouse", "Raycast": "hit",
                "InvokeServer": "response", "HttpGet": "response", "GetAsync": "response",
                "JSONDecode": "data", "JSONEncode": "json", "match": "match", "gsub": "str", "sub": "str",
                "lower": "str", "upper": "str", "format": "str", "rep": "str", "find": "pos", "split": "parts",
                "GetAttribute": "attribute", "IsA": "isA", "Wait": "result"}
GLOBAL_CALLS = {"tick": "now", "time": "now", "os.clock": "now", "os.time": "now", "type": "kind",
                "typeof": "kind", "tostring": "str", "tonumber": "num", "setmetatable": "obj",
                "loadstring": "chunk", "getgenv": "genv", "getfenv": "env", "gethui": "hui",
                "string.format": "str", "string.rep": "str", "string.sub": "str", "table.concat": "str",
                "math.floor": "n", "math.clamp": "n", "math.min": "n", "math.max": "n", "math.abs": "n",
                "math.random": "n", "select": "value", "unpack": "value", "table.unpack": "value",
                "coroutine.create": "thread", "task.spawn": "thread", "task.delay": "thread",
                "Instance.new": "instance", "newproxy": "proxy", "rawget": "value", "next": "key"}

WEAK = {"str", "n", "tbl", "flag", "fn", "value", "key", "kind", "num", "obj", "result"}
                                                                                           
SIGNAL_PARAMS = {
    "PlayerAdded": ["player"], "PlayerRemoving": ["player"],
    "CharacterAdded": ["character"], "CharacterRemoving": ["character"], "CharacterAppearanceLoaded": ["character"],
    "Touched": ["hit"], "TouchEnded": ["hit"],
    "InputBegan": ["input", "gameProcessed"], "InputEnded": ["input", "gameProcessed"],
    "InputChanged": ["input", "gameProcessed"],
    "Heartbeat": ["deltaTime"], "RenderStepped": ["deltaTime"], "PreSimulation": ["deltaTime"],
    "PostSimulation": ["deltaTime"], "PreRender": ["deltaTime"], "Stepped": ["time", "deltaTime"],
    "ChildAdded": ["child"], "ChildRemoved": ["child"],
    "DescendantAdded": ["descendant"], "DescendantRemoving": ["descendant"],
    "HealthChanged": ["health"], "Chatted": ["message"],
    "FocusLost": ["enterPressed", "inputObject"], "Triggered": ["player"],
    "PromptTriggered": ["prompt", "player"], "PromptButtonHoldBegan": ["prompt", "player"],
    "Idled": ["idleTime"], "MouseButton1Down": ["x", "y"], "Activated": ["inputObject", "clickCount"],
    "AncestryChanged": ["child", "parent"], "AttributeChanged": ["attribute"],
    "StateChanged": ["old", "new"], "MoveToFinished": ["reached"], "Completed": ["playbackState"],
}

def str_arg_name(call):
    """f("RouterClient") -> RouterClient, x.get("TradeAPI/SendTrade") -> sendTrade:
    a call whose first argument names what it returns."""
    args = call.get("args") or []
    s = const_str(args[0]) if args else None
    if not s:
        return None
    last = re.split(r"[/.:\\]", s)[-1]
    if re.match(r"^[A-Za-z_]\w{1,40}$", last) and last not in KEYWORDS:
        return last if last[:1].isupper() and not last.isupper() else camel(last)
    return None

def loc(s):
    a, b = s.split(" - ")
    l1, c1 = a.split(",")
    l2, c2 = b.split(",")
    return (int(l1), int(c1)), (int(l2), int(c2))

def camel(s):
    """'UICorner' -> 'uiCorner', 'Humanoid Root' -> 'humanoidRoot'; None if unusable."""
    parts = re.findall(r"[A-Za-z0-9]+", s)
    if not parts:
        return None
    if len(parts) > 1:
                                                                       
        parts = [p.capitalize() if p.isupper() else p for p in parts]
    w = "".join(p[:1].upper() + p[1:] for p in parts)
    m = re.match(r"^([A-Z]+)([A-Z][a-z].*)$", w)
    if m:
        w = m.group(1).lower() + m.group(2)
    else:
        m = re.match(r"^([A-Z]+)$", w)
        w = w.lower() if m else w[:1].lower() + w[1:]
    if w[:1].isdigit():
        w = "n" + w
    return w[:32] if w else None

def singular(name):
    if not name:
        return None
    if name == "children":
        return "child"
    if name.endswith("ies") and len(name) > 4:
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss") and len(name) > 3:
        return name[:-1]
    return None

def const_str(e):
    return e.get("value") if e and e.get("type") == "AstExprConstantString" else None

def dotted(e):
    """'a.b.c' for global/index-name chains, else None."""
    if e.get("type") == "AstExprGlobal":
        return e["global"]
    if e.get("type") == "AstExprIndexName" and e.get("op") == ".":
        d = dotted(e["expr"])
        return d + "." + e["index"] if d else None
    return None

class Namer:
    def __init__(self, text):
        import tempfile
        self.text = text
        with tempfile.NamedTemporaryFile("w", suffix=".luau", delete=False, encoding="utf-8", newline="\n") as f:
            f.write(text)
            path = f.name
        try:
            out = subprocess.run([os.path.join(HERE, "bin", "luau-ast.exe" if os.name == "nt" else "luau-ast"), path],
                                 capture_output=True).stdout
        finally:
            os.remove(path)
        self.root = json.loads(out.decode("latin-1"))["root"]
        self.locals = {}                               
        self.globals = set()
        self.order = []

    def local_info(self, node, scope):
        key = node["location"]
        info = self.locals.get(key)
        if info is None:
            info = self.locals[key] = {"key_loc": key, "name": node["name"], "decl": loc(key)[0], "scope": scope,
                                       "refs": [loc(key)[0]], "spans": [loc(key)], "hint": None, "hints": []}
            self.order.append(key)
        return info

    def collect(self):
        self.visit_block(self.root)

    def visit_block(self, blk):
        end = loc(blk["location"])[1]
        for st in blk["body"]:
            self.visit_stat(st, end)

    def visit_stat(self, st, block_end):
        t = st["type"]
        if t == "AstStatLocal":
            for v in st.get("values", []):
                self.visit_expr(v)
            stend = loc(st["location"])[1]
            vals = st.get("values", [])
            for i, var in enumerate(st["vars"]):
                                                                                        
                start = loc(st["location"])[0] if len(st["vars"]) == 1 and len(vals) == 1 \
                    and vals[0]["type"] == "AstExprFunction" else stend
                info = self.local_info(var, (start, block_end))
                if i < len(vals):
                    info["hints"].append((vals[i], 0))
                elif vals and vals[-1]["type"] == "AstExprCall":
                    info["hints"].append((vals[-1], i - len(vals) + 1))
            return
        if t == "AstStatLocalFunction":
            info = self.local_info(st["name"], (loc(st["location"])[0], block_end))
            info["hints"].append((st["func"], 0))
            self.visit_expr(st["func"])
            return
        if t == "AstStatAssign":
            for v in st["vars"]:
                self.visit_expr(v)
            vals = st["values"]
            for v in vals:
                self.visit_expr(v)
            for var, v in zip(st["vars"], vals):
                if var.get("type") == "AstExprIndexName" and v.get("type") == "AstExprFunction" and v.get("args"):
                    info = self.locals.get(v["args"][0]["location"])
                    if info is not None:
                        info["method_of"] = var["index"]
                                                                                         
                if v.get("type") == "AstExprBinary" and v.get("op") == "Or":
                    v = v["left"]
                if var.get("type") == "AstExprIndexName" and v.get("type") == "AstExprLocal":
                    info = self.locals.get(v["local"]["location"])
                    if info is not None:
                        info.setdefault("prop", camel(var["index"]))
            for var in st["vars"]:
                if var.get("type") == "AstExprIndexName" and var["expr"].get("type") == "AstExprLocal":
                    info = self.locals.get(var["expr"]["local"]["location"])
                    if info is not None:
                        info.setdefault("fields", set()).add(var["index"])
            for i, var in enumerate(st["vars"]):
                if var["type"] == "AstExprLocal":
                    info = self.locals.get(var["local"]["location"])
                    if info is not None:
                        if i < len(vals):
                            info["hints"].append((vals[i], 0))
                        elif vals and vals[-1]["type"] == "AstExprCall":
                            info["hints"].append((vals[-1], i - len(vals) + 1))
            return
        if t == "AstStatFor":
            for k in ("from", "to", "step"):
                if st.get(k):
                    self.visit_expr(st[k])
            body = loc(st["body"]["location"])
            info = self.local_info(st["var"], (body[0], body[1]))
            info["hint"] = "i"
            self.visit_block(st["body"])
            return
        if t == "AstStatForIn":
            for v in st["values"]:
                self.visit_expr(v)
            body = loc(st["body"]["location"])
            names = self.forin_names(st)
            for var, nm in zip(st["vars"], names):
                info = self.local_info(var, (body[0], body[1]))
                info["hint"] = nm
            self.visit_block(st["body"])
            return
        if t in ("AstStatBlock",):
            self.visit_block(st)
            return
        if t == "AstStatIf":
            self.visit_expr(st["condition"])
            self.visit_block(st["thenbody"])
            e = st.get("elsebody")
            if e is not None:
                if e["type"] == "AstStatIf":
                    self.visit_stat(e, block_end)
                else:
                    self.visit_block(e)
            return
        if t == "AstStatWhile":
            self.visit_expr(st["condition"])
            self.visit_block(st["body"])
            return
        if t == "AstStatRepeat":
            self.visit_block(st["body"])
            self.visit_expr(st["condition"])
            return
        if t == "AstStatCompoundAssign":
            vi = self.use_of(st["var"])
            if vi is not None and st.get("op") in ("Add", "Sub") and st["value"].get("type") == "AstExprConstantNumber" \
                    and st["value"].get("value") == 1:
                vi["counter"] = True
            self.visit_expr(st["var"])
            self.visit_expr(st["value"])
            return
                                             
        for k, v in st.items():
            if isinstance(v, dict) and v.get("type", "").startswith("AstExpr"):
                self.visit_expr(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, dict) and x.get("type", "").startswith("AstExpr"):
                        self.visit_expr(x)

    def use_of(self, e):
        """info of a local expression (for usage facts), else None."""
        if e.get("type") == "AstExprLocal":
            return self.locals.get(e["local"]["location"])
        return None

    def visit_expr(self, e):
        t = e.get("type")
                                                
        if t == "AstExprIndexName":
            info = self.use_of(e["expr"])
            if info is not None:
                info.setdefault("reads", []).append(e["index"])
        elif t == "AstExprCall":
            f = e.get("func", {})
            info = self.use_of(f)
            if info is not None:
                info["called"] = info.get("called", 0) + 1
            if f.get("type") == "AstExprIndexName" and f.get("op") == ":":
                info = self.use_of(f["expr"])
                if info is not None:
                    info.setdefault("methods", []).append(f["index"])
                if f["index"] in ("Connect", "Once") and e.get("args"):
                    cb = self.use_of(e["args"][0])
                    sig = f["expr"]
                    sname = None
                    if sig.get("type") == "AstExprIndexName":
                        sname = sig["index"]
                    elif sig.get("type") == "AstExprCall" and sig["func"].get("type") == "AstExprIndexName" \
                            and sig.get("args") and const_str(sig["args"][0]):
                        if sig["func"]["index"] == "GetPropertyChangedSignal":
                            sname = const_str(sig["args"][0]) + "Changed"
                        elif sig["func"]["index"] == "GetAttributeChangedSignal":
                            sname = const_str(sig["args"][0]) + "Changed"
                    if cb is not None and sname and re.match(r"^[A-Za-z]\w*$", sname):
                        cb["connected"] = sname
            if f.get("type") == "AstExprIndexName" and len(e.get("args") or []) == 1:
                m = re.match(r"^(?:Set|set|Toggle|Update)([A-Z]\w*)$", f["index"])
                ai = self.use_of(e["args"][0])
                if m and ai is not None:
                    ai.setdefault("argof", camel(m.group(1)))
            d = dotted(f) if f.get("type") in ("AstExprGlobal", "AstExprIndexName") else None
            if d in ("ipairs", "pairs", "table.insert", "table.remove", "table.find", "table.sort", "table.clear",
                     "table.concat", "next") and e.get("args"):
                info = self.use_of(e["args"][0])
                if info is not None:
                    info["iterated"] = True
            if d in ("type", "typeof") and e.get("args"):
                info = self.use_of(e["args"][0])
                if info is not None:
                    info["typechecked"] = True
        elif t == "AstExprUnary" and e.get("op") == "Len":
            info = self.use_of(e["expr"])
            if info is not None:
                info["iterated"] = True
        elif t == "AstExprIndexExpr":
            info = self.use_of(e["expr"])
            if info is not None:
                info["indexed"] = True
            ki = self.use_of(e.get("index", {}))
            if ki is not None:
                ki["as_key"] = True
        elif t == "AstExprBinary" and e.get("op") in ("CompareEq", "CompareNe"):
            for a, b in ((e["left"], e["right"]), (e["right"], e["left"])):
                info = self.use_of(a)
                if info is None:
                    continue
                if b.get("type") == "AstExprIndexName" and b.get("op") == "." and camel(b["index"]):
                    info.setdefault("eqfield", camel(b["index"]))
                elif b.get("type") == "AstExprConstantString":
                    info["eqstr"] = True
        elif t == "AstExprBinary" and e.get("op") == "Concat":
            for side in (e["left"], e["right"]):
                info = self.use_of(side)
                if info is not None:
                    info["concat"] = True
        if t == "AstExprLocal":
            info = self.locals.get(e["local"]["location"])
            if info is not None:
                a, b = loc(e["location"])
                info["refs"].append(a)
                info["spans"].append((a, b))
            return
        if t == "AstExprGlobal":
            self.globals.add(e["global"])
            return
        if t == "AstExprCall":
            f = e.get("func", {})
                                                                  
            if f.get("type") == "AstExprIndexExpr" and f["expr"].get("type") == "AstExprLocal"                     and const_str(f.get("index")) is None:
                info = self.locals.get(f["expr"]["local"]["location"])
                if info is not None:
                    info["dispatch"] = True
                                                                                            
            if f.get("type") == "AstExprIndexName" and f.get("index") in ("Connect", "Once", "ConnectParallel") \
                    and f["expr"].get("type") == "AstExprIndexName" and e.get("args"):
                params = SIGNAL_PARAMS.get(f["expr"]["index"])
                if params:
                    for k, v in e.items():
                        if k != "args" and isinstance(v, dict) and v.get("type", "").startswith("AstExpr"):
                            self.visit_expr(v)
                    for a in e["args"]:
                        self.visit_expr(a)
                    self.callback_params(e["args"][0], params)
                    return
        if t == "AstExprBinary" and e.get("op") == "Concat" and e["right"].get("type") == "AstExprLocal":
                                       
            m = re.match(r"^([A-Za-z][A-Za-z ]{0,30}?)\s*[:=]\s*$", const_str(e["left"]) or "")
            info = self.locals.get(e["right"]["local"]["location"]) if m else None
            if info is not None:
                info.setdefault("label", camel(m.group(1)))
        if t == "AstExprFunction":
            body = loc(e["body"]["location"])
            for a in e["args"]:
                info = self.local_info(a, (loc(e["location"])[0], body[1]))
                info["hint"] = info["hint"] or "arg"
            self.visit_block(e["body"])
            return
        for k, v in e.items():
            if isinstance(v, dict) and "type" in v:
                if v["type"] == "AstStatBlock":
                    self.visit_block(v)
                elif v["type"].startswith("AstExpr"):
                    self.visit_expr(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, dict) and x.get("type", "").startswith("AstExpr"):
                        self.visit_expr(x)
                    elif isinstance(x, dict) and "value" in x and isinstance(x["value"], dict) and False:
                        pass
                    elif isinstance(x, dict) and "value" in x and isinstance(x["value"], dict):
                                                          
                        kk = x.get("key")
                        if isinstance(kk, dict) and const_str(kk) in ("Callback", "OnChanged", "Changed", "OnChange") \
                                and x["value"].get("type") == "AstExprFunction" and x["value"].get("args"):
                            self._cbparams = getattr(self, "_cbparams", set())
                            self._cbparams.add(x["value"]["args"][0]["location"])
                        if isinstance(kk, dict) and const_str(kk) and re.match(r"^[A-Za-z]\w*$", const_str(kk)):
                            vi = self.use_of(x["value"])
                            if vi is not None:
                                vi.setdefault("prop", camel(const_str(kk)))
                                                          
                        if isinstance(x.get("key"), dict):
                            self.visit_expr(x["key"])
                        self.visit_expr(x["value"])

    def callback_params(self, fn, params):
        """Name the parameters of a function (literal, or a local holding one) after the signal's."""
        if fn.get("type") == "AstExprLocal":
            info = self.locals.get(fn["local"]["location"])
            funcs = [h for h, _ in (info or {}).get("hints", []) if h.get("type") == "AstExprFunction"]
            if not funcs:
                return
            fn = funcs[0]
        if fn.get("type") != "AstExprFunction":
            return
        for a, nm in zip(fn["args"], params):
            info = self.locals.get(a["location"])
            if info is not None and info["hint"] in (None, "arg"):
                info["hint"] = nm

    def function_name(self, fn):
        nm = self.function_name0(fn)
        if nm:
            return nm
        return self.function_name_deobf(fn)

    def param_name(self, fn, i=0):
        args = fn.get("args") or []
        if len(args) <= i:
            return None
        info = self.locals.get(args[i]["location"])
        if info is None:
            return None
        try:
            nm = self.base_name(info)
        except Exception:                
            return None
        return nm if nm and nm not in ("arg", "v", "tbl", "value", "options", "self") else None

    def function_name_deobf(self, fn):
        nm = self.function_name_deobf0(fn)
        if nm:
            return nm
        body = fn.get("body", {}).get("body", [])
        if not body:
            return None
        last = body[-1]
                                                          
        if last["type"] == "AstStatReturn" and len(last.get("list", [])) == 1:
            r = last["list"][0]
            def boolish(x):
                t = x.get("type")
                if t == "AstExprBinary":
                    if x["op"].startswith("Compare"):
                        return True
                    if x["op"] in ("And", "Or"):
                        return boolish(x["right"]) or boolish(x["left"])
                if t == "AstExprUnary" and x["op"] == "Not":
                    return True
                if t == "AstExprCall" and x["func"].get("type") == "AstExprIndexName" and x["func"]["index"] in ("IsA", "IsDescendantOf", "IsAncestorOf"):
                    return True
                if t == "AstExprConstantBool":
                    return True
                return False
            if boolish(r):
                p = self.param_name(fn)
                return camel("is valid " + p) if p else "check"
                                              
        n = 0
        for st in body:
            if st["type"] == "AstStatAssign" and all(v.get("type") in ("AstExprConstantNil", "AstExprConstantBool", "AstExprConstantNumber")
                                                     for v in st["values"]):
                n += 1
            else:
                break
        if n >= 3:
            return "reset"
        if len(body) == 1 and body[0]["type"] == "AstStatWhile":
            return "loop"
        return None

    def function_name_deobf0(self, fn):
        """deobf: more body shapes. tween(obj, props, ...) for a function that
        calls X:Create(...) and :Play(); create(className, props) for one returning
        Instance.new(<param>); getX for `return a.X`-style one-liners."""
        body = fn.get("body", {}).get("body", [])
        if not body:
            return None
        calls = []
        stack = [fn["body"]]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if n.get("type") == "AstExprCall" and n["func"].get("type") == "AstExprIndexName":
                    calls.append(n["func"]["index"])
                if n.get("type") == "AstExprFunction" and n is not fn:
                    continue
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                stack.extend(n)
        last = body[-1]
        if "Create" in calls and "Play" in calls:
            return "tween"
        if last["type"] == "AstStatReturn" and len(last.get("list", [])) == 1:
            r = last["list"][0]
            where = r["local"]["location"] if r.get("type") == "AstExprLocal" else None
            for st in body:
                if where and st["type"] == "AstStatLocal" and len(st["vars"]) == 1 and st.get("values") \
                        and st["vars"][0]["location"] == where:
                    v = st["values"][0]
                    if v.get("type") == "AstExprCall" and dotted(v["func"]) in ("Instance.new", "Drawing.new"):
                        return "create"
                    if v.get("type") == "AstExprTable" and len(body) > 2:
                        return "new"
            if len(body) == 1 and r.get("type") == "AstExprIndexName" and r["expr"].get("type") == "AstExprCall":
                r = r["expr"]
            if len(body) == 1 and r.get("type") == "AstExprCall":
                f = r["func"]
                if f.get("type") == "AstExprIndexName" and f.get("op") == ":":
                    return camel(f["index"][0].lower() + f["index"][1:]) if f["index"] else None
        if len(body) <= 3 and "Disconnect" in calls:
            return "disconnect"
        if "Destroy" in calls and len(body) <= 4:
            return "destroy"
        return None

    def function_name0(self, fn):
        """toggleShield for `function() S.Shield = not S.Shield ...`, createFrame for a
        function returning the local it made with Instance.new("Frame")."""
        body = fn.get("body", {}).get("body", [])
        if not body:
            return None
        st = body[0]
        if st["type"] == "AstStatAssign" and len(st["vars"]) == 1 and len(st["values"]) == 1:
            var, val = st["vars"][0], st["values"][0]
            if var.get("type") == "AstExprIndexName" and val.get("type") == "AstExprUnary" \
                    and val.get("op") == "Not" and self.same(var, val["expr"]):
                return camel("toggle " + var["index"])
        last = body[-1]
        if last["type"] == "AstStatReturn" and len(last.get("list", [])) == 1 \
                and last["list"][0].get("type") == "AstExprLocal":
            where = last["list"][0]["local"]["location"]
            for st in body:
                if st["type"] == "AstStatLocal" and len(st["vars"]) == 1 and st.get("values") \
                        and st["vars"][0]["location"] == where:
                    v = st["values"][0]
                    if v.get("type") == "AstExprCall" and dotted(v["func"]) in ("Instance.new", "Drawing.new") \
                            and v.get("args") and const_str(v["args"][0]):
                        return camel("create " + const_str(v["args"][0]))
        return None

    def same(self, a, b):
        """Same variable path (locals by declaration, globals by name, .x chains)."""
        if a.get("type") != b.get("type"):
            return False
        t = a["type"]
        if t == "AstExprLocal":
            return a["local"]["location"] == b["local"]["location"]
        if t == "AstExprGlobal":
            return a["global"] == b["global"]
        if t == "AstExprIndexName":
            return a["index"] == b["index"] and self.same(a["expr"], b["expr"])
        return False

    def forin_names(self, st):
        n = len(st["vars"])
        vals = st["values"]
        first = vals[0] if vals else {}
        fn = dotted(first.get("func", {})) if first.get("type") == "AstExprCall" else None
        base = None
        if first.get("type") == "AstExprCall" and first.get("args"):
            a = first["args"][0]
            if a.get("type") == "AstExprLocal":
                base = ("local", a["local"]["location"])
            elif a.get("type") == "AstExprCall" and a["func"].get("type") == "AstExprIndexName":
                base = ("name", METHOD_NAMES.get(a["func"]["index"]))
            elif a.get("type") == "AstExprIndexName":
                base = ("name", camel(a["index"]))                                 
        if first.get("type") == "AstExprCall" and first["func"].get("type") == "AstExprIndexName"                 and first["func"]["index"] == "gmatch":
            return (["match"] + ["match%d" % i for i in range(2, n + 1)])[:n]
        key = "i" if fn == "ipairs" else "k"
        names = [key, ("v", base)] + ["v%d" % i for i in range(3, n + 1)]
        return names[:n]

    def infer(self, e, idx=0):
        t = e.get("type")
        if t == "AstExprCall":
            f = e["func"]
            d = dotted(f)
            if d in ("pcall", "xpcall"):
                return ["ok", "result"][min(idx, 1)]
            if idx > 0:
                return None
            if d == "require" and e["args"]:
                a = e["args"][0]
                if a.get("type") == "AstExprIndexName":
                    return a["index"]
                if a.get("type") == "AstExprCall" and a["func"].get("type") == "AstExprIndexName" \
                        and a["func"]["index"] in ("WaitForChild", "FindFirstChild") and a.get("args"):
                    nm = const_str(a["args"][0])
                    if nm and re.match(r"^[A-Za-z_]\w*$", nm):
                        return nm
                return "module"
            if d in ("Instance.new", "Drawing.new") and e["args"] and const_str(e["args"][0]):
                return camel(const_str(e["args"][0]))
            if d and "." in d and d.split(".")[0] in DATATYPES:
                return DATATYPES[d.split(".")[0]]
            if d in GLOBAL_CALLS:
                return GLOBAL_CALLS[d]
            if f.get("type") == "AstExprIndexName" and f.get("op") == ":":
                m = f["index"]
                arg = const_str(e["args"][0]) if e["args"] else None
                if m == "GetService" and arg and re.match(r"^[A-Za-z]\w*$", arg):
                    return arg
                if m in ("FindFirstChild", "WaitForChild", "FindFirstChildOfClass", "FindFirstChildWhichIsA",
                         "FindFirstAncestor", "FindFirstAncestorOfClass", "FindFirstAncestorWhichIsA") and arg:
                    return camel(arg)
                if m == "IsA" and arg:
                    return camel("is " + arg)
                if m in METHOD_NAMES:
                    return METHOD_NAMES[m]
                if m.startswith("Get") and len(m) > 3:
                    return camel(m[3:])
                for pre in ("Add", "Create", "New", "Make"):
                    if m.startswith(pre) and len(m) > len(pre) and m[len(pre)].isupper():
                        return camel(m[len(pre):])
                return str_arg_name(e)
            if f.get("type") == "AstExprIndexName" and f.get("op") == "." and f.get("index"):
                                                                                                  
                m = f["index"]
                for pre in ("Get", "Add", "Create", "New", "Make", "Find", "Load"):
                    if m.startswith(pre) and len(m) > len(pre) and m[len(pre)].isupper():
                        return camel(m[len(pre):])
            if f.get("type") == "AstExprCall":
                inner = dotted(f.get("func", {}))
                if inner == "loadstring":
                    return "lib"
            if f.get("type") in ("AstExprLocal", "AstExprGlobal") and len(e.get("args") or []) == 1:
                                                                                                     
                a = e["args"][0]
                if a.get("type") == "AstExprCall" and a["func"].get("type") == "AstExprIndexName" \
                        and a["func"].get("index") == "GetService" and a.get("args"):
                    nm = const_str(a["args"][0])
                    if nm and re.match(r"^[A-Za-z]\w*$", nm):
                        return nm
            if f.get("type") == "AstExprLocal":
                                                                             
                nm = str_arg_name(e)
                if nm:
                    return nm
                for a in e.get("args") or []:
                    s = const_str(a)
                    if s is not None:
                        s = re.sub(r"\s*:\s*\w*$", "", s)                                 
                                                                                                  
                        ok = re.match(r"^[A-Z][A-Za-z0-9 _]{1,30}$", s) and camel(s) not in KEYWORDS
                        return camel(s) if ok else None
            return str_arg_name(e)
        if t == "AstExprIndexName":
            d = dotted(e)
            if d and d.split(".")[0] in DATATYPES:
                return DATATYPES[d.split(".")[0]]                             
            return camel(e["index"])
        if t == "AstExprIndexExpr":
            s = const_str(e.get("index"))
            if s:
                return camel(s)
                                                                             
            b = e.get("expr", {})
            if b.get("type") == "AstExprLocal":
                src = self.locals.get(b["local"]["location"])
                nm = src.get("new") or src["name"] if src else None
                if nm and not GENERATED.match(nm) and nm not in WEAK and not re.match(r"^(?:tbl|fn|obj|v)\d*$", nm):
                    sg = singular(nm)
                    return sg if sg and sg != nm else nm + "Item"
            if b.get("type") == "AstExprIndexName":
                nm = camel(b["index"])
                if nm:
                    sg = singular(nm)
                    return sg if sg and sg != nm else nm
            return None
        if t == "AstExprLocal":
                                                      
            src = self.locals.get(e["local"]["location"])
            nm = (src.get("new") or src["name"]) if src else None
            if nm and not GENERATED.match(nm) and not re.match(r"^(?:tbl|fn|obj|v|flag|n|str|arg)\d*$", nm):
                return nm
            return None
        if t == "AstExprFunction":
            return self.function_name(e) or "fn"
        if t == "AstExprTable":
            return "tbl"
        if t == "AstExprBinary":
            if e["op"] == "Or":
                                               
                left = self.infer(e["left"])
                if left and left not in WEAK:
                    return left
                return self.infer(e["right"]) or left
            if e["op"] == "And":
                return self.infer(e["right"]) or self.infer(e["left"])
            if e["op"].startswith("Compare"):
                                                                                                           
                l, r = e["left"], e["right"]
                if l.get("type") == "AstExprCall" and dotted(l["func"]) in ("type", "typeof") and const_str(r):
                    return camel("is " + const_str(r))
                if e["op"] in ("CompareEq", "CompareNe") and l.get("type") == "AstExprIndexName":
                    rn = const_str(r)
                    if rn is None and r.get("type") == "AstExprLocal":
                        src = self.locals.get(r["local"]["location"])
                        rn = src and (src.get("new") or src["name"])
                        if rn and (GENERATED.match(rn) or re.match(r"^(?:tbl|fn|obj|v|flag|n|str|arg)\d*$", rn)):
                            rn = None
                    elif rn is None and r.get("type") == "AstExprGlobal":
                        rn = r["global"]
                    if rn and re.match(r"^[A-Za-z][A-Za-z0-9 ]{0,20}$", rn):
                        nm = camel(l["index"] + (" is " if e["op"] == "CompareEq" else " not ") + rn)
                        if nm and len(nm) <= 30:
                            return nm
                return "flag"
            if e["op"] == "Concat":
                return "str"
            return "n"
        if t == "AstExprUnary":
            return "flag" if e["op"] == "Not" else "n"
        if t == "AstExprConstantBool":
            return "flag"
        if t == "AstExprConstantString":
            return "str"
        if t == "AstExprConstantNumber":
            return "n"
        if t == "AstExprGroup":
            return self.infer(e["expr"], idx)
        return None

    USE_RULES = [
                                              
        ("Players", {"LocalPlayer", "PlayerAdded", "PlayerRemoving"}, {"GetPlayers", "GetPlayerFromCharacter", "GetPlayerByUserId"}),
        ("RunService", {"Heartbeat", "RenderStepped", "Stepped", "PreSimulation", "PostSimulation", "PreRender"}, {"IsStudio", "BindToRenderStep", "UnbindFromRenderStep"}),
        ("UserInputService", {"InputBegan", "InputEnded", "InputChanged", "TouchEnabled", "KeyboardEnabled", "MouseEnabled"}, {"GetMouseLocation", "IsKeyDown", "GetFocusedTextBox"}),
        ("HttpService", set(), {"JSONEncode", "JSONDecode", "GenerateGUID", "UrlEncode"}),
        ("TextService", set(), {"GetTextSize"}),
        ("TeleportService", set(), {"Teleport", "TeleportToPlaceInstance"}),
        ("CollectionService", set(), {"GetTagged", "GetInstanceAddedSignal", "HasTag"}),
        ("Lighting", {"ClockTime", "Brightness", "FogEnd", "GlobalShadows", "Ambient"}, set()),
        ("camera", {"FieldOfView", "ViewportSize"}, {"WorldToViewportPoint", "WorldToScreenPoint", "ViewportPointToRay"}),
        ("player", {"UserId", "DisplayName", "Character", "Team", "PlayerGui", "Backpack", "CharacterAdded"}, {"Kick", "GetMouse", "GetRoleInGroup", "GetRankInGroup", "IsInGroup", "IsFriendsWith"}),
        ("character", {"Humanoid", "HumanoidRootPart", "PrimaryPart"}, {"GetPivot", "PivotTo"}),
        ("humanoid", {"Health", "MaxHealth", "WalkSpeed", "JumpPower", "MoveDirection"}, {"MoveTo", "LoadAnimation", "ChangeState"}),
        ("input", {"KeyCode", "UserInputType", "UserInputState"}, set()),
        ("connection", set(), {"Disconnect"}),
        ("tween", set(), {"Play", "Cancel"}),
        ("signal", set(), {"Connect", "Once", "Fire"}),
        ("remote", set(), {"FireServer", "InvokeServer"}),
        ("part", {"CFrame", "Size", "Anchored", "CanCollide", "AssemblyLinearVelocity", "Velocity"}, set()),
        ("gui", {"Text", "TextColor3", "BackgroundColor3", "BackgroundTransparency", "Size", "Visible", "Position"}, set()),
        ("instance", {"Parent", "Name", "ClassName"}, {"Destroy", "IsA", "FindFirstChild", "WaitForChild",
                                                        "GetChildren", "GetDescendants", "GetAttribute", "SetAttribute"}),
        ("str", set(), {"sub", "lower", "upper", "match", "gsub", "find", "split", "gmatch", "len", "format", "rep", "byte"}),
    ]

    def usage_name(self, info):
        """deobf: a name from how the local is used (fields read, methods called)."""
        if info.get("counter"):
            return "count"
        reads = set(info.get("reads", ()))
        methods = set(info.get("methods", ()))
        mo = info.get("method_of")
        if mo and (reads or methods) and (":" + mo + "(") in self.text:
            return "self"
        for nm, rd, md in self.USE_RULES:
            if (reads & rd) or (methods & md):
                if nm == "gui" and len(reads & rd) < 2:
                    continue
                return nm
        if info.get("eqfield") and info["eqfield"] not in ("name",):
            return info["eqfield"]
        if info.get("argof"):
            return info["argof"]
        if info.get("hint") == "arg" and info.get("key_loc") in getattr(self, "_cbparams", ()):
            return "value"
        if info.get("as_key") and not reads and not methods:
            return "key"
        if info.get("called") and not reads and not methods:
            return "callback" if info.get("hint") == "arg" else None
        if info.get("iterated") and not reads:
            return "list"
        if info.get("concat") and not reads and not methods and not info.get("indexed"):
            return "text"
        if info.get("eqstr") and not reads and not methods:
            return "kind"
        if info.get("eqfield"):
            return info["eqfield"]
        if reads:
            c = {}
            for r in info["reads"]:
                c[r] = c.get(r, 0) + 1
            if len(c) >= 3:
                return "data" if info.get("hint") != "arg" else "options"
        return None

    def base_name(self, info):
        nm = self.base_name0(info)
        if info.get("connected") and nm in (None, "fn", "v", "callback"):
            return camel("on " + info["connected"])
        if nm in (None, "v", "arg", "tbl", "fn", "obj", "value", "result", "flag", "str", "n") or nm.endswith("_"):
            un = self.usage_name(info)
            if un and not (nm in ("flag", "n", "str") and un in ("text", "list", "data", "options")):
                return un
        return nm

    def base_name0(self, info):
        h = info["hint"]
        if h == "arg" and (info.get("label") or info.get("prop")):
            return info.get("label") or info["prop"]                                   
        if isinstance(h, tuple):
            key, base = h
            if base is not None:
                if base[0] == "local":
                    src = self.locals.get(base[1])
                    nm = singular(src.get("new")) if src else None
                    if nm:
                        return nm
                else:
                    nm = singular(base[1])
                    if nm:
                        return nm
            return key
        if h:
            return h
        for expr, idx in info["hints"]:
            nm = self.infer(expr, idx)
            if nm:
                if nm == "tbl" and info.get("dispatch"):
                    return "handlers"
                if nm == "tbl" and info.get("prop"):
                    return info["prop"]
                if nm == "tbl" and info.get("fields"):
                    fl = info["fields"]
                    caps = [f for f in fl if f[:1].isupper()]
                    if len(caps) >= 8 and any(re.match(r"^Load[A-Z]", f) for f in caps):
                        return "Modules"
                    if len(fl) >= 1 and all(f[:1].islower() for f in fl) and len(fl) <= 3:
                        return None if False else "tbl"
                if nm in WEAK and (info.get("label") or info.get("prop")):
                    return info.get("label") or info["prop"]
                return nm
        return info.get("label") or "v"

    def run(self):
        self.collect()
        taken = {}                           
        reserved = self.globals | KEYWORDS
        for key in self.order:
            info = self.locals[key]
            info["refs"].sort()
            if not GENERATED.match(info["name"]):
                info["new"] = info["name"]
                taken.setdefault(info["name"], []).append(info)
        for key in sorted(self.order, key=lambda k: self.locals[k]["decl"]):                                
            info = self.locals[key]
            if "new" in info:
                continue
            base = self.base_name(info)
            if base == "self" and self.fits("self", info, taken):
                info["new"] = "self"
                taken.setdefault("self", []).append(info)
                continue
            if base == "self":
                base = "obj"
            if not base or not re.match(r"^[A-Za-z_]\w*$", base) or base in reserved:
                base = (base or "v") + "_"
                if not re.match(r"^[A-Za-z_]\w*$", base):
                    base = "v"
            n = 1
            while True:
                cand = base if n == 1 else "%s%d" % (base, n)
                if cand not in reserved and self.fits(cand, info, taken):
                    break
                n += 1
            info["new"] = cand
            taken.setdefault(cand, []).append(info)
        return self.rewrite()

    def fits(self, cand, info, taken):
        """No other local named `cand` has a scope overlapping this one's: a
        generated name never shadows (Luau lint: LocalShadow), even where it
        would be legal because the outer one isn't referenced inside."""
        s0, s1 = info["scope"]
        for other in taken.get(cand, ()):
            o0, o1 = other["scope"]
            if s0 < o1 and o0 < s1:
                return False
        return True

    def rewrite(self):
        lines = self.text.split("\n")
        edits = {}
        for info in self.locals.values():
            if info["new"] == info["name"]:
                continue
            for (a, b) in info["spans"]:
                edits.setdefault(a[0], []).append((a[1], b[1], info["name"], info["new"]))
        for ln, lst in edits.items():
            s = lines[ln].encode("utf-8")                                          
            for c0, c1, old, new in sorted(lst, reverse=True):
                if s[c0:c1] == old.encode("utf-8"):
                    s = s[:c0] + new.encode("utf-8") + s[c1:]
            lines[ln] = s.decode("utf-8")
        return "\n".join(lines)

def rename_text(text):
    return Namer(text).run()

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()
    with open(a.file, encoding="utf-8") as f:
        text = f.read()
    out = rename_text(text)
    with open(a.output or a.file, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)

if __name__ == "__main__":
    main()
