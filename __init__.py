# SPDX-FileCopyrightText: 2026 Sponge
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Visibility Sets: keyframe object visibility in bulk, and get disabled
objects back as a selection using named sets saved in the .blend file.

Blender clears the selection on objects that are disabled in viewports
(``hide_viewport``), so once a batch is disabled it can't be selected again
to key it back on. The add-on keeps its own references to those objects
(the last keyed batch, plus named sets) and briefly clears their hide flags,
without a keyframe, so they become selectable again.
"""

import re
from fnmatch import fnmatchcase

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)


# ----------------------------------------------------------------------
# Settings access
# ----------------------------------------------------------------------

def _settings(context):
    return context.scene.visibility_sets


def _active_slot(st):
    if not st.slots:
        return None
    return st.slots[max(0, min(st.active_slot, len(st.slots) - 1))]


def _in_view_layer(context, obj):
    return context.view_layer.objects.get(obj.name) == obj


def _reachable(context, refs):
    """Objects from ``refs`` that are still in the current view layer.

    Used by operators only; draw() just shows ``len(refs)``.
    """
    return [ref.obj for ref in refs
            if ref.obj is not None and _in_view_layer(context, ref.obj)]


# ----------------------------------------------------------------------
# Set names
# ----------------------------------------------------------------------

_SUFFIX_RE = re.compile(r"^(.*?)(?: \((\d+)\))?$")


def _unique_name(st, name, skip=None):
    """Return ``name``, or ``name (n)`` if another set already uses it."""
    skip_ptr = skip.as_pointer() if skip is not None else None
    taken = {sl.name for sl in st.slots if sl.as_pointer() != skip_ptr}
    if name not in taken:
        return name
    base = _SUFFIX_RE.match(name).group(1) or name
    i = 1
    while f"{base} ({i})" in taken:
        i += 1
    return f"{base} ({i})"


def _slot_name_update(self, _context):
    # Writing self["name"] bypasses this update callback, so it can't recurse.
    st = self.id_data.visibility_sets
    fixed = _unique_name(st, self.name, skip=self)
    if fixed != self.name:
        self["name"] = fixed


# ----------------------------------------------------------------------
# Pruning references to deleted objects
#
# An ID PointerProperty counts as a user, so an object deleted in the
# viewport is only unlinked: it stays in bpy.data and would be written to
# the .blend for as long as a set points at it. References are pruned when
# a file is loaded, before it is saved, and when the reselect operators run.
# See docs/performance-notes.md for the measurements behind this code.
# ----------------------------------------------------------------------

def _alive_objects():
    """Every object linked to any scene (one pass, pointer-hashed)."""
    alive = set()
    for scene in bpy.data.scenes:
        alive.update(scene.objects)
    return alive


def _prune_refs(refs, alive):
    """Drop refs to deleted objects.

    Iterate rather than index: ``refs[i]`` walks the collection from the
    start each time, which makes an indexed loop O(n^2).
    """
    dead = [i for i, ref in enumerate(refs) if ref.obj is None or ref.obj not in alive]
    for i in reversed(dead):
        refs.remove(i)


def _prune_settings(st, alive):
    for slot in st.slots:
        _prune_refs(slot.objects, alive)
        if slot.active_obj >= len(slot.objects):
            slot.active_obj = max(0, len(slot.objects) - 1)
    _prune_refs(st.last_keyed, alive)


def _prune_all_scenes():
    alive = None
    for scene in bpy.data.scenes:
        st = scene.visibility_sets
        if not (st.slots or st.last_keyed):
            continue
        if alive is None:
            alive = _alive_objects()
        _prune_settings(st, alive)


@persistent
def _vissets_load_post(*_args):
    _prune_all_scenes()


@persistent
def _vissets_save_pre(*_args):
    _prune_all_scenes()


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------

def _context_outliner_objects(context):
    """Objects in ``context.selected_ids`` (set when running from the Outliner)."""
    ids = getattr(context, "selected_ids", None) or ()
    return [item for item in ids if isinstance(item, bpy.types.Object)]


def _open_outliner_objects(context):
    """Objects highlighted in any open Outliner, read from any editor.

    Disabled objects can still be selected as Outliner rows, but Blender only
    exposes ``selected_ids`` in Outliner context. So each Outliner's WINDOW
    region is entered with ``temp_override`` and the member is read through
    ``bpy.context`` inside it (the captured ``context`` doesn't see it).
    Operators only: too costly for draw() or poll().
    """
    objs = set()
    wm = context.window_manager
    if wm is None:
        return objs
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'OUTLINER':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            with context.temp_override(window=window, area=area, region=region):
                objs.update(_context_outliner_objects(bpy.context))
    return objs


def _selected_objects(context):
    """Viewport selection plus Outliner selection, sorted by name."""
    objs = set(context.selected_objects)
    objs.update(_context_outliner_objects(context))
    objs.update(_open_outliner_objects(context))
    return sorted(objs, key=lambda o: o.name)


def _sync(context):
    """Flush the lazy view-layer resync so Base selection flags are current."""
    context.view_layer.update()


def _select(context, objs):
    """Select ``objs`` (all in the view layer) and return the ones that took."""
    vl = context.view_layer
    _sync(context)
    for obj in objs:
        obj.select_set(True, view_layer=vl)
    return [obj for obj in objs if obj.select_get(view_layer=vl)]


def _deselect_all(context):
    vl = context.view_layer
    for obj in context.selected_objects:
        obj.select_set(False, view_layer=vl)


# ----------------------------------------------------------------------
# Unblocking disabled objects
# ----------------------------------------------------------------------

def _unblock(context, objs):
    """Clear the hide flags that block selection, without a keyframe.

    On keyframed objects the next frame change drives ``hide_viewport`` back;
    pressing Enable commits it as a keyframe instead, and Ctrl+Z reverts it.
    All ``hide_set`` calls go first and ``hide_viewport`` writes last:
    interleaving them forces a view-layer resync per object.
    """
    vl = context.view_layer
    _sync(context)
    objs = [obj for obj in objs if obj.is_editable]
    touched = set()
    for obj in objs:
        if obj.hide_get(view_layer=vl):
            obj.hide_set(False, view_layer=vl)
            touched.add(obj)
    for obj in objs:
        if obj.hide_viewport:
            obj.hide_viewport = False
            touched.add(obj)
    return sorted(touched, key=lambda o: o.name)


def _reselect(context, objs, unblock):
    """Replace the selection with ``objs``. Returns (selected, unblocked)."""
    _deselect_all(context)
    selected = _select(context, objs)
    touched = []
    if unblock and len(selected) < len(objs):
        chosen = set(selected)
        touched = _unblock(context, [obj for obj in objs if obj not in chosen])
        if touched:
            selected = _select(context, objs)
    if selected:
        context.view_layer.objects.active = selected[0]
    return selected, touched


def _report_reselect(op, label, objs, selected, touched):
    missed = len(objs) - len(selected)
    if missed:
        op.report({'WARNING'},
                  f"{label}: {len(selected)}/{len(objs)} selected - {missed} unreachable "
                  f"(collection hidden or excluded)")
    elif touched:
        op.report({'INFO'},
                  f"{label}: {len(selected)} selected - {len(touched)} unblocked, press "
                  f"Enable to keyframe or Ctrl+Z to revert")
    else:
        op.report({'INFO'}, f"{label}: {len(selected)} object(s) selected")


def _key_visibility(context, objs, mode, show):
    """Set and keyframe visibility on ``objs``. Returns (keyed, skipped).

    Boolean properties get discrete F-curves, so Blender inserts these keys
    with Constant interpolation regardless of the user's preferences.
    """
    st = _settings(context)
    frame = context.scene.frame_current
    hide = not show
    if mode == 'RENDER':
        paths = ("hide_render",)
    elif st.link_render:
        paths = ("hide_viewport", "hide_render")
    else:
        paths = ("hide_viewport",)

    keyed = []
    skipped = 0
    for obj in objs:
        if not obj.is_editable:  # Linked from a library.
            skipped += 1
            continue
        try:
            for path in paths:
                setattr(obj, path, hide)
                obj.keyframe_insert(data_path=path, frame=frame)
        except RuntimeError:
            skipped += 1
            continue
        keyed.append(obj)

    if keyed:
        st.last_keyed.clear()
        for obj in keyed:
            st.last_keyed.add().obj = obj
        # Mirror an Outliner selection in the viewport where possible.
        _select(context, [obj for obj in keyed if _in_view_layer(context, obj)])
    return keyed, skipped


# ======================================================================
# Properties
# ======================================================================

class VISSETS_ObjectRef(bpy.types.PropertyGroup):
    # An ID pointer rather than a name, so renaming an object keeps the ref.
    obj: PointerProperty(type=bpy.types.Object)


class VISSETS_Slot(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Set", update=_slot_name_update)
    objects: CollectionProperty(type=VISSETS_ObjectRef)
    expanded: BoolProperty(
        name="Show Objects",
        description="List the objects in this set",
        default=False,
    )
    active_obj: IntProperty(default=0, options={'HIDDEN'})


class VISSETS_Settings(bpy.types.PropertyGroup):
    link_render: BoolProperty(
        name="Also key Render",
        description="Keyframe render visibility together with viewport visibility "
                    "on Enable / Disable",
        default=False,
    )
    slots: CollectionProperty(type=VISSETS_Slot)
    active_slot: IntProperty(name="Active Set", default=0)
    # Objects keyed by the most recent Show / Hide / Enable / Disable.
    last_keyed: CollectionProperty(type=VISSETS_ObjectRef)


# ======================================================================
# Operators
# ======================================================================

class _KeyframeOpBase:
    """Shared body of the four keyframe operators.

    Kept as four operators rather than one with an enum so each has its own
    name in the keymap editor and in Assign Shortcut.
    """
    bl_options = {'REGISTER', 'UNDO'}
    _mode = 'RENDER'  # 'RENDER' | 'VIEWPORT'
    _show = True

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        # Selection is checked here, not in poll(): reading the Outliner
        # selection is too costly for poll(), which runs on every redraw.
        targets = _selected_objects(context)
        if not targets:
            self.report({'WARNING'}, "Nothing selected - select objects, or use "
                                     "Reselect Sets / Reselect Last Keyed")
            return {'CANCELLED'}
        keyed, skipped = _key_visibility(context, targets, self._mode, self._show)
        if skipped:
            self.report({'WARNING'},
                        f"{self.bl_label}: keyed {len(keyed)}, skipped {skipped} "
                        f"(linked or not keyable)")
        else:
            self.report({'INFO'},
                        f"{self.bl_label}: {len(keyed)} object(s) at frame "
                        f"{context.scene.frame_current}")
        return {'FINISHED'} if keyed else {'CANCELLED'}


class VISSETS_OT_render_show(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_render_show"
    bl_label = "Render: Show"
    bl_description = "Keyframe the selected objects as visible in renders at the current frame"
    _mode, _show = 'RENDER', True


class VISSETS_OT_render_hide(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_render_hide"
    bl_label = "Render: Hide"
    bl_description = "Keyframe the selected objects as hidden in renders at the current frame"
    _mode, _show = 'RENDER', False


class VISSETS_OT_viewport_enable(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_viewport_enable"
    bl_label = "Viewport: Enable"
    bl_description = ("Keyframe the selected objects as enabled in viewports at the current "
                      "frame (and visible in renders if 'Also key Render' is on)")
    _mode, _show = 'VIEWPORT', True


class VISSETS_OT_viewport_disable(_KeyframeOpBase, bpy.types.Operator):
    bl_idname = "object.visibility_sets_viewport_disable"
    bl_label = "Viewport: Disable"
    bl_description = ("Keyframe the selected objects as disabled in viewports at the current "
                      "frame (and hidden in renders if 'Also key Render' is on)")
    _mode, _show = 'VIEWPORT', False


class VISSETS_OT_slot_add(bpy.types.Operator):
    bl_idname = "object.visibility_sets_slot_add"
    bl_label = "Add Selection Set"
    bl_description = "Add an empty selection set and make it active"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        st = _settings(context)
        slot = st.slots.add()
        slot.name = _unique_name(st, "Set", skip=slot)
        st.active_slot = len(st.slots) - 1
        return {'FINISHED'}


class VISSETS_OT_slot_remove(bpy.types.Operator):
    bl_idname = "object.visibility_sets_slot_remove"
    bl_label = "Remove Selection Set"
    bl_description = "Delete the active selection set (the objects are not affected)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and bool(_settings(context).slots)

    def execute(self, context):
        st = _settings(context)
        idx = max(0, min(st.active_slot, len(st.slots) - 1))
        st.slots.remove(idx)
        st.active_slot = min(idx, max(0, len(st.slots) - 1))
        return {'FINISHED'}


class VISSETS_OT_store(bpy.types.Operator):
    bl_idname = "object.visibility_sets_store"
    bl_label = "Store Selection"
    bl_description = ("Save the current selection into the active set, replacing its "
                      "contents. Creates a set if there is none")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        objs = _selected_objects(context)
        if not objs:
            self.report({'WARNING'}, "Nothing selected")
            return {'CANCELLED'}
        st = _settings(context)
        slot = _active_slot(st)
        if slot is None:
            slot = st.slots.add()
            slot.name = _unique_name(st, "Set", skip=slot)
            st.active_slot = len(st.slots) - 1

        slot.objects.clear()
        for obj in objs:
            slot.objects.add().obj = obj
        slot.active_obj = 0
        self.report({'INFO'}, f"Stored {len(objs)} object(s) in '{slot.name}'")
        return {'FINISHED'}


_UNBLOCK_PROP = dict(
    name="Unblock Disabled",
    description=("Temporarily clear hide_viewport on objects that refuse to select. "
                 "Turn off to select only what is already visible"),
    default=True,
)


class VISSETS_OT_reselect(bpy.types.Operator):
    bl_idname = "object.visibility_sets_reselect"
    bl_label = "Reselect Sets"
    bl_description = ("Select the objects in the active set again. Disabled objects are "
                      "not selectable in Blender, so their hide flag is temporarily "
                      "cleared -- no keyframe, and the animation drives it back on the "
                      "next frame change")
    bl_options = {'REGISTER', 'UNDO'}

    unblock: BoolProperty(**_UNBLOCK_PROP)

    @classmethod
    def poll(cls, context):
        if context.scene is None:
            return False
        slot = _active_slot(_settings(context))
        if slot is None or not slot.objects:
            cls.poll_message_set("The active set is empty")
            return False
        return True

    def execute(self, context):
        st = _settings(context)
        _prune_settings(st, _alive_objects())
        slot = _active_slot(st)
        objs = _reachable(context, slot.objects)
        if not objs:
            self.report({'WARNING'}, f"'{slot.name}': no objects in this view layer")
            return {'CANCELLED'}
        selected, touched = _reselect(context, objs, self.unblock)
        _report_reselect(self, f"'{slot.name}'", objs, selected, touched)
        return {'FINISHED'}


class VISSETS_OT_reselect_last(bpy.types.Operator):
    bl_idname = "object.visibility_sets_reselect_last"
    bl_label = "Reselect Last Keyed"
    bl_description = ("Select the objects from the most recent Show / Hide / Enable / "
                      "Disable again. Disabled objects have their hide flag temporarily "
                      "cleared (no keyframe) so they can be selected")
    bl_options = {'REGISTER', 'UNDO'}

    unblock: BoolProperty(**_UNBLOCK_PROP)

    @classmethod
    def poll(cls, context):
        if context.scene is None or not _settings(context).last_keyed:
            cls.poll_message_set("Nothing has been keyed in this scene yet")
            return False
        return True

    def execute(self, context):
        st = _settings(context)
        _prune_settings(st, _alive_objects())
        objs = _reachable(context, st.last_keyed)
        if not objs:
            self.report({'WARNING'}, "Last keyed objects are no longer in this view layer")
            return {'CANCELLED'}
        selected, touched = _reselect(context, objs, self.unblock)
        _report_reselect(self, "Last keyed", objs, selected, touched)
        return {'FINISHED'}


class VISSETS_OT_obj_select(bpy.types.Operator):
    bl_idname = "object.visibility_sets_obj_select"
    bl_label = "Select Set Object"
    bl_description = ("Select this object in the viewport. If it is disabled, hide_viewport "
                      "is cleared without a keyframe so it can be selected. "
                      "Shift-click to extend the selection")
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(options={'HIDDEN'})
    extend: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        slot = _active_slot(_settings(context))
        if slot is None or not 0 <= self.index < len(slot.objects):
            return {'CANCELLED'}
        slot.active_obj = self.index
        obj = slot.objects[self.index].obj
        if obj is None or not _in_view_layer(context, obj):
            self.report({'WARNING'}, "Object is not in this view layer")
            return {'CANCELLED'}

        if not self.extend:
            _deselect_all(context)
        selected = _select(context, [obj])
        touched = []
        if not selected:
            touched = _unblock(context, [obj])
            if touched:
                selected = _select(context, [obj])
        if not selected:
            self.report({'WARNING'},
                        f"'{obj.name}' unreachable (collection hidden/excluded or "
                        f"selection disabled)")
            return {'FINISHED'}

        context.view_layer.objects.active = obj
        if touched:
            self.report({'INFO'},
                        f"'{obj.name}' unblocked - press Enable to keyframe or Ctrl+Z to revert")
        return {'FINISHED'}


class VISSETS_OT_obj_remove(bpy.types.Operator):
    bl_idname = "object.visibility_sets_obj_remove"
    bl_label = "Remove From Set"
    bl_description = "Remove this object from the active set (the object is not affected)"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(options={'HIDDEN'})

    def execute(self, context):
        slot = _active_slot(_settings(context))
        if slot is None or not 0 <= self.index < len(slot.objects):
            return {'CANCELLED'}
        slot.objects.remove(self.index)
        if slot.active_obj >= len(slot.objects):
            slot.active_obj = max(0, len(slot.objects) - 1)
        return {'FINISHED'}


class VISSETS_OT_obj_add(bpy.types.Operator):
    bl_idname = "object.visibility_sets_obj_add"
    bl_label = "Add Selected To Set"
    bl_description = ("Add the selected objects to the active set, skipping objects "
                      "already in it")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.scene is None or _active_slot(_settings(context)) is None:
            cls.poll_message_set("Add a selection set first")
            return False
        return True

    def execute(self, context):
        objs = _selected_objects(context)
        if not objs:
            self.report({'WARNING'}, "Nothing selected")
            return {'CANCELLED'}
        slot = _active_slot(_settings(context))
        have = {ref.obj for ref in slot.objects}
        new = [obj for obj in objs if obj not in have]
        if not new:
            self.report({'INFO'}, f"All selected objects are already in '{slot.name}'")
            return {'CANCELLED'}
        for obj in new:
            slot.objects.add().obj = obj
        self.report({'INFO'}, f"Added {len(new)} object(s) to '{slot.name}'")
        return {'FINISHED'}


# ======================================================================
# UI
# ======================================================================

class VISSETS_UL_slots(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon,
                  active_data, active_propname, index):
        row = layout.row(align=True)
        if index == getattr(active_data, active_propname):
            row.prop(item, "expanded", text="", emboss=False,
                     icon='TRIA_DOWN' if item.expanded else 'TRIA_RIGHT')
        else:
            row.label(text="", icon='BLANK1')
        row.prop(item, "name", text="", emboss=False, icon='COPYDOWN')
        count = len(item.objects)
        sub = row.row()
        sub.alignment = 'RIGHT'
        sub.label(text=str(count) if count else "-")


class VISSETS_UL_objects(bpy.types.UIList):
    # Only visible rows are drawn, so this stays O(rows) for any set size.
    def draw_item(self, context, layout, _data, item, _icon,
                  _active_data, _active_propname, index):
        obj = item.obj
        row = layout.row(align=True)
        if obj is None:
            row.label(text="(deleted)", icon='ERROR')
        else:
            op = row.operator(
                VISSETS_OT_obj_select.bl_idname, text=obj.name, emboss=False,
                depress=obj.select_get(view_layer=context.view_layer),
                icon='RESTRICT_VIEW_ON' if obj.hide_viewport else 'RESTRICT_VIEW_OFF',
            )
            op.index = index
        op = row.operator(VISSETS_OT_obj_remove.bl_idname, text="", icon='X', emboss=False)
        op.index = index

    def draw_filter(self, _context, layout):
        row = layout.row(align=True)
        row.prop(self, "filter_name", text="", icon='VIEWZOOM')
        row.prop(self, "use_filter_sort_alpha", text="", icon='SORTALPHA')
        row.prop(self, "use_filter_sort_reverse", text="",
                 icon='SORT_DESC' if self.use_filter_sort_reverse else 'SORT_ASC')

    def filter_items(self, _context, data, propname):
        # Empty lists mean "no filtering, no sorting", so idle lists cost nothing.
        if not (self.filter_name or self.use_filter_sort_alpha):
            return [], []
        names = [(ref.obj.name if ref.obj is not None else "")
                 for ref in getattr(data, propname)]
        flags, order = [], []
        if self.filter_name:
            pattern = f"*{self.filter_name.lower()}*"
            flags = [self.bitflag_filter_item if fnmatchcase(n.lower(), pattern) else 0
                     for n in names]
        if self.use_filter_sort_alpha:
            order = [0] * len(names)
            for pos, i in enumerate(sorted(range(len(names)), key=lambda i: names[i].lower())):
                order[i] = pos
        return flags, order


class VISSETS_MT_object(bpy.types.Menu):
    bl_idname = "VISSETS_MT_object"
    bl_label = "Visibility Sets"

    def draw(self, _context):
        layout = self.layout
        layout.operator(VISSETS_OT_render_show.bl_idname, icon='HIDE_OFF')
        layout.operator(VISSETS_OT_render_hide.bl_idname, icon='HIDE_ON')
        layout.separator()
        layout.operator(VISSETS_OT_viewport_enable.bl_idname, icon='CHECKBOX_HLT')
        layout.operator(VISSETS_OT_viewport_disable.bl_idname, icon='CHECKBOX_DEHLT')
        layout.separator()
        layout.operator(VISSETS_OT_store.bl_idname, icon='COPYDOWN')
        layout.operator(VISSETS_OT_obj_add.bl_idname, icon='ADD')
        layout.operator(VISSETS_OT_reselect.bl_idname, icon='PASTEDOWN')
        layout.separator()
        layout.operator(VISSETS_OT_reselect_last.bl_idname, icon='LOOP_BACK')


def _draw_menu(self, _context):
    self.layout.separator()
    self.layout.menu(VISSETS_MT_object.bl_idname, icon='COPYDOWN')


class VISSETS_PT_panel(bpy.types.Panel):
    bl_label = "Visibility Sets"
    bl_idname = "VISSETS_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Visibility"

    def draw(self, context):
        layout = self.layout
        st = _settings(context)
        # No Outliner read in draw() (too costly per redraw);
        # context.selected_objects is cheap and good enough for the UI.
        selected = context.selected_objects
        slot = _active_slot(st)
        n_slot = len(slot.objects) if slot is not None else 0

        # Selection sets
        box = layout.box()
        box.label(text="Selection Sets", icon='COPYDOWN')
        row = box.row()
        row.template_list("VISSETS_UL_slots", "", st, "slots", st, "active_slot", rows=3)
        col = row.column(align=True)
        col.operator(VISSETS_OT_slot_add.bl_idname, text="", icon='ADD')
        col.operator(VISSETS_OT_slot_remove.bl_idname, text="", icon='X')

        if slot is not None and slot.expanded:
            sub = box.box()
            sub.template_list("VISSETS_UL_objects", "vissets_objs", slot, "objects",
                              slot, "active_obj", rows=min(max(n_slot, 2), 8), maxrows=8)
            have = {ref.obj for ref in slot.objects}
            n_new = sum(1 for obj in selected if obj not in have)
            # Also enabled when the viewport shows nothing: there may be an
            # Outliner selection that draw() can't see.
            row = sub.row()
            row.enabled = n_new > 0 or not selected
            row.operator(VISSETS_OT_obj_add.bl_idname,
                         text=f"Add {n_new} Selected" if n_new else "Add Selected",
                         icon='ADD')

        row = box.row(align=True)
        row.operator(VISSETS_OT_store.bl_idname, text="Store", icon='COPYDOWN')
        row.operator(VISSETS_OT_reselect.bl_idname, text="Reselect Sets", icon='PASTEDOWN')

        # Target
        n_last = len(st.last_keyed)
        box = layout.box()
        if selected:
            box.label(text=f"Target: {len(selected)} selected", icon='RESTRICT_SELECT_OFF')
        else:
            # draw() can't see the Outliner selection, so this only reports
            # the viewport; the keying buttons stay enabled for Outliner use.
            box.label(text="Target: Outliner selection, if any", icon='OUTLINER')
            if not n_last and n_slot:
                box.label(text=f"Or Reselect Sets to target '{slot.name}'", icon='INFO')
        box.operator(VISSETS_OT_reselect_last.bl_idname,
                     text=f"Reselect Last Keyed ({n_last})" if n_last
                     else "Reselect Last Keyed",
                     icon='LOOP_BACK')

        # Render visibility
        box = layout.box()
        box.label(text="Render Visibility", icon='RESTRICT_RENDER_OFF')
        row = box.row(align=True)
        row.operator(VISSETS_OT_render_show.bl_idname, text="Show", icon='HIDE_OFF')
        row.operator(VISSETS_OT_render_hide.bl_idname, text="Hide", icon='HIDE_ON')

        # Viewport visibility
        box = layout.box()
        box.label(text="Disable (Viewport)", icon='RESTRICT_VIEW_OFF')
        box.prop(st, "link_render")
        row = box.row(align=True)
        row.operator(VISSETS_OT_viewport_enable.bl_idname, text="Enable", icon='CHECKBOX_HLT')
        row.operator(VISSETS_OT_viewport_disable.bl_idname, text="Disable",
                     icon='CHECKBOX_DEHLT')

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

_handlers = (
    (bpy.app.handlers.load_post, _vissets_load_post),
    (bpy.app.handlers.save_pre, _vissets_save_pre),
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.visibility_sets = PointerProperty(type=VISSETS_Settings)
    bpy.types.VIEW3D_MT_object.append(_draw_menu)
    bpy.types.OUTLINER_MT_object.append(_draw_menu)
    for handler_list, func in _handlers:
        if func not in handler_list:
            handler_list.append(func)


def unregister():
    for handler_list, func in _handlers:
        if func in handler_list:
            handler_list.remove(func)
    bpy.types.OUTLINER_MT_object.remove(_draw_menu)
    bpy.types.VIEW3D_MT_object.remove(_draw_menu)
    del bpy.types.Scene.visibility_sets
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
