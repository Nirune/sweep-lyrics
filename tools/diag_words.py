#!/usr/bin/env python3
"""Why does Sweep fall back to the estimated karaoke path?

Runs the SAME functions the app uses, on the SAME metadata it sees from MPRIS,
and prints each step so you can see where the word-level path is lost.

Usage (fish) - use the pipx python so it imports the installed package:
    ~/.local/share/pipx/venvs/sweep-lyrics/bin/python tools/diag_words.py          # players matching "mpv"
    ~/.local/share/pipx/venvs/sweep-lyrics/bin/python tools/diag_words.py brave    # or another player name
    ~/.local/share/pipx/venvs/sweep-lyrics/bin/python tools/diag_words.py all      # every player

The player must have the track loaded (paused is fine). Read-only: it only
fetches/caches lyrics the same way the app does.
"""
import importlib
import sys
import time
import traceback


def hr(title=""):
    print("\n" + "=" * 64)
    if title:
        print(title)
        print("-" * 64)


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else "mpv"

    hr("1. import")
    try:
        from sweep_lyrics import app
    except Exception:
        traceback.print_exc()
        print("\n[!] cannot import sweep_lyrics.app")
        return
    print("HAVE_PROVIDERS =", app.HAVE_PROVIDERS)
    if not app.HAVE_PROVIDERS:
        try:
            importlib.import_module("sweep_lyrics.lyrics_providers")
            print("importing sweep_lyrics.lyrics_providers works on its own, so the")
            print("sys.path / import trick inside app.py is what fails.")
        except Exception:
            print("importing the provider module fails with:")
            traceback.print_exc()

    hr("2. players")
    listed = app.playerctl("-l") or ""
    players = [p.strip() for p in listed.splitlines() if p.strip()]
    print(players)
    targets = players if want == "all" else [p for p in players if want in p]
    if not targets:
        print(f"[!] no player matching {want!r}")
        return

    for p in targets:
        hr(f"3. player: {p}")
        meta = app.get_metadata(p)
        if not meta:
            print("no metadata")
            continue
        meta = app._enrich_from_plasma(meta)
        artist, title = app.resolve_track_info(meta)
        album = meta.get("album", "") or ""
        dur = meta.get("duration", 0) or 0
        print("raw meta      :", {k: meta.get(k) for k in ("title", "artist", "album", "duration")})
        print(f"app passes    : artist={artist!r} title={title!r} album={album!r} duration={dur!r}")

        hr("4. provider.fetch (what the app calls first)")
        if app.HAVE_PROVIDERS:
            t0 = time.time()
            try:
                res = app.lyrics_providers.fetch(title, artist, album, dur)
                print(f"result        : {type(res).__name__}  ({time.time() - t0:.1f}s)")
                print("word_level    :", getattr(res, "word_level", None))
                lrc = getattr(res, "lrc", "") or ""
                print("lrc chars     :", len(lrc))
                print(lrc[:260])
            except Exception:
                traceback.print_exc()
        else:
            print("skipped (HAVE_PROVIDERS is False)")

        hr("5. load_lyrics_with_words (the full loader)")
        try:
            entries, plain, lrc_dur, wt = app.load_lyrics_with_words(artist, title, album, dur)
        except Exception:
            traceback.print_exc()
            continue
        rows = sum(1 for w in wt if w) if wt else 0
        print("entries       :", len(entries))
        print("lrc_duration  :", lrc_dur)
        print("word_timings  :", "None  <-- OLD (estimated) PATH" if not wt else f"yes, {rows} rows with words")
        if wt:
            for i, e in enumerate(entries):
                if "makan" in (e[1] or "") and wt[i]:
                    print("sample row    :", e[0], e[1])
                    print("word timings  :", [tuple(w)[:2] for w in wt[i]])
                    break

    hr("done")
    print("Send this whole output back.")


if __name__ == "__main__":
    main()
