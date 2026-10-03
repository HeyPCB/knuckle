"""Knuckle's printed parts, built around the real board and the real STS3215 horn, with a fit check.

Run from the repository root with the CAD environment (CadQuery 2.x, OCP, trimesh):

    python boards/knuckle/cad/parts.py   # a Python with CadQuery and trimesh

Inputs (all measured, none typed):
  build/knuckle/fab/knuckle.step      the board as the pipeline exported it (KiCad 10 STEP, solids named by reference)
  build/knuckle/knuckle.kicad_pcb     J1/J2 positions and F.Fab outlines (KiCad 10 has no 3D model for the
                                      Molex 5267-03A, so the STEP has no header body)
  STS3215_03a.step                    TheRobotStudio SO-ARM100 (Apache-2.0), the horn the adapter must match

Outputs, in build/knuckle/cad/: standoff, magnet_holder, cover, horn_adapter as STEP and STL, assembly.step,
fit.json (every number measured on the solids, and each STL read back for watertightness), exploded.svg/png
and section.svg/png.

The frame: z is the motor axis, z = 0 the motor's base (stator) face, +z toward the board. X/Y are the board's
own axes with the origin on the motor axis and +Y toward bus socket J1.

What is not measured here, and is taken from the actuator spec (research/actuator/spec.md, section 2.4 and 6),
each one flagged "est." in fit.json: the GBM2804H-100T's height (15 mm), its shaft stub (7 mm, protruding 1.2 mm
from the base face), its base-hole pattern (default: a 16 mm pair and a 19 mm pair, M2), the magnet (6 x 2.5 mm)
and the 5267-03A header height (5.9 mm, from its catalogue row). Measure one motor and change MOTOR below.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

import cadquery as cq
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cylinder

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
# the research folder sits beside the repository (kicad-projects/robots); from a worktree under .claude/worktrees
# it is four levels further up. The first one that exists is the default; --sts-step names any other.
_STS_REL = os.path.join("robots", "research", "actuator", "cad", "STS3215_03a.step")
_STS_CANDIDATES = [os.path.abspath(os.path.join(ROOT, "..", _STS_REL)),
                   os.path.abspath(os.path.join(ROOT, "..", "..", "..", "..", _STS_REL))]
STS_DEFAULT = next((c for c in _STS_CANDIDATES if os.path.exists(c)), _STS_CANDIDATES[0])

# --- what the spec states and this script cannot measure (est.; measure one motor) -------------------------
MOTOR = {
    "height_mm": 15.0,            # base face to rotor top [IF2804]
    "rotor_od_mm": 35.0,          # [IF2804]
    "shaft_od_mm": 7.0,           # hollow shaft OD [IF2804]
    "shaft_stub_mm": 1.2,         # protrusion from the base face [RIA], cross-check only
    "base_pairs": [(16.0, 45.0), (19.0, 135.0)],   # (pair spacing mm, axis angle deg): the 16 / 19 mm base pattern
    "screw": "M2",
}
MAGNET = {"d_mm": 6.0, "t_mm": 2.5}            # MT6701 magnet table (diametric NdFeB)
HEADER_HEIGHT_MM = 5.9                          # Molex 5267-03A body height, catalogue row C45581067 ("5.9mm")
AIR_GAP_TARGET_MM = 1.0                         # MT6701 air gap 0.5 / 1.0 / 2.0 mm (min / typ / max)
SCREW = {"M2": {"clear": 2.4, "head_d": 3.8, "head_h": 2.0}, "M3": {"clear": 3.4, "head_d": 5.6, "head_h": 3.0}}

# --- the parts' own design numbers --------------------------------------------------------------------------
HOLDER = {"od": 9.0, "bore": 6.98, "bore_depth": 1.0, "floor": 0.6, "pocket_d": 6.05, "lift": 0.2}
STANDOFF = {"boss_d": 5.6, "ring_id": 11.0, "ring_od": 25.0, "ring_h": 1.5}
COVER = {"wall": 1.2, "radial_clear": 0.3, "roof": 2.0, "header_clear": 0.5, "opening_clear": 0.4,
         "insert_d": 4.0, "insert_depth": 4.5, "insert_boss_d": 7.0, "insert_r": 14.0,
         "insert_angles": (20.0, 160.0, 200.0, 340.0), "post_d": 5.6, "lead_notch": (6.0, 2.0)}
ADAPTER = {"od": 23.0, "thick": 4.0, "relief_d": 7.5, "insert_d": 4.0, "insert_depth": 3.5,
           "rotor_slot": (6.0, 8.49), "rotor_slot_w": 2.4, "cbore_w": 4.2, "cbore_depth": 2.0}


# ============================================================================== readers
def read_named_step(path):
    """{name: TopoDS_Shape} for every named component of a KiCad STEP, located in the file's frame, plus the
    unnamed board body under the key 'BOARD'."""
    doc = TDocStd_Document(TCollection_ExtendedString("doc"))
    r = STEPCAFControl_Reader()
    r.SetNameMode(True)
    if r.ReadFile(path) != IFSelect_RetDone:
        raise SystemExit("cannot read %s" % path)
    r.Transfer(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    free = TDF_LabelSequence()
    st.GetFreeShapes(free)
    out = {}

    def name(lab):
        n = TDataStd_Name()
        return n.Get().ToExtString() if lab.FindAttribute(TDataStd_Name.GetID_s(), n) else ""

    for i in range(1, free.Length() + 1):
        comps = TDF_LabelSequence()
        st.GetComponents_s(free.Value(i), comps)
        for k in range(1, comps.Length() + 1):
            lab = comps.Value(k)
            nm = name(lab)
            shape = st.GetShape_s(lab)
            key = nm if re.match(r"^[A-Z]+\d+$", nm) else "BOARD"
            if key == "BOARD" and "BOARD" in out:
                key = "BOARD_%d" % k
            out[key] = cq.Shape.cast(shape)
    return out


def sexpr(text):
    tok = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)
    stack, cur = [], []
    for t in tok:
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done = cur
            cur = stack.pop()
            cur.append(done)
        else:
            cur.append(t[1:-1] if t.startswith('"') else t)
    return cur[0]


def footprints(pcb_path):
    """[(ref, fpid, x, y, rot, side, fab_box)] from the .kicad_pcb, fab_box the F.Fab/B.Fab extent in footprint
    coordinates."""
    root = sexpr(open(pcb_path).read())
    out = []
    for fp in root:
        if not (isinstance(fp, list) and fp and fp[0] == "footprint"):
            continue
        at = next(x for x in fp if isinstance(x, list) and x[0] == "at")
        layer = next(x for x in fp if isinstance(x, list) and x[0] == "layer")[1]
        ref = next(x[2] for x in fp if isinstance(x, list) and x[0] == "property" and x[1] == "Reference")
        xs, ys = [], []
        for g in fp:
            if not (isinstance(g, list) and g and g[0] in ("fp_line", "fp_rect", "fp_poly")):
                continue
            lay = next((x[1] for x in g if isinstance(x, list) and x[0] == "layer"), "")
            if not lay.endswith(".Fab"):
                continue
            for x in g:
                if isinstance(x, list) and x[0] in ("start", "end"):
                    xs.append(float(x[1])); ys.append(float(x[2]))
        out.append((ref, fp[1], float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0,
                    "bottom" if layer == "B.Cu" else "top", (min(xs), min(ys), max(xs), max(ys)) if xs else None))
    return out


def sts_horn(path):
    """The STS3215 output horn from the STEP: its disc (the D 20 cylinder on the output face), its thickness, and
    the four screw holes on it (centres relative to the horn axis)."""
    shape = cq.importers.importStep(path)
    discs, holes = [], []
    for f in shape.faces().vals():
        ad = BRepAdaptor_Surface(f.wrapped)
        if ad.GetType() != GeomAbs_Cylinder:
            continue
        c = ad.Cylinder()
        loc = c.Axis().Location()
        fb = f.BoundingBox()
        d = 2 * c.Radius()
        if abs(d - 20.0) < 0.5:
            discs.append((loc.X(), loc.Y(), fb.zmin, fb.zmax, d))
        elif d < 3.0:
            holes.append((loc.X(), loc.Y(), fb.zmin, fb.zmax, d))
    top = max(discs, key=lambda t: t[3])           # the output horn is the disc highest in z
    cx, cy, z0, z1, d = top
    on = [(x - cx, y - cy, hd) for x, y, a, b, hd in holes
          if a >= z0 - 0.05 and b <= z1 + 0.05 and math.hypot(x - cx, y - cy) > 1.0]
    uniq = []
    for h in on:
        if not any(math.hypot(h[0] - u[0], h[1] - u[1]) < 0.05 for u in uniq):
            uniq.append(h)
    radii = [math.hypot(x, y) for x, y, _ in uniq]
    return {"od": round(d, 3), "thickness": round(z1 - z0, 3),
            "holes": [(round(x, 3), round(y, 3)) for x, y, _ in uniq], "hole_d": round(uniq[0][2], 3) if uniq else None,
            "bolt_circle_d": round(2 * sum(radii) / len(radii), 3) if radii else None}


def render_svg(items, path, view, up=(0, 0, 1), size=(1100, 1300), margin=50, stroke=1.1, labels=(), fills=()):
    """Hidden-line drawing of `items` (CadQuery shapes) seen from `view` with `up` pointing up the page:
    CadQuery's own exporter lets OCC choose the picture's x axis, which turned the stack sideways."""
    from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir, gp_Vec
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.BRepLib import BRepLib
    from cadquery.occ_impl.exporters.svg import getPaths
    n = gp_Vec(*view).Normalized()
    u = gp_Vec(*up)
    u = (u - n.Multiplied(u.Dot(n))).Normalized()
    r = u.Crossed(n).Normalized()
    ax = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(n), gp_Dir(r))
    hlr = HLRBRep_Algo()
    hlr.Add(cq.Compound.makeCompound(items).wrapped)
    hlr.Projector(HLRAlgo_Projector(ax))
    hlr.Update()
    hlr.Hide()
    out = HLRBRep_HLRToShape(hlr)
    vis = [c for c in (out.VCompound(), out.Rg1LineVCompound(), out.OutLineVCompound()) if not c.IsNull()]
    for c in vis:
        BRepLib.BuildCurves3d_s(c, 1e-4)
    vis = [cq.Shape.cast(c) for c in vis]
    _, paths = getPaths(vis, [])
    bb = cq.Compound.makeCompound(vis).BoundingBox()
    k = min((size[0] - 2 * margin - 260) / bb.xlen, (size[1] - 2 * margin) / bb.ylen)
    tx, ty = margin - bb.xmin * k, margin + bb.ymax * k
    body = "".join('<path d="%s"/>\n' % p for p in paths)
    under = ""
    for face_shape, color in fills:                     # filled cut faces, drawn under the lines
        for f in face_shape.Faces():
            verts, tris = f.tessellate(0.02)
            pts = [(gp_Vec(v.x, v.y, v.z).Dot(r), gp_Vec(v.x, v.y, v.z).Dot(u)) for v in verts]
            for a, b, c in tris:
                under += '<polygon points="%s" fill="%s" stroke="%s" stroke-width="%.4f"/>' % (
                    " ".join("%.4f,%.4f" % pts[i] for i in (a, b, c)), color, color, 0.3 / k)
    text = ""
    for (x, y, z), label in labels:
        px = gp_Vec(x, y, z).Dot(r) * k + tx
        py = -gp_Vec(x, y, z).Dot(u) * k + ty
        text += ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#888" stroke-width="0.8"/>'
                 '<text x="%.1f" y="%.1f" font-family="Helvetica, Arial" font-size="19" fill="#222">%s</text>\n'
                 % (px, py, bb.xmax * k + tx + 20, py, bb.xmax * k + tx + 26, py + 6, label))
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
           '<rect width="100%%" height="100%%" fill="white"/>'
           '<g transform="translate(%.3f,%.3f) scale(%.5f,%.5f)" fill="none" stroke="#1d2433" stroke-width="%.4f" '
           'stroke-linejoin="round" stroke-linecap="round"><g stroke="none">%s</g>%s</g>%s</svg>'
           % (size[0], size[1], size[0], size[1], tx, ty, k, -k, stroke / k, under, body, text))
    open(path, "w").write(svg)


def dist(a, b):
    m = BRepExtrema_DistShapeShape(a.wrapped, b.wrapped)
    m.Perform()
    return m.Value()


# ============================================================================== parts
def magnet_holder():
    h = HOLDER
    height = h["bore_depth"] + h["floor"] + MAGNET["t_mm"]
    body = cq.Workplane("XY").circle(h["od"] / 2).extrude(height)
    body = body.cut(cq.Workplane("XY").circle(h["bore"] / 2).extrude(h["bore_depth"]))
    body = body.cut(cq.Workplane("XY").workplane(offset=height - MAGNET["t_mm"]).circle(h["pocket_d"] / 2)
                    .extrude(MAGNET["t_mm"]))
    z0 = MOTOR["shaft_stub_mm"] - h["bore_depth"]          # rides on the shaft end; 0.2 mm above the base face
    return body.translate((0, 0, z0)), z0, z0 + height


def magnet(z_top):
    return cq.Workplane("XY").circle(MAGNET["d_mm"] / 2).extrude(MAGNET["t_mm"]).translate((0, 0, z_top - MAGNET["t_mm"]))


def screw_points():
    pts = []
    for spacing, ang in MOTOR["base_pairs"]:
        for s in (1, -1):
            a = math.radians(ang)
            pts.append((s * spacing / 2 * math.cos(a), s * spacing / 2 * math.sin(a)))
    return pts


def standoff(height):
    s = STANDOFF
    clear = SCREW[MOTOR["screw"]]["clear"]
    ring = cq.Workplane("XY").circle(s["ring_od"] / 2).circle(s["ring_id"] / 2).extrude(s["ring_h"])
    for x, y in screw_points():
        boss = cq.Workplane("XY").center(x, y).circle(s["boss_d"] / 2).extrude(height)
        ring = ring.union(boss)
    for x, y in screw_points():
        ring = ring.cut(cq.Workplane("XY").center(x, y).circle(clear / 2).extrude(height))
    # the spinning holder must clear the ring and the bosses
    return ring.cut(cq.Workplane("XY").circle(s["ring_id"] / 2).extrude(height))


def cover(z_board_top, z_roof_under, openings):
    c = COVER
    rid = 35.0 / 2 + c["radial_clear"]
    rod = rid + c["wall"]
    top = z_roof_under + c["roof"]
    shell = cq.Workplane("XY").circle(rod).extrude(top).cut(cq.Workplane("XY").circle(rid).extrude(z_roof_under))
    head = SCREW[MOTOR["screw"]]
    for x, y in screw_points():
        post = cq.Workplane("XY").workplane(offset=z_board_top).center(x, y).circle(c["post_d"] / 2) \
            .extrude(z_roof_under - z_board_top)
        shell = shell.union(post)
        shell = shell.cut(cq.Workplane("XY").workplane(offset=z_board_top).center(x, y).circle(head["clear"] / 2)
                          .extrude(top - z_board_top))
        shell = shell.cut(cq.Workplane("XY").workplane(offset=top - head["head_h"]).center(x, y)
                          .circle(head["head_d"] / 2 + 0.2).extrude(head["head_h"]))
    for ang in c["insert_angles"]:
        a = math.radians(ang)
        x, y = c["insert_r"] * math.cos(a), c["insert_r"] * math.sin(a)
        boss_h = c["insert_depth"] - c["roof"]
        shell = shell.union(cq.Workplane("XY").workplane(offset=z_roof_under - boss_h).center(x, y)
                            .circle(c["insert_boss_d"] / 2).extrude(boss_h))
        shell = shell.cut(cq.Workplane("XY").workplane(offset=top - c["insert_depth"]).center(x, y)
                          .circle(c["insert_d"] / 2).extrude(c["insert_depth"]))
    for x0, y0, x1, y1 in openings:
        shell = shell.cut(cq.Workplane("XY").workplane(offset=z_roof_under - 0.1)
                          .center((x0 + x1) / 2, (y0 + y1) / 2).rect(x1 - x0, y1 - y0).extrude(c["roof"] + 0.2))
    w, h = c["lead_notch"]
    shell = shell.cut(cq.Workplane("XY").center(rid + c["wall"] / 2, 0).rect(c["wall"] * 3, w).extrude(h))
    return shell, top


def horn_adapter(horn):
    a = ADAPTER
    body = cq.Workplane("XY").circle(a["od"] / 2).extrude(a["thick"])
    body = body.cut(cq.Workplane("XY").circle(a["relief_d"] / 2).extrude(a["thick"]))
    for x, y in horn["holes"]:                          # joint side (z = thick) carries the STS pattern
        body = body.cut(cq.Workplane("XY").workplane(offset=a["thick"] - a["insert_depth"]).center(x, y)
                        .circle(a["insert_d"] / 2).extrude(a["insert_depth"]))
    r0, r1 = a["rotor_slot"]
    for ang in (0, 90, 180, 270):                       # rotor side: radial slots for 12 mm c-c ... 16 mm BC
        t = math.radians(ang)
        mid, length = (r0 + r1) / 2, r1 - r0
        slot = cq.Workplane("XY").center(mid * math.cos(t), mid * math.sin(t)) \
            .slot2D(length + a["rotor_slot_w"], a["rotor_slot_w"], ang).extrude(a["thick"])
        cb = cq.Workplane("XY").workplane(offset=a["thick"] - a["cbore_depth"]) \
            .center(mid * math.cos(t), mid * math.sin(t)).slot2D(length + a["cbore_w"], a["cbore_w"], ang) \
            .extrude(a["cbore_depth"])
        body = body.cut(slot).cut(cb)
    return body


# ============================================================================== main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board-step", default=os.path.join(ROOT, "build", "knuckle", "fab", "knuckle.step"))
    ap.add_argument("--pcb", default=os.path.join(ROOT, "build", "knuckle", "knuckle.kicad_pcb"))
    ap.add_argument("--sts-step", default=STS_DEFAULT)
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "knuckle", "cad"))
    args = ap.parse_args()
    if not os.path.exists(args.sts_step):
        raise SystemExit("refused: no STS3215 STEP at %s; pass --sts-step (SO-ARM100 STEP/SO100/STS3215_03a.step)"
                         % args.sts_step)
    os.makedirs(args.out, exist_ok=True)

    # ---- the board, measured
    named = read_named_step(args.board_step)
    body = named["BOARD"]
    bb = body.BoundingBox()
    cx, cy = (bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2
    comps = {k: v for k, v in named.items() if not k.startswith("BOARD")}
    top_parts = {k: v for k, v in comps.items() if v.BoundingBox().zmin > bb.zmax - 0.2}
    bot_parts = {k: v for k, v in comps.items() if v.BoundingBox().zmax < bb.zmin + 0.2}
    seat_top = min(v.BoundingBox().zmin for v in top_parts.values())
    seat_bot = max(v.BoundingBox().zmax for v in bot_parts.values())
    enc = comps["U3"].BoundingBox()
    enc_below = seat_bot - enc.zmin                    # how far the MT6701 body stands proud of the bottom face
    enc_xy = ((enc.xmin + enc.xmax) / 2 - cx, (enc.ymin + enc.ymax) / 2 - cy)

    # ---- the stack, from the measured encoder and the stated magnet
    holder, holder_z0, magnet_face = magnet_holder()
    standoff_h = magnet_face + AIR_GAP_TARGET_MM + enc_below
    dz = standoff_h - seat_bot                         # STEP z -> assembly z
    move = lambda s: s.translate(cq.Vector(-cx, -cy, dz))
    board_body = move(body)
    top_parts = {k: move(v) for k, v in top_parts.items()}
    bot_parts = {k: move(v) for k, v in bot_parts.items()}
    z_board_bot, z_board_top = standoff_h, seat_top + dz

    # ---- bus sockets: no STEP body in KiCad 10, so the F.Fab outline and the catalogue height stand in
    headers, openings = {}, {}
    for ref, fpid, x, y, rot, side, fab in footprints(args.pcb):
        if "SPOX_5267" not in fpid:
            continue
        pts = []
        for px, py in ((fab[0], fab[1]), (fab[2], fab[3]), (fab[0], fab[3]), (fab[2], fab[1])):
            a = math.radians(rot)
            # KiCad: positive rotation turns counter-clockwise on screen (y down)
            qx = px * math.cos(a) + py * math.sin(a)
            qy = -px * math.sin(a) + py * math.cos(a)
            pts.append((x + qx - (bb.xmin + bb.xmax) / 2, -(y + qy) - cy))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = (min(xs), min(ys), max(xs), max(ys))
        headers[ref] = cq.Workplane("XY").workplane(offset=z_board_top).center((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) \
            .rect(box[2] - box[0], box[3] - box[1]).extrude(HEADER_HEIGHT_MM).val()
        oc = COVER["opening_clear"]
        openings[ref] = (box[0] - oc, box[1] - oc, box[2] + oc, box[3] + oc)

    if sorted(headers) != ["J1", "J2"] or any(k not in comps for k in ("U3",)):
        raise SystemExit("refused: %s does not hold J1 and J2 (5267-03A) and %s does not hold U3; run the pipeline's "
                         "fab stage first and do not read a board mid-run" % (args.pcb, args.board_step))
    z_roof_under = z_board_top + HEADER_HEIGHT_MM + COVER["header_clear"]
    cov, z_cover_top = cover(z_board_top, z_roof_under, list(openings.values()))
    sto = standoff(standoff_h)
    horn = sts_horn(args.sts_step)
    adapter = horn_adapter(horn)
    z_rotor_top = -MOTOR["height_mm"]
    adapter_placed = adapter.mirror("XY").translate((0, 0, z_rotor_top))   # joint face points away from the motor
    mag = magnet(magnet_face)

    # motor proxy (est.): rotor can and the shaft stub
    motor = cq.Workplane("XY").workplane(offset=z_rotor_top).circle(MOTOR["rotor_od_mm"] / 2).extrude(MOTOR["height_mm"] - 0.3) \
        .union(cq.Workplane("XY").circle(MOTOR["shaft_od_mm"] / 2).extrude(MOTOR["shaft_stub_mm"]))

    # ---- fit, measured on the solids
    enc_s = bot_parts["U3"]
    others_bot = {k: v for k, v in bot_parts.items() if k != "U3"}
    gap = dist(mag.val(), enc_s)
    cover_s = cov.val()
    top_d = {k: dist(v, cover_s) for k, v in top_parts.items()}
    hdr_d = {k: dist(v, cover_s) for k, v in headers.items()}
    worst_top = min(top_d, key=top_d.get)
    sto_s = sto.val()
    sto_bot = {k: dist(v, sto_s) for k, v in bot_parts.items()}
    worst_sto = min(sto_bot, key=sto_bot.get)
    holder_s = holder.val()
    holder_to_bottom = {k: dist(v, holder_s) for k, v in bot_parts.items()}
    worst_holder = min(holder_to_bottom, key=holder_to_bottom.get)
    open_margin = {}
    for ref, h in sorted(headers.items()):
        op = openings[ref]
        hb = h.BoundingBox()
        open_margin[ref] = {"opening_mm": [round(op[2] - op[0], 2), round(op[3] - op[1], 2)],
                            "body_mm": [round(hb.xlen, 2), round(hb.ylen, 2)],
                            "margin_min_mm": round(min(hb.xmin - op[0], op[2] - hb.xmax, hb.ymin - op[1], op[3] - hb.ymax), 3),
                            "body_top_below_roof_mm": round(z_roof_under - hb.zmax, 3)}
    screw = SCREW[MOTOR["screw"]]
    grip = (z_cover_top - screw["head_h"]) - 0.0          # head seat to the base face
    screw_len = grip + 3.0                                 # 3 mm into the motor base
    std = [l for l in (16, 18, 20, 22, 25) if l >= screw_len]
    # the adapter's insert holes, read back off the solid (cylindrical faces of the insert diameter)
    made = []
    for f in adapter.val().Faces():
        ad = BRepAdaptor_Surface(f.wrapped)
        if ad.GetType() == GeomAbs_Cylinder and abs(2 * ad.Cylinder().Radius() - ADAPTER["insert_d"]) < 1e-3:
            loc = ad.Cylinder().Axis().Location()
            if not any(math.hypot(loc.X() - a, loc.Y() - b) < 1e-3 for a, b in made):
                made.append((loc.X(), loc.Y()))
    if len(made) != len(horn["holes"]):
        raise SystemExit("refused: the adapter has %d insert holes, the STS3215 horn %d" % (len(made), len(horn["holes"])))
    pattern_dev = max(min(math.hypot(x - hx, y - hy) for hx, hy in horn["holes"]) for x, y in made)
    fit = {
        "frame": "z = 0 at the motor base face, +z toward the board; X/Y the board's axes, +Y toward J1",
        "board": {"thickness_step_mm": round(z_board_top - z_board_bot, 3), "bottom_face_z": round(z_board_bot, 3),
                  "top_face_z": round(z_board_top, 3), "outline_mm": [round(bb.xlen, 2), round(bb.ylen, 2)]},
        "encoder": {"body_below_bottom_face_mm": round(enc_below, 3), "face_z": round(z_board_bot - enc_below, 3),
                    "offset_from_axis_mm": round(math.hypot(*enc_xy), 3), "offset_limit_mm": 0.3},
        "magnet_holder": {"z": [round(holder_z0, 3), round(magnet_face, 3)], "od_mm": HOLDER["od"],
                          "running_gap_above_base_mm": round(holder_z0, 3),
                          "closest_bottom_part": worst_holder, "clearance_to_it_mm": round(holder_to_bottom[worst_holder], 3)},
        "air_gap": {"target_mm": AIR_GAP_TARGET_MM, "measured_magnet_to_ic_mm": round(gap, 3), "window_mm": [0.5, 2.0]},
        "standoff": {"height_mm": round(standoff_h, 3), "closest_bottom_part": worst_sto,
                     "clearance_to_it_mm": round(sto_bot[worst_sto], 3),
                     "ring_to_holder_radial_mm": round((STANDOFF["ring_id"] - HOLDER["od"]) / 2, 3),
                     "screw_points_mm": [[round(x, 2), round(y, 2)] for x, y in screw_points()]},
        "cover": {"od_mm": round(2 * (35.0 / 2 + COVER["radial_clear"] + COVER["wall"]), 2),
                  "roof_underside_z": round(z_roof_under, 3), "top_z": round(z_cover_top, 3),
                  "closest_top_part": worst_top, "clearance_to_it_mm": round(top_d[worst_top], 3),
                  "header_clearance_mm": {k: round(v, 3) for k, v in hdr_d.items()},
                  "openings": open_margin},
        "screws": {"size": MOTOR["screw"], "grip_mm": round(grip, 2), "length_needed_mm": round(screw_len, 2),
                   "standard_length_mm": std[0] if std else None},
        "horn_adapter": {"sts_horn_od_mm": horn["od"], "sts_horn_thickness_mm": horn["thickness"],
                         "sts_hole_d_mm": horn["hole_d"], "sts_bolt_circle_mm": horn["bolt_circle_d"],
                         "sts_holes_mm": horn["holes"], "adapter_od_mm": ADAPTER["od"], "adapter_thick_mm": ADAPTER["thick"],
                         "adapter_insert_holes_mm": [[round(x, 3), round(y, 3)] for x, y in made],
                         "pattern_deviation_mm": round(pattern_dev, 4),
                         "rotor_slot_bolt_circle_mm": [2 * ADAPTER["rotor_slot"][0], 2 * ADAPTER["rotor_slot"][1]],
                         "z": [round(z_rotor_top - ADAPTER["thick"], 3), round(z_rotor_top, 3)]},
        "stack": {"horn_face_z": round(z_rotor_top - ADAPTER["thick"], 3), "rotor_top_z_est": z_rotor_top,
                  "base_face_z": 0.0, "magnet_face_z": round(magnet_face, 3), "board_bottom_z": round(z_board_bot, 3),
                  "board_top_z": round(z_board_top, 3), "tallest_top_part": max(top_parts, key=lambda k: top_parts[k].BoundingBox().zmax),
                  "tallest_top_part_z": round(max(v.BoundingBox().zmax for v in top_parts.values()), 3),
                  "header_top_z": round(z_board_top + HEADER_HEIGHT_MM, 3), "cover_top_z": round(z_cover_top, 3),
                  "overall_mm": round(z_cover_top - (z_rotor_top - ADAPTER["thick"]), 3)},
        "estimated_inputs": {"motor": MOTOR, "magnet": MAGNET, "header_height_mm": HEADER_HEIGHT_MM},
    }
    # ---- exports
    parts = {"standoff": sto, "magnet_holder": holder, "cover": cov, "horn_adapter": adapter}
    for name, wp in parts.items():
        cq.exporters.export(wp, os.path.join(args.out, name + ".step"))
        cq.exporters.export(wp, os.path.join(args.out, name + ".stl"), tolerance=0.01, angularTolerance=0.1)
    # the STLs a slicer will get, read back: a mesh that is not watertight does not slice reliably
    try:
        import trimesh
        fit["stl"] = {}
        for name in parts:
            m = trimesh.load(os.path.join(args.out, name + ".stl"))
            fit["stl"][name] = {"watertight": bool(m.is_watertight), "volume_mm3": round(float(m.volume), 1),
                                "extents_mm": [round(float(x), 2) for x in m.extents]}
    except ImportError:
        fit["stl"] = "not checked: trimesh is not installed"
    tmp = os.path.join(args.out, "fit.json.tmp")
    json.dump(fit, open(tmp, "w"), indent=2)
    os.replace(tmp, os.path.join(args.out, "fit.json"))
    asm = cq.Assembly(name="knuckle")
    asm.add(motor, name="motor_proxy_est", color=cq.Color(0.25, 0.25, 0.28))
    asm.add(holder, name="magnet_holder", color=cq.Color(0.9, 0.55, 0.2))
    asm.add(mag, name="magnet", color=cq.Color(0.6, 0.6, 0.65))
    asm.add(sto, name="standoff", color=cq.Color(0.2, 0.5, 0.85))
    asm.add(cq.Workplane("XY").add(board_body), name="pcb", color=cq.Color(0.1, 0.35, 0.15))
    for k, v in list(top_parts.items()) + list(bot_parts.items()):
        asm.add(cq.Workplane("XY").add(v), name=k, color=cq.Color(0.15, 0.15, 0.15))
    for k, v in headers.items():
        asm.add(cq.Workplane("XY").add(v), name=k + "_envelope", color=cq.Color(0.95, 0.95, 0.9))
    asm.add(cov, name="cover", color=cq.Color(0.85, 0.85, 0.88, 0.5))
    asm.add(adapter_placed, name="horn_adapter", color=cq.Color(0.85, 0.3, 0.3))
    asm.save(os.path.join(args.out, "assembly.step"))

    # ---- previews: an exploded view and a section through the axis (SVG, then rsvg-convert)
    spread = {"horn_adapter": -22, "motor": -10, "holder": 5, "standoff": 15, "board": 29, "cover": 47}
    board_items = [board_body] + list(top_parts.values()) + list(bot_parts.values()) + list(headers.values())
    pieces = [adapter_placed.val().translate(cq.Vector(0, 0, spread["horn_adapter"])),
              motor.val().translate(cq.Vector(0, 0, spread["motor"])),
              holder.val().translate(cq.Vector(0, 0, spread["holder"])), mag.val().translate(cq.Vector(0, 0, spread["holder"])),
              sto.val().translate(cq.Vector(0, 0, spread["standoff"]))] + \
        [b.translate(cq.Vector(0, 0, spread["board"])) for b in board_items] + \
        [cov.val().translate(cq.Vector(0, 0, spread["cover"]))]
    near = lambda rad, z: (rad * 0.86, -rad * 0.5, z)
    labels = [(near(18.4, z_cover_top * 0.6 + spread["cover"]), "cover (printed): 4 x M3 inserts, socket openings"),
              (near(17.5, z_board_top + spread["board"]), "Knuckle PCB, 35 mm (MT6701QT on the underside)"),
              (near(9.5, standoff_h + spread["standoff"]), "standoff (printed): sets the %.2f mm air gap" % gap),
              (near(4.5, magnet_face + spread["holder"]), "magnet holder (printed) + 6 x 2.5 mm magnet"),
              (near(17.5, -6 + spread["motor"]), "GBM2804H-100T (proxy, est.)"),
              (near(11.5, z_rotor_top - 2 + spread["horn_adapter"]), "horn adapter (printed): STS3215 pattern")]
    exploded = os.path.join(args.out, "exploded.svg")
    render_svg(pieces, exploded, view=(1.0, -1.7, 0.75), size=(1300, 1500), labels=labels)
    whole = [adapter_placed.val(), motor.val(), holder.val(), mag.val(), sto.val()] + board_items + [cov.val()]
    half = cq.Workplane("XY").box(60, 60, 100).translate((-30, 0, 0)).val()   # keep x <= 0: cut on the YZ plane
    cut = [w.intersect(half) for w in whole]
    plane = cq.Face.makePlane(200, 200, basePnt=(0, 0, 0), dir=(1, 0, 0))
    colours = ["#e9a3a3", "#8d8f99", "#f0b46a", "#c7ccd6", "#8fb4e8", "#3f7f4f"] + ["#2b2b2b"] * (len(board_items) - 1) + ["#d9dbe2"]
    fills = []
    for w, col in zip(whole, colours):
        try:
            sec = w.intersect(plane)
            if sec.Faces():
                fills.append((sec, col))
        except Exception:
            pass
    section = os.path.join(args.out, "section.svg")
    render_svg([c for c in cut if c.isValid()], section, view=(1.0, 0.0, 0.0), size=(1500, 1100), stroke=1.0, fills=fills,
               labels=[((0, 0, (magnet_face + z_board_bot - enc_below) / 2), "air gap %.2f mm (magnet to MT6701)" % gap),
                       ((0, 0, z_cover_top), "cover top z = %.1f" % z_cover_top),
                       ((0, 0, z_rotor_top - ADAPTER["thick"]), "horn face z = %.1f" % (z_rotor_top - ADAPTER["thick"]))])
    for svg in (exploded, section):
        png = svg[:-4] + ".png"
        try:
            subprocess.run(["rsvg-convert", "-b", "white", "-w", "1400", svg, "-o", png], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            print("rsvg-convert failed for %s: %s" % (svg, e), file=sys.stderr)
    print(json.dumps({k: fit[k] for k in ("air_gap", "stack")}, indent=1))


if __name__ == "__main__":
    main()
