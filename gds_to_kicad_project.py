#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""GDS -> KiCad project (interposer board).

Third output mode of the gds_to_kicad tool. Where gds_to_kicad.py makes a footprint
and gds_to_kicad_symbol.py makes a symbol, this makes a whole KiCad project
(.kicad_pro + .kicad_sch + .kicad_pcb) from a stripped interposer GDS (TopMetal2
copper + text only). The result is a base for the user to extend in KiCad, not a
finished board.

Pipeline: parse the GDS into an InterposerModel (classify shapes, extract nets),
then emit the KiCad files from that model. This phase exposes the model stage
(``--dump-model``) and the summary; emission is added on top of the same model.
"""

import argparse
import sys

from interposer_profile import InterposerProfile, _as_layer
from interposer_model import build_model


def _build_profile(args) -> InterposerProfile:
    profile = InterposerProfile.load(args.profile) if args.profile else InterposerProfile()
    overlay = {}
    if args.pad_layer_number:
        overlay["pad_layer"] = _as_layer(args.pad_layer_number)
    if args.text_layer_number:
        overlay["label_layer"] = _as_layer(args.text_layer_number)
    if args.board_outline:
        overlay["board_outline_src"] = args.board_outline
    if args.no_fill:
        overlay["keep_fill"] = False
    return profile.merge(overlay)


def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="gds_to_kicad_project",
        description="Convert a stripped interposer GDS into a KiCad project.")
    p.add_argument("input", help="stripped GDS (TopMetal2 copper + text only)")
    p.add_argument("--pad-layer-number", metavar="L/D",
                   help="copper layer as layer/datatype (default 134/0)")
    p.add_argument("--text-layer-number", metavar="L/D",
                   help="net-label text layer as layer/datatype (default 134/25)")
    p.add_argument("--profile", metavar="JSON",
                   help="classification profile overlay (see InterposerProfile)")
    p.add_argument("--full-gds", metavar="GDS",
                   help="un-stripped GDS; its 41/35, 41/0 markers calibrate and "
                        "override pillar/bond-pad classification")
    p.add_argument("--pin-list", metavar="JSON",
                   help="reviewed pin list to override pad names by proximity")
    p.add_argument("--board-outline", choices=["frame", "plane", "bbox"],
                   help="where the Edge.Cuts outline comes from (default frame)")
    p.add_argument("--no-fill", action="store_true",
                   help="drop dummy fill metal from the model/output")
    p.add_argument("--out-dir", metavar="DIR",
                   help="directory for the emitted project (default: cwd)")
    p.add_argument("--dump-model", metavar="JSON",
                   help="write the intermediate model as JSON and stop")
    p.add_argument("--emit", choices=["model", "pcb", "sch", "pro", "all"],
                   default="model", help="what to emit (default: model only)")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    pin_list = None
    if args.pin_list:
        from pin_list import PinList
        pin_list = PinList.load(args.pin_list)

    profile = _build_profile(args)
    model = build_model(args.input, profile=profile, full_gds=args.full_gds,
                        pin_list=pin_list)

    print(model.summary())
    for w in model.metadata.get("net_warnings", []):
        print(f"  note: {w}", file=sys.stderr)

    if args.dump_model:
        with open(args.dump_model, "w", encoding="utf-8") as f:
            f.write(model.to_json())
        print(f"model -> {args.dump_model}")

    if args.emit != "model":
        try:
            import kicad_project_writer
        except ImportError:
            print("error: project emission is not available in this build yet "
                  "(use --dump-model to inspect the model)", file=sys.stderr)
            return 2
        out_dir = args.out_dir or "."
        written = kicad_project_writer.write_project(
            model, out_dir, emit=args.emit, template_pcb=None)
        for path in written:
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
