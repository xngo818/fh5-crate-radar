# Supported build and provenance

## Scope

The author supplied the original project as their own work. The FH5 adapter
was developed separately using read-only process observations and Data Out.
`pool.py` retains its bytecode reconstruction provenance. Original author
comments, formatting and unpublished build files were not recovered.

## Identity mapping

`data/fh5_loot_mapping.json` retains the 45 factual ID/name/tier associations
validated against the loaded tables for Xbox PC package 3.688.109.0.
The registered uint32 LootRowId joins to CarID and OverrideDisplayLevel.
All table rows were joined; not every vehicle prompt was individually checked
in a match. Unknown identities and other builds remain unresolved.
This public package includes a written provenance summary, not the private
raw memory records. The synthetic demo is not evidence of live correctness.
The supplemental own-car name table is limited to these same vehicles.

## Map alignment

The author checked a fixed own-car reference and real map labels at render
and physical client size 2560 by 1440, DPI 120, client_equals_render mode.
Pan/zoom tracking and close/reopen clearing/restoration were observed.
Pickup disappearance was confirmed by the user. Small pan/zoom ghosting was
reported. Raw captured coordinates, process addresses and session IDs are
intentionally absent from this public source package.

## Limits

Other display modes require calibration and fail closed. Geometry and clock
activity are lifecycle heuristics; an independent map-open field has not been
recovered. Loaded candidates are not a claim of all-world coverage or a fixed
72-object count. No cold-process discovery time or 20-second guarantee is
claimed. In the accepted version the on/off caption is tied to available crate
data, so a valid free-roam map with no crate data can omit the caption.

## Public package changes

Game PNGs, map textures, extraction tools, proprietary executables, original
licensing/update modules, private captures and local session hints are omitted.
Original crests fall back to circles/numbers; offline preview uses a plain grid.
No anti-cheat bypass, input injection or game memory writes are implemented.
