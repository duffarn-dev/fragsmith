# Roadmap

What's coming in Fragsmith. It's a hobby project, so there are **no dates**: a feature ships in the first Wednesday release after it's done. Each item links to its issue, where the details and progress live. The [project board](https://github.com/users/duffarn-dev/projects/1) shows what's being worked on right now.

Have an idea? Open a [feature idea](https://github.com/duffarn-dev/fragsmith/issues/new?template=feature.yml). What has already shipped is in the [release notes](https://github.com/duffarn-dev/fragsmith/releases).

## Now

The big theme is **next-level editing**: films that look as if an editor cut them, not a tool. Most of it is done and ships in the next release: kill context tags ([#29](https://github.com/duffarn-dev/fragsmith/issues/29)), a gap and a boom on the music's drop ([#30](https://github.com/duffarn-dev/fragsmith/issues/30)), kinetic typography ([#31](https://github.com/duffarn-dev/fragsmith/issues/31), [#44](https://github.com/duffarn-dev/fragsmith/issues/44)), transitions on the beat ([#32](https://github.com/duffarn-dev/fragsmith/issues/32)) and motion blur ([#33](https://github.com/duffarn-dev/fragsmith/issues/33)). Still to come in this theme:

- Research: a camera that flies around the climax kill ([#34](https://github.com/duffarn-dev/fragsmith/issues/34))

## Next

Decided, and built after the item above:

- `finish --wait`: Fragsmith waits for the render PC itself, so you don't have to check when it's done ([#50](https://github.com/duffarn-dev/fragsmith/issues/50))

Not decided yet, and the order can change:

- A killfeed panel in the vertical film (the killfeed falls outside the 9:16 crop today)
- A "best of the week" film from several demos
- A fast low-resolution preview before the full render
- Cleaning up raw clips on the render PC in the two-PC setup
- One command to update `worker.py` on the render PC

## Later

Ideas further out.

- A helper that checks new CS2, HLAE and CS Demo Manager versions on the render PC by itself and reports back
- A small website with showcase films and tips
- A local browser UI: drop in a demo and a song, toggle effects, preview the film
