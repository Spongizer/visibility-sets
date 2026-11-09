# Performance & Design Notes

Background for the parts of `__init__.py` that look odd at first glance. The code comments were trimmed for release, so the measurements and the reasoning behind them live here instead. Read this before "cleaning up" any of these spots.

Benchmark scene for the numbers below: **20,000 objects, 2,000 refs in a set / 2,000 objects being worked on**, unless stated otherwise.

---

## Measured numbers

| Where | Slow way | Fast way (what the code does) | Result |
| --- | --- | --- | --- |
| `_alive_objects()` | `Object.users_scene` per ref | One pass over every `scene.objects` into a set | **4.4 s → 2.8 ms** |
| `_prune_refs()` | Indexed loop (`refs[i]`) | Iterate with `enumerate(refs)` | **126 ms → 0.7 ms** |
| `_unblock()` | `hide_set` and `hide_viewport` interleaved per object | All `hide_get`/`hide_set` first, all `hide_viewport` writes last | **2.7x faster** |
| `_scene_signature()` | — | Two object counts, O(N) each | **~2 ms** per call |

---

## Details

### `_alive_objects()`: finding deleted objects

An ID `PointerProperty` holds a user, so an object deleted in the viewport is only **unlinked, not freed**. Its pointer in a set stays valid and has to be detected some other way.

- `Object.users_scene` would tell you, but it walks every scene's collection tree per call. That's O(N) per object, so O(K·N) across a set. **Measured 4.4 s.**
- One pass over `scene.objects` for every scene is O(N) total, and set membership is pointer-hashed. **Measured 2.8 ms.**

### `_prune_refs()`: iterate, don't index

RNA resolves an integer index on this collection by **walking from the start** every time, so `for i in range(len(refs)): refs[i]` is O(K²).

- Indexed loop: **126 ms**. Iteration: **0.7 ms** (2,000 refs).
- Removal still uses indices, but removal is rare, so that cost only applies to the refs actually dropped.

### `_unblock()`: write order matters

Each `hide_viewport` write marks the view layer out of sync. `hide_get`/`hide_set` then have to resync it (O(N)) before they can read a Base.

- Interleaving them per object makes the whole thing O(K·N).
- Doing all the Base-level work first and all `hide_viewport` writes last was **measured 2.7x faster**.

### `_vissets_prune_handler()`: the depsgraph handler

This runs on **every depsgraph update**, including each mouse move during a G/R/S drag, so the common path has to cost almost nothing. It sits behind two gates:

1. **`depsgraph.id_type_updated('COLLECTION')`**: adding, deleting, duplicating and unlinking objects all tag a Collection. Transforms and ordinary property edits don't. It's one C flag read, so drags cost ~0.
2. **Object-count signature** `(len(scene.objects), len(bpy.data.objects))`: deleting an object always changes the scene's count, and adding one always changes `bpy.data`'s. Tracking both means "delete one + add one in the same update" still trips the check. Both counts are O(N) RNA walks (**~2 ms at 20k objects**), which is why they sit behind gate 1.

The signature cache is keyed by scene **pointer**, not name, so renaming a scene can't leave a dead entry behind. (Undo may reallocate the Scene, which just costs one extra prune pass.)

A missed structural change only leaves a stale count in the UI until the next one. Operators always go through `_reachable()`, which filters deleted objects on its own.

### `draw()` and UI lists

- **No Outliner read in `draw()`.** Running `temp_override` on every redraw is wasteful and unsafe. The panel only reads `context.selected_objects`.
- `context.selected_objects` is a C-built list, so it's cheaper than iterating the RNA collection into a set. `draw()` only needs its length, plus one membership pass when a set is expanded.
- **`_reachable()` is for operators only.** Its per-object view-layer lookup is too costly for `draw()`, which just uses `len(refs)`. That count stays honest because the depsgraph handler prunes stale refs.
- **`VISSETS_UL_slots`** does no per-row loops: the count is `len()`, and reachability isn't checked.
- **`VISSETS_UL_objects`** only draws visible rows, so it stays O(rows) no matter how big the set is. `filter_items()` returns empty lists (no filtering, no sorting, no work) unless a filter string or alpha sort is active.

---

## Design decisions (non-performance)

These were in the original comments too.

### Why disabled objects can't be selected

Setting `hide_viewport = True` makes an object's Base non-selectable, and `layer_collection_objects_sync()` ends with:

```c
if ((base->flag & BASE_SELECTABLE) == 0)
    base->flag &= ~BASE_SELECTED;
```

That sync runs on every viewport redraw, so a disabled object can't hold a selection at all. `select_set(True)` silently does nothing, and the Outliner's row highlighting has no write API. That's the whole reason this addon exists.

### Reading the Outliner selection

Outliner rows survive disabling, so a multi-selection made there is still readable. `selected_ids` only exists in Outliner context and needs the **WINDOW region** in the override, not just the area. It has to be read through `bpy.context` inside the override. Reading the captured `context` instead is what silently broke this before.

### No active-object fallback in `_get_targets()`

Blender keeps `basact` pointing at the last active object even after everything is deselected. It shows up as a faint Outliner row and in the viewport header. Treating that as a target would key visibility on an object the user never picked, which is the worst possible failure mode here. If nothing is selected, nothing happens.

For the same reason, **Reselect Last Keyed** uses a record of what was actually keyed, not the active object. `basact` lingers on objects the user never picked and only ever names one object.

### Sets are never a silent target

A selection set has to be turned into a real selection with **Reselect** first, so what gets keyed is always what shows an outline in the viewport.

Set refs are ID pointers, not names, so renaming an object doesn't break a set.

### Four keyframe operators instead of one

Keymap items are listed by `bl_label`. One "Set Visibility Keyframe" operator with properties would show up four times under the same name, and the user would have to expand each row to tell *Render: Hide* from *Viewport: Disable*. With visibility as the payload, that mix-up is too expensive to allow.

### Unblock is self-healing

The reselect operators clear `hide_viewport` **without a keyframe** on objects that refuse to select. The next frame change lets the animation system drive the flag back, and pressing Enable commits it as a keyframe instead.
