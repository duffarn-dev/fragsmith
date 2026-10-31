# Troubleshooting

## "Demo file incompatible with this game version"

CS2 refuses to play demos from older game versions. Demos have to be rendered while they're **fresh**, meaning the same or a close game version. Old demos can still be parsed and analyzed (steps 1–3), but never filmed.

Ways to get fresh demos:
- your own recent matches
- share codes: `csdm dl-valve <code>`
- FACEIT: `csdm dl-faceit`
- recent matches on csstats.gg

## Rendering fails right after a CS2 update

CS2 updates break HLAE from time to time. Symptom: a stream of "Error - AfxHookSource2 / Problem in ...\addresses.cpp" dialogs when CS2 starts. Fix: update HLAE in the CS Demo Manager app (**Settings → Video → HLAE**). Updating CS Demo Manager itself does *not* update HLAE. If the newest HLAE is older than the CS2 update, wait a few days for a new HLAE release. The worker stops after 2 failures in a row instead of retrying blindly. The versions Fragsmith was last tested with are in [compatibility.md](compatibility.md).

## CS2 starts but nothing renders

If CS2 sits at the demo for many minutes and no clips appear, check whether the **CS Demo Manager app** is open (also in the system tray). The app and `csdm video` use the same port to talk to CS2, so CS2 connects to the app and the render waits forever. Close the app before rendering. Fragsmith checks for this before it starts CS2, and `python main.py doctor` warns about it.

## Rendering "succeeded" but clips are missing

`csdm video` can return exit code 0 even when CS2 playback failed, and `csdm analyze` can complain about old demos ("unknown grenade model"). The worker therefore counts the output files and reports the job as `ok`, `partial` or `failed`. Check `worker.log` on the render PC.

## "None of known_players are in the demo"

The SteamID64s in `[players].known_players` don't match anyone in the demo. A common cause is a wrongly resolved vanity URL. Check your ID against an actual demo (e.g. on steamid.io), or pass `--player <steamid64>` for a single run.

To make a film of a friend, use `--player <their steamid64>` rather than adding them to `known_players`.

## Re-running `finish` gives a different soundtrack

`finish` without `--music` uses the track the Director chose in `02-edit-plan.json`. If the film was originally made with a `--music` override, pass `--music` again when re-running.

## The climax doesn't land on the drop

Drop sync picks the **strongest** drop in the track, and may skip the track's intro to get there. Tracks without a clear drop fall back to placing the climax at the end. Turn it off with `[editing].drop_sync = false`.

## Slow-mo encoding is very slow

`[effects].smooth_slowmo` uses optical-flow interpolation, which can make encoding ~5× slower on those segments. Turn it off, or use `[render].highfps_framerate = 120` so slow-mo clips are rendered with real frames instead (costs an extra render pass and ~2× disk for those clips).

## Smeared artifacts on smoke, molotovs or particles in slow-mo

That's `minterpolate` (smooth slow-mo) guessing frames. Use high-fps rendering instead, or turn off `smooth_slowmo`.

## Garbled player names in the Windows console

Fragsmith reconfigures the console to UTF-8, but older terminals can still show replacement characters for unicode names. The files and the video are not affected.

## Crosshair or killfeed missing in renders

`[render].true_view` can, in rare cases, hide the crosshair on some CS2 versions. Set it to `false` and re-render.
