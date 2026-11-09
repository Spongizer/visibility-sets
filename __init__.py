bl_info = {
    "name": "Visibility Sets",
    "author": "Sponge",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Visibility, Object menu > Visibility Sets",
    "description": "Bulk-select objects that are already disabled and keyframe their "
                   "visibility in one click, using named selection sets saved with the file",
    "category": "Animation",
}

import re
from fnmatch import fnmatchcase

import bpy
from bpy.app.handlers import persistent


# =====================================================================
# Blender drops the selection on disabled (hide_viewport) objects, so once
# you disable a batch you can't grab it back to key it on again. We get
# around it with Outliner reads, saved sets, and a no-keyframe "unblock".
# ========================================


def _st(context):
    return getattr(context.scene, "visibility_sets", None)


# ----------------------------------------------------------------------
# Selection sets
# ----------------------------------------------------------------------

def _active_slot(context):
    st = _st(context)
    if st is None or not st.slots:
        return None
    idx = max(0, min(st.active_slot, len(st.slots) - 1))
    return st.slots[idx]


def _reachable(context, refs):
    """Objects from `refs` that are still in this view layer.
    Operators only -- too slow for draw(), which just uses len(refs)."""
    vl_objs = context.view_layer.objects
    out = []
    for ref in refs:
        o = ref.obj
        if o is not None and vl_objs.get(o.name) == o:
            out.append(o)
    return out


def _slot_objects(context, slot):
    return _reachable(context, slot.objects) if slot is not None else []


_SUFFIX_RE = re.compile(r"^(.*?)(?: \((\d+)\))?$")


def _unique_name(st, name, skip=None):
    """Return `name`, or `name (n)` if another slot already uses it."""
    skip_ptr = skip.as_pointer() if skip is not None else None
    taken = {sl.name for sl in st.slots if sl.as_pointer() != skip_ptr}
    if name not in taken:
        return name
    base = _SUFFIX_RE.match(name).group(1) or name
    i = 1
    while f"{base} ({i})" in taken:
        i += 1
    return f"{base} ({i})"


def _slot_name_update(self, context):
    # Runs on rename. Writing self["name"] directly won't re-trigger this.
    st = getattr(self.id_data, "visibility_sets", None)
    if st is None:
        return
    fixed = _unique_name(st, self.name, skip=self)
    if fixed != self.name:
        self["name"] = fixed


def _alive_objects():
    """Every object linked to any scene. Deleted objects linger (our
    pointers keep them alive), and this beats users_scene by ~1000x."""
    alive = set()
    for sc in bpy.data.scenes:
        alive.update(sc.objects)
    return alive


def _prune_refs(refs, alive):
    """Drop refs to deleted objects. Iterate, don't index -- refs[i]
    walks from the start every time, so an indexed loop goes O(n^2)."""
    dead = [i for i, r in enumerate(refs) if r.obj is None or r.obj not in alive]
    for i in reversed(dead):
        refs.remove(i)
    return len(dead)


def _prune_slot(slot, alive):
    removed = _prune_refs(slot.objects, alive)
    if removed and slot.active_obj >= len(slot.objects):
        slot.active_obj = max(0, len(slot.objects) - 1)
    return removed


def _prune_settings(st, alive):
    for slot in st.slots:
        _prune_slot(slot, alive)
    _prune_refs(st.last_keyed, alive)


# Fires on every depsgraph update (even mid-drag), so bail fast: only prune
# when a collection changed AND the object counts moved. Missing one is no
# big deal -- operators filter dead objects through _reachable() anyway.
_scene_sig = {}


def _scene_signature(scene):
    return (len(scene.objects), len(bpy.data.objects))


@persistent
def _vissets_prune_handler(scene, depsgraph):
    st = getattr(scene, "visibility_sets", None)
    if st is None or not (st.slots or st.last_keyed):
        return
    if not depsgraph.id_type_updated('COLLECTION'):
        return
    key = scene.as_pointer()
    sig = _scene_signature(scene)
    if _scene_sig.get(key) == sig:
        return
    _scene_sig[key] = sig
    _prune_settings(st, _alive_objects())


@persistent
def _vissets_load_post(_dummy):
    _scene_sig.clear()
    alive = None
    for scene in bpy.data.scenes:
        st = getattr(scene, "visibility_sets", None)
        if st is None or not (st.slots or st.last_keyed):
            continue
        if alive is None:
            alive = _alive_objects()
        _prune_settings(st, alive)


# ----------------------------------------------------------------------
# Selection readers
# ----------------------------------------------------------------------

def _viewport_selection(context):
    """Objects Blender's select flags currently report."""
    objs = set(context.view_layer.objects.selected)
    for did in (getattr(context, "selected_ids", None) or []):
        if isinstance(did, bpy.types.Object):
            objs.add(did)
    return objs


def _outliner_selection(context):
    """Objects highlighted in any open Outliner. Needs a WINDOW-region
    override, and has to read bpy.context inside it or it sees nothing."""
    objs = set()
    wm = context.window_manager
    if wm is None:
        return objs

    for win in wm.windows:
        scr = win.screen
        if scr is None:
            continue
        for area in scr.areas:
            if area.type != 'OUTLINER':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            try:
                with context.temp_override(window=win, area=area, region=region):
                    for did in (getattr(bpy.context, "selected_ids", None) or []):
                        if isinstance(did, bpy.types.Object):
                            objs.add(did)
            except Exception as e:
                print("[VisibilitySets] outliner override failed:", e)
    return objs


def _live_selection(context):
    return _viewport_selection(context) | _outliner_selection(context)


def _get_targets(context):
    """Returns (objects, 'LIVE' | 'NONE'). Only a real selection counts --
    no active-object fallback, so we never key something you didn't pick."""
    live = _live_selection(context)
    if live:
        return sorted(live, key=lambda o: o.name), 'LIVE'
    return [], 'NONE'


# ----------------------------------------------------------------------
# Selection / visibility helpers
# ----------------------------------------------------------------------

def _sync(context):
    """Force the lazy view-layer resync so base flags are current."""
    try:
        context.view_layer.update()
    except Exception:
        pass


def _try_select(context, objs):
    """Set select flags and report how many actually took."""
    vl = context.view_layer
    _sync(context)
    ok = 0
    for obj in objs:
        try:
            obj.select_set(True, view_layer=vl)
            if obj.select_get(view_layer=vl):
                ok += 1
        except (RuntimeError, ReferenceError):
            pass
    return ok


def _unblock(context, objs):
    """Clear the hide flags that block selection -- no keyframe, so the
    animation flips it back on the next frame change. All hide_set calls go
    first, hide_viewport writes last (interleaving forces a resync each time)."""
    vl = context.view_layer
    _sync(context)
    touched = set()
    for obj in objs:
        try:
            if obj.hide_get(view_layer=vl):
                obj.hide_set(False, view_layer=vl)
                touched.add(obj.name)
        except (RuntimeError, ReferenceError):
            pass
    for obj in objs:
        try:
            if obj.hide_viewport:
                obj.hide_viewport = False
                touched.add(obj.name)
        except (RuntimeError, ReferenceError):
            pass
    _sync(context)
    return sorted(touched)


def _reselect(context, objs, unblock=True):
    """Replace the selection with `objs`, unblocking disabled ones.

    Shared by Reselect Sets and Reselect Last Keyed. Returns
    (selected_count, unblocked_names).
    """
    vl = context.view_layer
    for o in list(vl.objects.selected):
        try:
            o.select_set(False, view_layer=vl)
        except (RuntimeError, ReferenceError):
            pass

    ok = _try_select(context, objs)
    touched = []
    if ok < len(objs) and unblock:
        stuck = [o for o in objs if not o.select_get(view_layer=vl)]
        touched = _unblock(context, stuck)
        ok = _try_select(context, objs)

    # Point the active object at the result so the header reflects it.
    try:
        vl.objects.active = objs[0]
    except Exception:
        pass
    return ok, touched


def _report_reselect(op, label, ok, total, touched):
    missed = total - ok
    if missed:
        op.report({'WARNING'},
                  f"{label}: {ok}/{total} selected - {missed} unreachable "
                  f"(collection hidden or excluded)")
    elif touched:
        op.report({'INFO'},
                  f"{label}: {ok} selected - {len(touched)} unblocked, press "
                  f"Enable to keyframe or Ctrl+Z to revert")
    else:
        op.report({'INFO'}, f"{label}: {ok} object(s) selected")


# ----------------------------------------------------------------------
# Core apply
# ----------------------------------------------------------------------

def _apply(context, mode, show):
    """mode: 'RENDER' | 'DISABLE'.  show=True means make visible."""
    targets, source = _get_targets(context)
    if not targets:
        return 0, source

    st = _st(context)
    frame = context.scene.frame_current
    hide = not show

    # CONSTANT interpolation so boolean channels snap instead of ramping.
    prefs = context.preferences.edit
    old_interp = prefs.keyframe_new_interpolation_type
    prefs.keyframe_new_interpolation_type = 'CONSTANT'

    count = 0
    keyed = []
    try:
        for obj in targets:
            try:
                if mode == 'RENDER':
                    obj.hide_render = hide
                    obj.keyframe_insert(data_path="hide_render", frame=frame)
                else:
                    obj.hide_viewport = hide
                    obj.keyframe_insert(data_path="hide_viewport", frame=frame)
                    if st is not None and st.link_render:
                        obj.hide_render = hide
                        obj.keyframe_insert(data_path="hide_render", frame=frame)
                count += 1
                keyed.append(obj)
            except (RuntimeError, ReferenceError) as e:
                print("[VisibilitySets] skipped", getattr(obj, "name", "?"), e)
    finally:
        prefs.keyframe_new_interpolation_type = old_interp

    # Remember what got keyed for Reselect Last Keyed (only on success).
    if keyed and st is not None:
        st.last_keyed.clear()
        for obj in keyed:
            st.last_keyed.add().obj = obj

    _try_select(context, targets)
    return count, source


# ======================================================================
# Properties
# ======================================================================

class VISSETS_ObjectRef(bpy.types.PropertyGroup):
    # An ID pointer, not a name: survives renames, self-nulls on delete.
    obj: bpy.props.PointerProperty(type=bpy.types.Object)


class VISSETS_Slot(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name", default="Set", update=_slot_name_update)
    objects: bpy.props.CollectionProperty(type=VISSETS_ObjectRef)
    expanded: bpy.props.BoolProperty(
        name="Show Objects", default=False,
        description="List the objects in this set",
    )
    active_obj: bpy.props.IntProperty(default=0, options={'HIDDEN'})


class VISSETS_Settings(bpy.types.PropertyGroup):
    link_render: bpy.props.BoolProperty(
        name="Also key Render",
        default=False,
        description="Keyframe hide_render together with hide_viewport on Disable",
    )
    slots: bpy.props.CollectionProperty(type=VISSETS_Slot)
    active_slot: bpy.props.IntProperty(name="Active Set", default=0)
    # Objects touched by the most recent keyframe operation. Not a set:
    # no name, no UI list, overwritten by every successful key.
    last_keyed: bpy.props.CollectionProperty(type=VISSETS_ObjectRef)


# ======================================================================
# Operators
# ======================================================================

class _KeyframeOpBase:
    """Shared body of the four keyframe operators. Split into four (not one
    with options) so each gets its own readable name in the keymap editor."""
    bl_options = {'REGISTER', 'UNDO'}
    _mode = 'RENDER'   # 'RENDER' | 'DISABLE'
    _show = True

    def execute(self, context):
        count, source = _apply(context, self._mode, self._show)
        if count == 0:
            self.report({'WARNING'},
                        "Nothing selected - select objects, or use Reselect Sets / "
                        "Reselect Last Keyed")
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"{self.bl_label}: {count} object(s) @ frame {context.scene.frame_current}")
        return {'FINISHED'}


class VISSETS_OT_render_show(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_render_show"
    bl_label = "Render: Show"
    bl_description = "Keyframe hide_render = False on every selected object at the current frame"
    _mode, _show = 'RENDER', True


class VISSETS_OT_render_hide(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_render_hide"
    bl_label = "Render: Hide"
    bl_description = "Keyframe hide_render = True on every selected object at the current frame"
    _mode, _show = 'RENDER', False


class VISSETS_OT_viewport_enable(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_viewport_enable"
    bl_label = "Viewport: Enable"
    bl_description = ("Keyframe hide_viewport = False on every selected object at the current "
                      "frame (also hide_render if 'Also key Render' is on)")
    _mode, _show = 'DISABLE', True


class VISSETS_OT_viewport_disable(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_viewport_disable"
    bl_label = "Viewport: Disable"
    bl_description = ("Keyframe hide_viewport = True on every selected object at the current "
                      "frame (also hide_render if 'Also key Render' is on)")
    _mode, _show = 'DISABLE', False


class VISSETS_OT_slot_add(bpy.types.Operator):
    bl_idname = "object.visibility_sets_slot_add"
    bl_label = "Add Selection Set"
    bl_description = "Add an empty selection set and make it active"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = _st(context)
        slot = st.slots.add()
        slot.name = _unique_name(st, "Set", skip=slot)
        st.active_slot = len(st.slots) - 1
        self.report({'INFO'}, f"Added '{slot.name}'")
        return {'FINISHED'}


class VISSETS_OT_slot_remove(bpy.types.Operator):
    bl_idname = "object.visibility_sets_slot_remove"
    bl_label = "Remove Selection Set"
    bl_description = "Delete the active selection set"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = _st(context)
        if not st.slots:
            return {'CANCELLED'}
        idx = max(0, min(st.active_slot, len(st.slots) - 1))
        name = st.slots[idx].name
        st.slots.remove(idx)
        st.active_slot = min(idx, max(0, len(st.slots) - 1))
        self.report({'INFO'}, f"Removed '{name}'")
        return {'FINISHED'}


class VISSETS_OT_store(bpy.types.Operator):
    bl_idname = "object.visibility_sets_store"
    bl_label = "Store Selection"
    bl_description = ("Save the current selection into the active set, replacing what "
                      "was there. Stores only -- nothing else changes")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = _st(context)
        live = _live_selection(context)
        if not live:
            self.report({'WARNING'}, "Nothing selected")
            return {'CANCELLED'}

        slot = _active_slot(context)
        if slot is None:
            slot = st.slots.add()
            slot.name = _unique_name(st, "Set", skip=slot)
            st.active_slot = len(st.slots) - 1

        slot.objects.clear()
        for o in sorted(live, key=lambda x: x.name):
            slot.objects.add().obj = o

        self.report({'INFO'}, f"Stored {len(live)} object(s) in '{slot.name}'")
        return {'FINISHED'}


_UNBLOCK_PROP = dict(
    name="Unblock Disabled",
    default=True,
    description=("Temporarily clear hide_viewport on objects that refuse to select. "
                 "Turn off to select only what is already visible"),
)


class VISSETS_OT_reselect(bpy.types.Operator):
    bl_idname = "object.visibility_sets_reselect"
    bl_label = "Reselect Sets"
    bl_description = ("Select the objects in the active selection set again. Disabled "
                      "objects are not selectable in Blender, so their hide flag is "
                      "temporarily cleared -- no keyframe, and the animation drives it "
                      "back on the next frame change")
    bl_options = {'REGISTER', 'UNDO'}

    unblock: bpy.props.BoolProperty(**_UNBLOCK_PROP)

    def execute(self, context):
        slot = _active_slot(context)
        objs = _slot_objects(context, slot)
        if not objs:
            self.report({'WARNING'}, "Active set is empty")
            return {'CANCELLED'}
        ok, touched = _reselect(context, objs, self.unblock)
        _report_reselect(self, f"'{slot.name}'", ok, len(objs), touched)
        return {'FINISHED'}


class VISSETS_OT_reselect_last(bpy.types.Operator):
    bl_idname = "object.visibility_sets_reselect_last"
    bl_label = "Reselect Last Keyed"
    bl_description = ("Select the objects from the most recent Show/Hide/Enable/Disable "
                      "again -- no selection set needed. Disabled objects have their "
                      "hide flag temporarily cleared (no keyframe) so they can be selected")
    bl_options = {'REGISTER', 'UNDO'}

    unblock: bpy.props.BoolProperty(**_UNBLOCK_PROP)

    def execute(self, context):
        st = _st(context)
        objs = _reachable(context, st.last_keyed) if st is not None else []
        if not objs:
            self.report({'WARNING'}, "Nothing keyed yet in this scene")
            return {'CANCELLED'}
        ok, touched = _reselect(context, objs, self.unblock)
        _report_reselect(self, "Last keyed", ok, len(objs), touched)
        return {'FINISHED'}


class VISSETS_OT_obj_select(bpy.types.Operator):
    bl_idname = "object.visibility_sets_obj_select"
    bl_label = "Select Set Object"
    bl_description = ("Select this object in the viewport. If it is disabled, hide_viewport "
                      "is cleared without a keyframe so it can be selected. "
                      "Shift-click to extend the selection")
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(options={'HIDDEN'})
    extend: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        slot = _active_slot(context)
        if slot is None or not (0 <= self.index < len(slot.objects)):
            return {'CANCELLED'}
        obj = slot.objects[self.index].obj
        if obj is None:
            return {'CANCELLED'}

        vl = context.view_layer
        if not self.extend:
            for o in list(vl.objects.selected):
                try:
                    o.select_set(False, view_layer=vl)
                except (RuntimeError, ReferenceError):
                    pass

        touched = []
        ok = _try_select(context, [obj])
        if not ok:
            touched = _unblock(context, [obj])
            ok = _try_select(context, [obj])

        slot.active_obj = self.index
        if not ok:
            self.report({'WARNING'},
                        f"'{obj.name}' unreachable (collection hidden/excluded or "
                        f"selection disabled)")
            return {'FINISHED'}

        try:
            vl.objects.active = obj
        except Exception:
            pass
        if touched:
            self.report({'INFO'},
                        f"'{obj.name}' unblocked - press Enable to keyframe or Ctrl+Z to revert")
        return {'FINISHED'}


class VISSETS_OT_obj_remove(bpy.types.Operator):
    bl_idname = "object.visibility_sets_obj_remove"
    bl_label = "Remove From Set"
    bl_description = "Remove this object from the active set (the object itself is untouched)"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(options={'HIDDEN'})

    def execute(self, context):
        slot = _active_slot(context)
        if slot is None or not (0 <= self.index < len(slot.objects)):
            return {'CANCELLED'}
        o = slot.objects[self.index].obj
        name = o.name if o is not None else "<deleted>"
        slot.objects.remove(self.index)
        if slot.active_obj >= len(slot.objects):
            slot.active_obj = max(0, len(slot.objects) - 1)
        self.report({'INFO'}, f"Removed '{name}' from '{slot.name}'")
        return {'FINISHED'}


class VISSETS_OT_obj_add(bpy.types.Operator):
    bl_idname = "object.visibility_sets_obj_add"
    bl_label = "Add Selected To Set"
    bl_description = ("Add the selected objects (viewport or Outliner) to the active set. "
                      "Objects already in the set are skipped")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        slot = _active_slot(context)
        if slot is None:
            return {'CANCELLED'}
        live = _live_selection(context)
        if not live:
            self.report({'WARNING'}, "Nothing selected")
            return {'CANCELLED'}

        have = {ref.obj for ref in slot.objects}
        new = sorted((o for o in live if o not in have), key=lambda o: o.name)
        if not new:
            self.report({'INFO'}, f"All selected objects are already in '{slot.name}'")
            return {'CANCELLED'}
        for o in new:
            slot.objects.add().obj = o
        self.report({'INFO'}, f"Added {len(new)} object(s) to '{slot.name}'")
        return {'FINISHED'}


# ======================================================================
# UI
# ======================================================================

class VISSETS_UL_slots(bpy.types.UIList):
    # No per-row loops: the count is len(), and reachability is not
    # checked here (see _slot_objects).
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        row = layout.row(align=True)
        if index == getattr(active_data, active_propname):
            row.prop(item, "expanded", text="", emboss=False,
                     icon='TRIA_DOWN' if item.expanded else 'TRIA_RIGHT')
        else:
            row.label(text="", icon='BLANK1')
        row.prop(item, "name", text="", emboss=False, icon='COPYDOWN')
        n = len(item.objects)
        sub = row.row()
        sub.alignment = 'RIGHT'
        sub.label(text=str(n) if n else "-")


class VISSETS_UL_objects(bpy.types.UIList):
    # Only visible rows are drawn, so this stays O(rows) regardless of
    # set size. Clicking the name fires obj_select (Shift extends);
    # the highlighted button state mirrors the viewport selection.
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        obj = item.obj
        row = layout.row(align=True)
        if obj is None:
            row.label(text="<deleted>", icon='ERROR')
        else:
            op = row.operator(
                "object.visibility_sets_obj_select", text=obj.name, emboss=False,
                depress=obj.select_get(view_layer=context.view_layer),
                icon='RESTRICT_VIEW_ON' if obj.hide_viewport else 'RESTRICT_VIEW_OFF',
            )
            op.index = index
        op = row.operator("object.visibility_sets_obj_remove", text="", icon='X', emboss=False)
        op.index = index

    def draw_filter(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "filter_name", text="", icon='VIEWZOOM')
        row.prop(self, "use_filter_sort_alpha", text="", icon='SORTALPHA')
        row.prop(self, "use_filter_sort_reverse", text="",
                 icon='SORT_DESC' if self.use_filter_sort_reverse else 'SORT_ASC')

    def filter_items(self, context, data, propname):
        # Empty lists = no filtering, no sorting, no work. The loops
        # below run only while a filter string or alpha sort is active.
        if not (self.filter_name or self.use_filter_sort_alpha):
            return [], []
        refs = getattr(data, propname)
        names = [(r.obj.name if r.obj is not None else "") for r in refs]
        flags, order = [], []
        if self.filter_name:
            pat = f"*{self.filter_name.lower()}*"
            bit = self.bitflag_filter_item
            flags = [bit if fnmatchcase(n.lower(), pat) else 0 for n in names]
        if self.use_filter_sort_alpha:
            order = [0] * len(names)
            for pos, i in enumerate(sorted(range(len(names)), key=lambda i: names[i].lower())):
                order[i] = pos
        return flags, order


class VISSETS_MT_object(bpy.types.Menu):
    bl_idname = "VISSETS_MT_object"
    bl_label = "Visibility Sets"

    def draw(self, context):
        layout = self.layout
        layout.operator("object.visibility_sets_render_show", icon='HIDE_OFF')
        layout.operator("object.visibility_sets_render_hide", icon='HIDE_ON')
        layout.separator()
        layout.operator("object.visibility_sets_viewport_enable", icon='CHECKBOX_HLT')
        layout.operator("object.visibility_sets_viewport_disable", icon='CHECKBOX_DEHLT')
        layout.separator()
        layout.operator("object.visibility_sets_store", text="Store Selection", icon='COPYDOWN')
        layout.operator("object.visibility_sets_reselect", text="Reselect Sets", icon='PASTEDOWN')
        layout.operator("object.visibility_sets_obj_add", text="Add Selected To Set", icon='ADD')
        layout.separator()
        layout.operator("object.visibility_sets_reselect_last", icon='LOOP_BACK')


def _draw_object_menu(self, context):
    self.layout.separator()
    self.layout.menu("VISSETS_MT_object", icon='COPYDOWN')


class VISSETS_PT_panel(bpy.types.Panel):
    bl_label = "Visibility Sets"
    bl_idname = "VISSETS_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Visibility"

    def draw(self, context):
        layout = self.layout
        st = _st(context)
        if st is None:
            layout.label(text="Not registered", icon='ERROR')
            return

        # No Outliner read in draw() -- way too costly per redraw.
        # selected_objects is cheap and good enough for the UI.
        live = context.selected_objects
        slot = _active_slot(context)
        n_slot = len(slot.objects) if slot is not None else 0

        # ---------- Selection sets ----------
        box = layout.box()
        box.label(text="Selection Sets", icon='COPYDOWN')

        row = box.row()
        row.template_list("VISSETS_UL_slots", "", st, "slots", st, "active_slot", rows=3)
        col = row.column(align=True)
        col.operator("object.visibility_sets_slot_add", text="", icon='ADD')
        sub = col.column(align=True)
        sub.enabled = bool(st.slots)
        sub.operator("object.visibility_sets_slot_remove", text="", icon='X')

        # ---------- Objects in the active set (dropdown) ----------
        if slot is not None and slot.expanded:
            sub = box.box()
            sub.template_list("VISSETS_UL_objects", "vissets_objs", slot, "objects",
                              slot, "active_obj", rows=min(max(n_slot, 2), 8), maxrows=8)
            # Enabled when there's something new to add, or when the viewport
            # shows nothing (it might be an Outliner selection draw() can't see).
            n_new = 0
            if live:
                have = {ref.obj for ref in slot.objects}
                n_new = sum(1 for o in live if o not in have)
            row = sub.row()
            row.enabled = n_new > 0 or not live
            row.operator("object.visibility_sets_obj_add",
                         text=f"Add {n_new} Selected" if n_new else "Add Selected", icon='ADD')

        row = box.row(align=True)
        row.operator("object.visibility_sets_store", text="Store", icon='COPYDOWN')
        sub = row.row(align=True)
        sub.enabled = n_slot > 0
        sub.operator("object.visibility_sets_reselect", text="Reselect Sets", icon='PASTEDOWN')

        # ---------- Current target ----------
        n_last = len(st.last_keyed)
        box = layout.box()
        if live:
            box.label(text=f"Target: {len(live)} selected", icon='RESTRICT_SELECT_OFF')
        else:
            box.label(text="Target: none", icon='ERROR')
            if n_last:
                pass  # the button right below is the hint
            elif n_slot:
                box.label(text=f"Reselect Sets to target '{slot.name}'", icon='INFO')
            else:
                box.label(text="Select objects to enable keying", icon='INFO')
        # Always shown, so the button never jumps around between frames.
        row = box.row()
        row.enabled = n_last > 0
        row.operator("object.visibility_sets_reselect_last",
                     text=f"Reselect Last Keyed ({n_last})" if n_last else "Reselect Last Keyed",
                     icon='LOOP_BACK')

        # ---------- Render visibility ----------
        box = layout.box()
        box.label(text="Render Visibility", icon='RESTRICT_RENDER_OFF')
        row = box.row(align=True)
        row.enabled = bool(live)
        row.operator("object.visibility_sets_render_show", text="Show", icon='HIDE_OFF')
        row.operator("object.visibility_sets_render_hide", text="Hide", icon='HIDE_ON')

        # ---------- Disable ----------
        box = layout.box()
        box.label(text="Disable (Viewport)", icon='RESTRICT_VIEW_OFF')
        box.prop(st, "link_render")
        row = box.row(align=True)
        row.enabled = bool(live)
        row.operator("object.visibility_sets_viewport_enable", text="Enable", icon='CHECKBOX_HLT')
        row.operator("object.visibility_sets_viewport_disable", text="Disable", icon='CHECKBOX_DEHLT')

        layout.label(text=f"Frame: {context.scene.frame_current}")


# ======================================================================
# Registration
# ======================================================================

classes = (
    VISSETS_ObjectRef,
    VISSETS_Slot,
    VISSETS_Settings,
    VISSETS_OT_render_show,
    VISSETS_OT_render_hide,
    VISSETS_OT_viewport_enable,
    VISSETS_OT_viewport_disable,
    VISSETS_OT_slot_add,
    VISSETS_OT_slot_remove,
    VISSETS_OT_store,
    VISSETS_OT_reselect,
    VISSETS_OT_reselect_last,
    VISSETS_OT_obj_select,
    VISSETS_OT_obj_remove,
    VISSETS_OT_obj_add,
    VISSETS_UL_slots,
    VISSETS_UL_objects,
    VISSETS_MT_object,
    VISSETS_PT_panel,
)

# Shortcuts ship unbound -- every action sits in Preferences > Keymap >
# Object Mode with an empty key slot, ready for you to fill in.
_KEYMAP_ITEMS = (
    "object.visibility_sets_render_show",
    "object.visibility_sets_render_hide",
    "object.visibility_sets_viewport_enable",
    "object.visibility_sets_viewport_disable",
    "object.visibility_sets_store",
    "object.visibility_sets_reselect",
    "object.visibility_sets_reselect_last",
    "object.visibility_sets_obj_add",
)
_addon_keymaps = []


def _register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon if wm is not None else None
    if kc is None:  # background mode
        return
    km = kc.keymaps.new(name="Object Mode", space_type='EMPTY')
    for idname in _KEYMAP_ITEMS:
        kmi = km.keymap_items.new(idname, 'NONE', 'PRESS')
        _addon_keymaps.append((km, kmi))


def _unregister_keymaps():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (RuntimeError, ReferenceError):
            pass
    _addon_keymaps.clear()


def _set_handler(handler_list, func, add):
    # Match by name so a script reload does not stack duplicates.
    for h in list(handler_list):
        if getattr(h, "__name__", "") == func.__name__:
            handler_list.remove(h)
    if add:
        handler_list.append(func)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.visibility_sets = bpy.props.PointerProperty(type=VISSETS_Settings)
    bpy.types.VIEW3D_MT_object.append(_draw_object_menu)
    _register_keymaps()
    _set_handler(bpy.app.handlers.depsgraph_update_post, _vissets_prune_handler, True)
    _set_handler(bpy.app.handlers.load_post, _vissets_load_post, True)


def unregister():
    _set_handler(bpy.app.handlers.depsgraph_update_post, _vissets_prune_handler, False)
    _set_handler(bpy.app.handlers.load_post, _vissets_load_post, False)
    _unregister_keymaps()
    bpy.types.VIEW3D_MT_object.remove(_draw_object_menu)
    _scene_sig.clear()
    del bpy.types.Scene.visibility_sets
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()