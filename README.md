# FH5 Crate Radar — Windows releases

[Download a release](https://github.com/xngo818/fh5-crate-radar/releases/tag/1.1.1) · [Browse source](https://github.com/xngo818/fh5-crate-radar/tree/source)

This branch contains release notes and download instructions. ZIP packages belong
in GitHub Releases assets; Python source is maintained on the `source` branch.

## first-map-v10

Improves initial map camera detection and scanning recovery while retaining
vehicle-level sorting, statistics, and scan caching.

Download `FH5-Crate-Radar-first-map-v10-Windows.zip` from the release assets,
extract the entire archive, and run `FH5CrateCoordinates.exe`.
When upgrading, close the old program and keep its `state` folder to preserve
settings and local data.

Target: Windows, Forza Horizon 5 Xbox PC build **3.688.109.0**.
Data Out: **127.0.0.1:53005** over UDP.

The 2026-10-26 build passed 443 project tests and 38 frozen-camera tests.
First-match map behavior still awaits in-game verification.
The source edition uses fallback graphics; the existing Windows package retains
its bundled graphics and is not a byte-for-byte build of that source edition.
