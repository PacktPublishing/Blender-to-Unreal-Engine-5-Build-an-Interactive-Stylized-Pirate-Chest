"""
bake_ao_cavity.py  -  One-button AO + Cavity baker for a multi-object / multi-material
                      asset that shares ONE UV atlas (all islands in 0-1, non-overlapping).

WHAT IT DOES
    1. Bakes an Ambient Occlusion map (cross-object occlusion, so hinges/locks darken
       the panels they touch).
    2. Bakes a Cavity / Curvature map (convex edges bright, concave crevices dark) -
       the mask you multiply against AO to drive rust in the recesses.
    Both bake to a single shared 2K texture, straight from the shared UVs.

HOW TO USE (in Blender)
    - Select ALL the chest objects you want in the bake (Cycles will still use every
      visible object as an occluder, but only SELECTED objects get written to the map).
    - Make the last-clicked object the active one.
    - Open this file in the Text Editor and press "Run Script" (or run from console).
    - Two PNGs land next to the .blend in a "Bakes" folder: Chest_AO.png, Chest_Cavity.png.

WHY A SCRIPT
    With many objects x many materials, every material slot needs a target image node
    added + made active before you can bake, and the whole thing needs its shaders
    temporarily rewired to an emission pass and then restored. That is dozens of clicks
    to do by hand and one missed slot ruins the bake. This does it safely and restores
    every material afterwards.

SAFE: it snapshots each material's Surface connection, rewires for the bake, then puts
      everything back exactly as it was. Nothing about your look-dev survives the run.
"""

import bpy
import os

# ---------------------------------------------------------------- CONFIG ----
RESOLUTION      = 2048          # 2K maps
SAMPLES         = 16            # render samples per bake (emit is cheap; 16 is smooth)
MARGIN          = 8            # px bleed past island edges

# Ambient Occlusion
AO_DISTANCE     = 0.2          # ray length in SCENE UNITS - tune to your chest size.
                              #   bigger = softer/broader occlusion, smaller = tight contact only
AO_NODE_SAMPLES = 16           # AO node internal samples (quality of the occlusion)

# Cavity / Curvature (from mesh Pointiness). Needs reasonably dense, beveled geometry.
CAVITY_BLACK    = 0.44         # pointiness value mapped to black (concave / crevice)
CAVITY_WHITE    = 0.56         # pointiness value mapped to white (convex / edge)
                              #   narrow the gap for punchier edges, widen for subtler

OUT_DIR_NAME    = "Bakes"      # created next to the .blend
AO_NAME         = "Chest_AO"
CAVITY_NAME     = "Chest_Cavity"
# ----------------------------------------------------------------------------


def log(msg):
    print(f"[bake] {msg}")


def get_target_objects():
    objs = [o for o in bpy.context.selected_objects if o.type == 'MESH' and o.data.materials]
    if not objs:
        raise RuntimeError("Select the mesh object(s) to bake (each needs at least one material).")
    return objs


def unique_materials(objs):
    seen, mats = set(), []
    for o in objs:
        for slot in o.material_slots:
            m = slot.material
            if m and m.use_nodes and m.name not in seen:
                seen.add(m.name)
                mats.append(m)
    return mats


def make_target_image(name):
    img = bpy.data.images.get(name)
    if img and (img.size[0] != RESOLUTION or img.size[1] != RESOLUTION):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, RESOLUTION, RESOLUTION, alpha=False, float_buffer=False)
    img.colorspace_settings.name = 'Non-Color'
    return img


def surface_source(nt):
    """Return (from_node, from_socket) currently feeding Material Output.Surface, or (None, None)."""
    out = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    if out is None:
        out = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if out and out.inputs['Surface'].is_linked:
        l = out.inputs['Surface'].links[0]
        return out, l.from_node, l.from_socket
    return out, None, None


def prep_material(mat, image, mode):
    """Rewire mat's Surface to an emission bake network. Returns a restore-token."""
    nt = mat.node_tree
    out, src_node, src_sock = surface_source(nt)
    if out is None:
        out = nt.nodes.new('ShaderNodeOutputMaterial')

    temp = []
    emi = nt.nodes.new('ShaderNodeEmission'); temp.append(emi)
    emi.inputs['Strength'].default_value = 1.0
    emi.location = (out.location.x - 200, out.location.y - 300)

    if mode == 'AO':
        ao = nt.nodes.new('ShaderNodeAmbientOcclusion'); temp.append(ao)
        ao.samples = AO_NODE_SAMPLES
        ao.inside = False
        ao.only_local = False                       # False = let other objects occlude
        ao.inputs['Distance'].default_value = AO_DISTANCE
        ao.location = (emi.location.x - 250, emi.location.y)
        nt.links.new(ao.outputs['AO'], emi.inputs['Color'])
    else:  # CAVITY
        geo = nt.nodes.new('ShaderNodeNewGeometry'); temp.append(geo)
        geo.location = (emi.location.x - 500, emi.location.y)
        ramp = nt.nodes.new('ShaderNodeValToRGB'); temp.append(ramp)
        ramp.location = (emi.location.x - 250, emi.location.y)
        ramp.color_ramp.interpolation = 'LINEAR'
        ramp.color_ramp.elements[0].position = CAVITY_BLACK
        ramp.color_ramp.elements[0].color = (0, 0, 0, 1)
        ramp.color_ramp.elements[1].position = CAVITY_WHITE
        ramp.color_ramp.elements[1].color = (1, 1, 1, 1)
        nt.links.new(geo.outputs['Pointiness'], ramp.inputs['Fac'])
        nt.links.new(ramp.outputs['Color'], emi.inputs['Color'])

    nt.links.new(emi.outputs['Emission'], out.inputs['Surface'])

    # target image node (bake writes to the ACTIVE image node of each material)
    img_node = nt.nodes.new('ShaderNodeTexImage'); temp.append(img_node)
    img_node.image = image
    img_node.location = (out.location.x + 250, out.location.y)
    img_node.select = True
    nt.nodes.active = img_node

    return {'mat': mat, 'out': out, 'src_node': src_node, 'src_sock': src_sock, 'temp': temp}


def restore_material(token):
    nt = token['mat'].node_tree
    for n in token['temp']:
        try: nt.nodes.remove(n)
        except Exception: pass
    if token['src_node'] is not None and token['src_sock'] is not None:
        nt.links.new(token['src_sock'], token['out'].inputs['Surface'])


def bake_pass(objs, mats, image, mode):
    log(f"Preparing {len(mats)} material(s) for {mode} ...")
    tokens = [prep_material(m, image, mode) for m in mats]
    try:
        # active object + selection for the bake op
        bpy.ops.object.select_all(action='DESELECT')
        for o in objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = objs[0]
        log(f"Baking {mode} -> {image.name} ({RESOLUTION}px) ...")
        bpy.ops.object.bake(type='EMIT', use_clear=True,
                            margin=MARGIN, use_selected_to_active=False)
    finally:
        for t in tokens:
            restore_material(t)
    log(f"{mode} done.")


def save_image(image, out_dir):
    path = os.path.join(out_dir, image.name + ".png")
    image.filepath_raw = path
    image.file_format = 'PNG'
    image.save()
    log(f"Saved {path}")
    return path


def main():
    if not bpy.data.filepath:
        raise RuntimeError("Save the .blend first (maps are written next to it).")

    scene = bpy.context.scene
    objs = get_target_objects()
    mats = unique_materials(objs)
    log(f"{len(objs)} object(s), {len(mats)} unique material(s).")

    # --- render settings (Cycles is required for baking) ---
    prev_engine  = scene.render.engine
    prev_samples = getattr(scene.cycles, 'samples', None) if hasattr(scene, 'cycles') else None
    scene.render.engine = 'CYCLES'
    try:
        scene.cycles.device = 'GPU'
    except Exception:
        pass
    scene.cycles.samples = SAMPLES
    scene.render.bake.margin_type = 'ADJACENT_FACES'
    scene.render.bake.use_selected_to_active = False

    ao_img     = make_target_image(AO_NAME)
    cavity_img = make_target_image(CAVITY_NAME)

    out_dir = os.path.join(os.path.dirname(bpy.data.filepath), OUT_DIR_NAME)
    os.makedirs(out_dir, exist_ok=True)

    try:
        bake_pass(objs, mats, ao_img, 'AO')
        bake_pass(objs, mats, cavity_img, 'CAVITY')
        save_image(ao_img, out_dir)
        save_image(cavity_img, out_dir)
    finally:
        scene.render.engine = prev_engine
        if prev_samples is not None:
            scene.cycles.samples = prev_samples
        # re-select what the user had
        bpy.ops.object.select_all(action='DESELECT')
        for o in objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = objs[0]

    log("ALL DONE. Multiply AO x Cavity to mask rust in the recesses.")


main()
