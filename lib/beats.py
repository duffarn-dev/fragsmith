"""Step 5: beat analysis of the chosen music track (librosa) → beats dict.

Can run in parallel with rendering (step 4); it only needs the music file.
Also finds bass-drop candidates (energy based) so the climax clip can be
placed on the drop.
"""
from __future__ import annotations

from pathlib import Path

# The windows around a beat that are compared for drop detection.
# Classic drop = quiet build-up (pre) right before high energy (post)
DROP_WIN_S = 2.0        # seconds before/after the beat
DROP_MIN_RATIO = 1.35   # post RMS must be at least this many times pre RMS
DROP_MIN_GAP_S = 8.0    # two drops closer than this count as the same one
DROP_MAX_CANDIDATES = 5
# The drop's hit is often off the beat grid: in 128 Suno tracks, a third of
# the drops hit more than 100 ms from the beat they were found on (seen:
# 0.19 s after it, so the gap and the boom came before the drop and it
# sounded like a skipping record). The hit is the biggest step in level:
# the mean over DROP_HIT_SPAN_S after minus the mean over the same span
# before, so a lone snare in the build-up doesn't count
DROP_HIT_SPAN_S = 0.15
DROP_HIT_MIN_DB = 4.0   # a smaller step: the drop swells in, keep the beat


def _drop_hit(y, sr: int, beat_t: float, period: float) -> float:
    """When the drop near a drop beat hits: the biggest step in short-window
    level within half a beat of it (further away, it's another beat's hit).
    The beat itself when no step is DROP_HIT_MIN_DB or more."""
    import librosa
    import numpy as np

    hop, frame = 64, 256
    lo = max(0, int((beat_t - period / 2 - DROP_HIT_SPAN_S) * sr))
    hi = min(len(y), int((beat_t + period / 2 + DROP_HIT_SPAN_S) * sr))
    if hi - lo < frame:
        return beat_t
    rms = librosa.feature.rms(y=y[lo:hi], frame_length=frame, hop_length=hop, center=False)[0]
    db = 20 * np.log10(rms + 1e-6)
    k = max(1, int(DROP_HIT_SPAN_S * sr / hop))
    if len(db) <= 2 * k:
        return beat_t
    c = np.concatenate([[0.0], np.cumsum(db)])
    j = np.arange(k, len(db) - k)
    step = (c[j + k] - c[j]) / k - (c[j] - c[j - k]) / k
    # frame j starts at sample j*hop: a step in the signal at sample s puts
    # the biggest difference where the after-span's frames start at s
    t = (lo + j * hop) / sr
    step[np.abs(t - beat_t) > period / 2] = -np.inf
    i = int(np.argmax(step))
    return float(t[i]) if step[i] >= DROP_HIT_MIN_DB else beat_t


def _detect_drops(y, sr: int, beat_times: list[float]) -> list[dict]:
    """Energy-based drop candidates, found on beats and then moved to the
    moment the drop hits (_drop_hit), which can be between two beats.

    A drop candidate is a beat where the RMS energy in the window AFTER is
    clearly higher than in the window BEFORE (build-up → bang), and the
    after window itself is loud on the track's own scale (the drop is a
    LOUD section, not just "less quiet"). Returns up to DROP_MAX_CANDIDATES,
    strongest first, with strength normalized to 0-1 within the track.
    """
    import librosa
    import numpy as np

    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    if len(rms) < 4:
        return []
    frame_t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    median_rms = float(np.median(rms))

    scored: list[tuple[float, float, float]] = []  # (time, score, ratio)
    for bt in beat_times:
        pre = rms[(frame_t >= bt - DROP_WIN_S) & (frame_t < bt)]
        post = rms[(frame_t >= bt) & (frame_t < bt + DROP_WIN_S)]
        if len(pre) < 8 or len(post) < 8:
            continue  # too close to the start/end of the track
        pre_m = float(pre.mean())
        post_m = float(post.mean())
        if post_m <= median_rms:
            continue  # the drop must be a loud section on the track's own scale
        ratio = post_m / max(pre_m, 1e-9)
        if ratio < DROP_MIN_RATIO:
            continue
        # score weighs both the contrast (ratio) and how loud the section is
        scored.append((float(bt), ratio * post_m / median_rms, ratio))

    # non-max suppression: keep the strongest per DROP_MIN_GAP_S neighborhood
    scored.sort(key=lambda d: -d[1])
    kept: list[tuple[float, float, float]] = []
    for t, score, ratio in scored:
        if all(abs(t - k[0]) >= DROP_MIN_GAP_S for k in kept):
            kept.append((t, score, ratio))
        if len(kept) >= DROP_MAX_CANDIDATES:
            break
    max_score = kept[0][1] if kept else 1.0
    period = float(np.median(np.diff(beat_times))) if len(beat_times) > 1 else 0.5
    return [
        {
            "time_s": round(_drop_hit(y, sr, t, period), 3),
            "strength": round(score / max_score, 3),
            "ratio": round(ratio, 2),
        }
        for t, score, ratio in kept
    ]


def analyze_beats(track_path: str | Path) -> dict:
    import librosa  # heavy import, kept local
    import numpy as np

    track_path = Path(track_path)
    if not track_path.exists():
        raise FileNotFoundError(f"Cannot find music track: {track_path}")

    y, sr = librosa.load(str(track_path), sr=22050, mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)

    beat_list = [round(float(t), 3) for t in beat_times]
    return {
        "track": str(track_path),
        "duration_seconds": round(float(len(y) / sr), 3),
        "bpm": round(float(np.atleast_1d(tempo)[0]), 2),
        "beat_times": beat_list,
        # drop candidates (strongest first); empty list = no clear drop
        "drops": _detect_drops(y, sr, beat_list),
    }


def shift_beats(beats: dict, offset_s: float) -> dict:
    """New beats dict where the music starts offset_s into the track.

    All times (beats + drops) are shifted by -offset_s; anything that falls
    before timeline zero is cut. bpm/duration are left untouched.
    """
    if offset_s <= 0:
        return beats
    shifted = dict(beats)
    shifted["beat_times"] = [
        round(t - offset_s, 3) for t in beats["beat_times"] if t >= offset_s
    ]
    shifted["drops"] = [
        {**d, "time_s": round(d["time_s"] - offset_s, 3)}
        for d in beats.get("drops", [])
        if d["time_s"] >= offset_s
    ]
    if "drop_cut_s" in beats:
        shifted["drop_cut_s"] = round(beats["drop_cut_s"] - offset_s, 3)
    shifted["music_offset_s"] = round(
        offset_s + float(beats.get("music_offset_s", 0.0)), 3
    )
    return shifted


def nearest_beat_before(target: float, beats: dict, tolerance: float) -> float | None:
    """Nearest beat <= target within the tolerance (we can trim, not extend)."""
    candidates = [t for t in beats["beat_times"] if target - tolerance <= t <= target]
    return max(candidates, default=None)

