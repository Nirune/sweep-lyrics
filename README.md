# Sweep

Synced karaoke lyrics for the kitty terminal. Follows the track playing over MPRIS and sweeps a soft-edged highlight across each word as it is sung.


## Features

- Word-level karaoke wipe with a soft edge, with per-syllable timing estimated and calibrated per song
- Several lyrics sources raced in parallel: Better Lyrics, Unison, LyricsPlus, KuGou, LRCLIB, YouTube Music
- Per-word display mode, live translation, romanization
- Highlight color follows your kitty theme

## Requirements

- Python 3.10 or newer (developed on 3.14)
- kitty terminal
- An MPRIS player and `playerctl`
- External commands the app calls (auto-detected, may be incomplete):
  - `fc-match`
  - `kitty`
  - `niri`
  - `notify-send`
  - `parec`
  - `pw-record`
  - `songrec`
  - `xdotool`

## Install

    pipx install git+https://github.com/Nirune/sweep-lyrics

Optional extras (Japanese romanization, pronunciation dictionary):

    pipx inject sweep-lyrics pykakasi cmudict

## Usage

    sweeplyrics

## Files

- Config: `~/.config/sweep-lyrics/lyrics-providers.json` (provider order and enabled sources)
- Cache and state: `~/.cache/sweep-lyrics/`

## Environment variables

- `SWEEP_IMG_SMOOTH`
- `SWEEP_SMOOTH`
- `SWEEP_WINDOW`
- `SWEEP_WORD_WIPE_SOFT`

## Disclaimer

Lyrics are fetched at runtime from third-party services and are never bundled with this project. Several of those services expose unofficial or undocumented APIs, so lookups can break at any time and their terms of service may not allow this kind of use. Lyrics remain the property of their rights holders. Use at your own risk.

## Credits

The multi-provider lookup and the soft-edge wipe were inspired by [Dux Shell](https://github.com/duxlabs-dev/dux-shell) (MIT).

## License

MIT
