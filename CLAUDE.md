# snxffclip – notes for Claude Code

This is the user's own clipping tool: `clipper.py` (one file, Python + ffmpeg + yt-dlp + faster-whisper)
turns long videos into vertical short clips for paid clipping campaigns (Whop Content Rewards and
similar). The main user is on **EndeavourOS** with **kitty** and the **fish** shell – give fish-compatible
commands. Others use it on **Windows** (installed with `windows/install.ps1` into `%USERPROFILE%\clipper`,
started from the desktop *Clipper* shortcut): if you're running on Windows, give PowerShell commands,
use `py -3 clipper.py ...` instead of `./clipper.py ...`, and Downloads is `%USERPROFILE%\Downloads`. Explain things plainly; they're not a programmer by trade.

## The main job: "here's a campaign brief"

When the user pastes a campaign brief (the rules page from Whop, ClipAffiliates, Vyro, Elev8or,
Clipping.net, Clip Farm, FindClout or Zula – see `SITES` in clipper.py), do all of this without being
asked for each step:

1. **Read the brief carefully** and write `campaigns/<agency>/<campaign-name>/campaign.json`
   (lower-case, hyphenated folder names; agency = who runs it, e.g. `clipping-culture`, `clipflow`).
   Also save the brief text as `brief.txt` in the same folder. Fields (see `CAMPAIGN_FIELDS`):
   - `name`, `agency`, `platforms` (any of `TikTok`, `Instagram Reels`, `YouTube Shorts`, `X`)
   - `site` – which site it was found on: one of the ids in `SITES` (`whop`, `clipaffiliates`, `vyro`,
     `elev8or`, `clippingnet`, `clipfarm`, `findclout`, `zula`). Leave `""` if it's somewhere else.
   - `campaign_url` – the campaign's own page, where the user submits their posts
   - `rate` – pay per 1,000 views in $ (best platform). Ask the user if the brief doesn't say.
   - `source_note` – the content-folder link
   - `music` – **false** if added/background music is banned (very common)
   - `min_len` (seconds) and `len` (`"LOW-HIGH"`, e.g. `"15-45"`, LOW above min_len)
   - `mode` – `blur` (default, keeps whole frame), `crop` (zoom to fill) or `none`
   - `captions` – burn in word captions (true unless the brief says otherwise)
   - `on_screen` – text burned at the top of every clip. Only use it for things the brief wants on
     screen (e.g. a required "free trial" line, or a phrase that must appear on-screen). **Never a hashtag.**
   - `keywords` – words clips should mention (product / topic); clips that say them rank higher
   - `mention` (`@account` to tag), `disclosure` (`#Ad` etc.), `required_lines` (lines every caption needs),
     `hashtags` (max 3 extra, only if allowed)
   - `exact_captions` + `example_captions` – if the brief gives an **approved caption list to copy word
     for word**, set `exact_captions: true` and put the captions in exactly as written (with their @/#).
     Otherwise `example_captions` are style examples.
   - `caption_rules` – one or two sentences of caption guidance for the AI
   - `checklist` – the posting rules to tick off (likes visible, 30 days live, max 5 posts per clip,
     submit link within 30 minutes, paid partnership label, etc.)
   - `manual_checks` – things only a human can check, e.g. "No children using the devices",
     "Renders aren't presented as real footage"
2. **Flag the tricky rules** to the user in a short list: word-for-word captions, music bans, required
   tags/disclosure, on-screen requirements, children/faces/renders rules, platform limits, minimum views,
   low pay (< $1 per 1,000) or anything unusual. Say if the campaign looks worth doing.
3. **Move the footage in**: videos/zips the user just downloaded are in `~/Downloads`. Move the ones for
   this campaign into the campaign's `source/` folder (unzip zips; keep the zip in `zips/`). Ask if it's
   unclear which files belong to it.
4. **Check it**: `./clipper.py --check <campaign-folder-name>` – fix anything it warns about.
5. **Clip it**: `./clipper.py --bulk --campaign <name>` (best clips across the whole folder; `-k N` to set
   how many, default decides from the amount of footage). It's slow the first time (transcription);
   transcripts are cached, so re-runs are quick. The user can also press the buttons in the web page.
6. Tell the user the clips are ready and to use **🎬 Review** on the web page (http://localhost:8765).

## Running it

- Web page: `./clipper.py --ui` (port 8765). Usually started with
  `pkill -f "clipper.py --ui"; nohup ~/clipper/clipper.py --ui > ~/clipper/ui.log 2>&1 & disown`
  – unless the user has run `./clipper.py --phone-setup` (systemd user service "clipper"): then use
  `systemctl --user restart clipper` and `journalctl --user -u clipper -f` for logs.
  The page has an ⟳ Update button that does `git pull` and restarts it.
- Useful commands: `--bulk`, `--batch`, `--auto`, `--dry-run`, `--check`, `--restyle CLIP.mp4`,
  `--audio` (rip audio), `--fonts`. `./clipper.py -h` lists everything.

## Where things live

- `campaigns/` – the user's campaigns, footage and clips (**git-ignored, never commit**)
- `settings.json` – AI settings incl. an API key (**git-ignored, never commit or print the key**)
- `background-songs/`, `fonts/` – user files (git-ignored)
- `results.json` – posts the user logged (link, platform, views); used for the Results tab and to learn
  which clips get views (**git-ignored, keep it**)
- `~/.cache/clipper/` – downloads, transcripts, thumbnails, `history.json`

## Working on the code

- Keep it one file, standard library + ffmpeg/yt-dlp/faster-whisper/numpy only.
- Test changes for real (make small test videos with ffmpeg `testsrc`/`sine`), and load the web page to
  check it still works before committing.
- Commit with a clear message and push to `origin main` when a change works.
