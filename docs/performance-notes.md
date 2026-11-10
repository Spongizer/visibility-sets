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

### When pruning runs

Because a set's pointer keeps a deleted object alive, an unpruned ref would also get that object written into the `.blend`. Pruning runs:

- in a **`save_pre`** handler, so deleted objects are dropped before the file is written,
- in a **`load_post`** handler, to clean files saved by older versions,
- at the start of **Reselect Sets** and **Reselect Last Keyed**.

Version 1.0 pruned from a `depsgraph_update_post` handler instead. It was gated to stay cheap, but a handler that runs on every depsgraph update (and writes to scene data) is a lot of machinery for a cosmetic benefit, so 1.1 dropped it. The trade-off: the object count shown next to a set can include deleted objects until the next save or reselect. Operators are unaffected, since `_reachable()` filters out objects that aren't in the view layer.

### `draw()` and UI lists

- **No Outliner read in `draw()` or `poll()`.** Running `temp_override` on every redraw is wasteful and unsafe. The panel only reads `context.selected_objects`, and the operators check the full selection in `execute()`.
- `context.selected_objects` is a C-built list, so it's cheaper than iterating the RNA collection into a set. `draw()` only needs its length, plus one membership pass when a set is expanded.
- **`_reachable()` is for operators only.** Its per-object view-layer lookup is too costly for `draw()`, which just uses `len(refs)`.
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

When an operator runs from the Outliner's right-click menu (`OUTLINER_MT_object`), `context.selected_ids` is read directly as well.

### No active-object fallback in `_selected_objects()`

Blender keeps `basact` pointing at the last active object even after everything is deselected. It shows up as a faint Outliner row and in the viewport header. Treating that as a target would key visibility on an object the user never picked, which is the worst possible failure mode here. If nothing is selected, nothing happens.

For the same reason, **Reselect Last Keyed** uses a record of what was actually keyed, not the active object. `basact` lingers on objects the user never picked and only ever names one object.

### Sets are never a silent target

A selection set has to be turned into a real selection with **Reselect** first, so what gets keyed is always what shows an outline in the viewport.

Set refs are ID pointers, not names, so renaming an object doesn't break a set.

### Four keyframe operators instead of one

Keymap items are listed by `bl_label`. One "Set Visibility Keyframe" operator with properties would show up four times under the same name, and the user would have to expand each row to tell *Render: Hide* from *Viewport: Disable*. With visibility as the payload, that mix-up is too expensive to allow.

### Unblock is self-healing

The reselect operators clear `hide_viewport` **without a keyframe** on objects that refuse to select. The next frame change lets the animation system drive the flag back, and pressing Enable commits it as a keyframe instead.

That only holds when there *is* an F-curve to drive it back. Objects disabled by hand, and the eye toggle (`hide_set`, which can't be animated), stay cleared until Ctrl+Z. This is intentional: it lets a set recall objects regardless of how they were hidden.

### No preference changes

Boolean properties get discrete F-curves, and Blender inserts their keys with Constant interpolation regardless of **Preferences → Animation → Default Interpolation** (checked on 4.2 and 5.2). Version 1.0 temporarily switched that preference while keying; 1.1 doesn't touch user preferences at all.
