"""Steg 5: Beat-analyse av valgt musikkspor (librosa) → beats-struktur.

Kan kjøre parallelt med rendering (steg 4) — trenger bare musikkfila.
P21: finner også bass-drop-kandidater (energibaserte) så klimaks-klippet
kan legges på droppet.
"""
from __future__ import annotations

from pathlib import Path

# P21: vinduene rundt et beat som sammenlignes for drop-deteksjon —
# klassisk drop = stille oppbygging (pre) rett før høy energi (post)
DROP_WIN_S = 2.0        # sekunder før/etter beatet
DROP_MIN_RATIO = 1.35   # post-RMS må være minst så mange ganger pre-RMS
DROP_MIN_GAP_S = 8.0    # to drops nærmere enn dette regnes som samme
DROP_MAX_CANDIDATES = 5


def _detect_drops(y, sr: int, beat_times: list[float]) -> list[dict]:
    """P21: energibaserte drop-kandidater, snappet til beats.

    Et drop-kandidat er et beat der RMS-energien i vinduet ETTER er klart
    høyere enn i vinduet FØR (oppbygging → smell), og selve etter-vinduet
    er høyt i sporets egen skala (droppet er en HØY seksjon, ikke bare
    "mindre stille"). Returnerer inntil DROP_MAX_CANDIDATES, sterkest først,
    med strength normalisert til 0-1 innen sporet.
    """
    import librosa
    import numpy as np

    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    if len(rms) < 4:
        return []
    frame_t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    median_rms = float(np.median(rms))

    scored: list[tuple[float, float, float]] = []  # (tid, score, ratio)
    for bt in beat_times:
        pre = rms[(frame_t >= bt - DROP_WIN_S) & (frame_t < bt)]
        post = rms[(frame_t >= bt) & (frame_t < bt + DROP_WIN_S)]
        if len(pre) < 8 or len(post) < 8:
            continue  # for nær start/slutt av sporet
        pre_m = float(pre.mean())
        post_m = float(post.mean())
        if post_m <= median_rms:
            continue  # droppet må være en høy seksjon i sporets egen skala
        ratio = post_m / max(pre_m, 1e-9)
        if ratio < DROP_MIN_RATIO:
            continue
        # score vekter både kontrasten (ratio) og hvor høy seksjonen er
        scored.append((float(bt), ratio * post_m / median_rms, ratio))

    # non-max suppression: behold sterkeste per DROP_MIN_GAP_S-nabolag
    scored.sort(key=lambda d: -d[1])
    kept: list[tuple[float, float, float]] = []
    for t, score, ratio in scored:
        if all(abs(t - k[0]) >= DROP_MIN_GAP_S for k in kept):
            kept.append((t, score, ratio))
        if len(kept) >= DROP_MAX_CANDIDATES:
            break
    max_score = kept[0][1] if kept else 1.0
    return [
        {
            "time_s": round(t, 3),
            "strength": round(score / max_score, 3),
            "ratio": round(ratio, 2),
        }
        for t, score, ratio in kept
    ]


def analyze_beats(track_path: str | Path) -> dict:
    import librosa  # tung import — holdes lokal
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
        # P21: drop-kandidater (sterkest først); tom liste = ingen tydelig drop
        "drops": _detect_drops(y, sr, beat_list),
    }


def shift_beats(beats: dict, offset_s: float) -> dict:
    """P21: nytt beats-dict der musikken starter offset_s inn i sporet.

    Alle tider (beats + drops) forskyves med -offset_s; det som faller før
    tidslinje-null kuttes. bpm/duration beholdes urørt.
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
    shifted["music_offset_s"] = round(
        offset_s + float(beats.get("music_offset_s", 0.0)), 3
    )
    return shifted


def nearest_beat_before(target: float, beats: dict, tolerance: float) -> float | None:
    """Nærmeste beat <= target innenfor toleransen (vi kan trimme, ikke forlenge)."""
    candidates = [t for t in beats["beat_times"] if target - tolerance <= t <= target]
    return max(candidates, default=None)
