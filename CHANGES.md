# tool-improved: changes vs. the zip deobfuscator (Luraph v15 plugin)

Run (Linux): `python3 deobf/deob.py <script.lua> -o out.lua` (needs deobf/bin/luau + luau-ast built from
Luau 0.739 with build_luau.py's vector patch; deobf/bin/stylua optional; pypy3 optional).

Lifter (obfuscators/luraph_v15/devirt.py, structure.py)
- Plain-Lua closures (Luraph's `function(T,T,T,F) return function() ... end end` maker) are lifted as
  source instead of failing ("value <LuaFunc> in an expression"): 84 -> 939 functions. Their captures
  are resolved to the parent's variable names (PlainProto ClosureExpr), and their minified locals get
  generated names so the naming pass renames them.
- Empty packed list (`table.pack()` = {n=0}) stored in a register is lifted (was the last unlifted block).
- A for loop whose body never loops back (LPH_CRASH trap) is emitted as a for statement, not an error.
- `local ... = ...` no longer emitted at top level; INLINE_CONST_LOCALS enabled.

Post passes (deobf/postpass.py, run automatically)
- Luarmor string decryption: finds the loader's PRNG decryptor in the lifted code, extracts its
  constants, decodes all 381 call sites offline (before naming).
- t["x"] -> t.x, {["x"]=v} -> {x=v}; dead pure locals removed (81 string-cache aliases);
  `for k,v in t, nil, nil` -> `for k,v in t`; LPH_ENCFUNC constant pool named serverConsts with
  single-use copies inlined; `T.f = function(self, ...)` -> `function T:f(...)`; StyLua formatting.

Naming (deobf/names.py)
- Service names through cloneref(game:GetService("X")); usage-based names (player, character,
  humanoid, input, connection, services by members, self for methods, list/text/key/count ...);
  function names from bodies (tween, create, isValidX, reset, loop, onXChanged for connected
  callbacks), table-field / table-key names, comparison-derived flag names.

## undarklua pass (deobf/undarklua.py)
Runs after postpass (disable with DEOBF_NO_UNDARKLUA=1). AST-guided (luau-ast), semantics-preserving rewrites to idiomatic Luau:
- compound: `t.f = t.f op e` -> `t.f op= e` (target local / local.f / local[const] only)
- truncate: `({ f() })[1]` -> `(f())`
- ifexpr: `local x; if c then x=a else x=b end` -> `local x = if c then a else b` (single-assignment branches, no closures, <=200 chars)
- andor: `if c then x=c else x=e end` -> `x = c or e`; `if c then x=v else x=c end` -> `x = c and v` (c a local)
- detects darklua bundle wrappers (none present in FlyteBB: bundling/renaming/type removal leave no trace after Luraph bytecode compilation)
Final file formatting: /workspace/deob/finalize.py src dst.
