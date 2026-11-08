# SPDX-License-Identifier: GPL-3.0-or-later
"""Drawing on a handout's empty sheet as a student would, for the tests that
read such a sheet back."""
import copy

import chiplet_to_kicad_sch as g
import sexpr_io as sx


def draw_from_library(handout, project, placements, nets, *, libraries=None,
                      properties=None, no_connect=(), shown=None, wires=()):
    """Draw on the handout's empty sheet: each (reference, cell, x, y) placed
    unturned from the handout library, and a wire and a global label on every
    pin of ``nets``; the other pins are left bare.

    ``libraries`` maps extra nicknames to files. ``properties`` overrides each
    reference's copied library properties. Copying them all matches placement
    in KiCad; they are hidden, Reference and Value included, since these cases
    test connectivity, except the names ``shown`` lists for a reference, which
    sit where the library puts them. ``no_connect`` are (reference, pin) pairs
    flagged open. ``wires`` are pairs of (reference, pin) joined by one
    straight wire and no label, as a power symbol is to the pad it feeds."""
    lib = sx.parse((handout / g.HANDOUT_LIB_FILE).read_text(encoding="utf-8"))
    library = {str(sym[1]): sym for sym in sx.find_all(lib, "symbol")}
    for nickname, lib_path in (libraries or {}).items():
        extra = sx.parse(lib_path.read_text(encoding="utf-8"))
        library.update({f"{nickname}:{sym[1]}": sym for sym in sx.find_all(extra, "symbol")})
    path = handout / f"{project}.kicad_sch"
    sch = sx.parse(path.read_text(encoding="utf-8"))
    sheet = str(sx.find(sch, "uuid")[1])
    cache = sx.find(sch, "lib_symbols")
    net_of = {ep: name for name, eps in nets.items() for ep in eps}
    items, ends = [], {}
    for ref, cell, ox, oy in placements:
        lib_id = cell if ":" in cell else f"{g.HANDOUT_LIB}:{cell}"
        sym = copy.deepcopy(library[cell])
        sym[1] = sx.Quoted(lib_id)
        if lib_id not in {str(c[1]) for c in sx.find_all(cache, "symbol")}:
            cache.append(sym)
        pins = {str(sx.find(p, "number")[1]): (float(sx.find(p, "at")[1]),
                                                 float(sx.find(p, "at")[2]))
                for p in sx.find_all_rec(sym, "pin")}
        inst = ["symbol", ["lib_id", sx.Quoted(lib_id)],
                ["at", sx.num(ox), sx.num(oy), "0"], ["unit", "1"],
                ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
                ["dnp", "no"], ["uuid", sx.Quoted(g.uid("drawn", ref))]]
        lib_props = {str(p[1]): p for p in sx.find_all(sym, "property")}
        visible = set((shown or {}).get(ref, ()))
        if visible - lib_props.keys():
            raise ValueError(f"{ref} ({cell}) has no library properties "
                             f"{sorted(visible - lib_props.keys())} to show")
        defaults = {name: str(p[2]) for name, p in lib_props.items()}
        defaults.update((properties or {}).get(ref, {}), Reference=ref)
        for i, (name, value) in enumerate(defaults.items()):
            if name in visible:
                lx, ly = (float(v) for v in sx.find(lib_props[name], "at")[1:3])
                inst.append(g._prop(name, value, (ox + lx, oy - ly),
                                    show_name=sx.find(lib_props[name], "show_name") is not None))
            else:
                inst.append(g._prop(name, value, (ox, oy + 1.27 * i), hide=True))
        inst += [["pin", sx.Quoted(num), ["uuid", sx.Quoted(g.uid("drawn", ref, num))]]
                 for num in pins]
        inst.append(["instances", ["project", sx.Quoted(project),
                                   ["path", sx.Quoted(f"/{sheet}"),
                                    ["reference", sx.Quoted(ref)], ["unit", "1"]]]])
        items.append(inst)
        ends.update({(ref, num): (ox + px, oy - py) for num, (px, py) in pins.items()})
        for num, (px, py) in pins.items():
            if (ref, num) in no_connect:
                items.append(["no_connect", ["at", sx.num(ox + px), sx.num(oy - py)],
                              ["uuid", sx.Quoted(g.uid("drawn-nc", ref, num))]])
            if (ref, num) in net_of:
                items += g._hookup(ox + px, oy - py, "left" if px < 0 else "right",
                                   net_of[(ref, num)], ref, num)
    for a, b in wires:
        if ends[a][0] != ends[b][0] and ends[a][1] != ends[b][1]:
            raise ValueError(f"{a} at {ends[a]} and {b} at {ends[b]} are on no one line")
        items.append(g._wire(ends[a], ends[b], "drawn", *a, *b))
    at = next(i for i, c in enumerate(sch) if sx.head(c) == "sheet_instances")
    sch[at:at] = items
    path.write_text(sx.dumps(sch) + "\n", encoding="utf-8")
    return path
