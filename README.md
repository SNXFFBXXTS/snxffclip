# snxffclip ✂️

Turn YouTube videos (or any video file) into vertical clips for TikTok, YouTube Shorts and Instagram Reels — free, running on your own PC.

- **Auto mode** – transcribes the video and picks the best moments for you
- Vertical 1080×1920 output (blurred background or zoom-crop)
- Big burned-in captions in 6 styles, with colours that can match the video
- **Campaign profiles** – follows a clipping campaign's rules automatically (no music, on-screen text, ready-to-paste captions with `#Ad`, posting checklist)
- Optional background music from your own folder, ducked under speech
- Audio ripping
- A web page you can use from your PC or your phone

Uses free tools underneath: [yt-dlp](https://github.com/yt-dlp/yt-dlp), [ffmpeg](https://ffmpeg.org) and [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

---

## Setup (Linux)

**Arch / EndeavourOS**
```sh
sudo pacman -S --needed git yt-dlp ffmpeg python-pip
pip install --user --break-system-packages faster-whisper
```

**Ubuntu / Debian / Mint**
```sh
sudo apt install git ffmpeg python3-pip pipx
pipx install yt-dlp
pip install --user --break-system-packages faster-whisper
```

**Get snxffclip**
```sh
git clone https://github.com/SNXFFBXXTS/snxffclip ~/clipper
cd ~/clipper
chmod +x clipper.py
```

Update later with:
```sh
cd ~/clipper && git pull
```

Optional shortcut so you can type `clipper` anywhere:
- fish: `alias --save clipper ~/clipper/clipper.py`
- bash: `echo "alias clipper=~/clipper/clipper.py" >> ~/.bashrc && source ~/.bashrc`

The first run downloads the speech model (~500 MB for `small`).
If you have an NVIDIA card without CUDA set up, it automatically falls back to the CPU.

---

## The web page

```sh
./clipper.py --ui
```
Open **http://localhost:8765**. Paste a link or pick a campaign + footage, press **Find the best clips**.
Each batch of clips has a **🎬 Review** button: watch them one by one, tap ⭐ Keep or ✕ Drop (keys K / D,
arrows to move), then *Done* keeps the starred ones and deletes the rest. The **History** tab lists every
run – finished, failed or cancelled – with how long it took, clips made, errors and the log.
Clips show up at the bottom with **Save**, **Delete** and **Copy caption** buttons.

To use it from your phone, install [Tailscale](https://tailscale.com) on the PC and the phone, then open `http://YOUR-PC-NAME:8765`.

---

## Command line

```sh
./clipper.py "https://youtu.be/XXXX" --auto                 # 5 best clips
./clipper.py "https://youtu.be/XXXX" --auto -k 10 --len 15-30
./clipper.py "https://youtu.be/XXXX" --auto --dry-run       # just show the picks
./clipper.py ~/Downloads/video.mp4 --auto --campaign my-campaign
./clipper.py "https://youtu.be/XXXX" -s 1:23 -e 2:05 --captions   # pick times yourself
./clipper.py "https://youtu.be/XXXX" --audio                # rip the audio to ~/Music/rips
```

Useful options: `-m crop` (zoom instead of blur), `--model base` (faster transcription),
`--music background-songs` (random song per clip), `--ollama llama3.1` (smarter picks, see below).

Clips go to `~/Clips/<video name>/` – or to the campaign's folder when you use a campaign.
**🔎 Bulk folder scrape:** scans *every* video in a campaign's `source/` folder, ranks all the moments
against each other and makes the best N clips overall (`./clipper.py --bulk --campaign my-campaign -k 12`,
or `-k 0` / *Auto* to decide from the amount of footage). Transcripts are saved, so after the first scan
re-scraping the same folder is quick.

**Footage with no talking** (product shots, UGC, gameplay with music): clipper notices there's no speech and
picks by movement and sound instead – short videos are used whole, longer ones get their liveliest stretch,
starting and ending on scene cuts. Videos shorter than the campaign's minimum are skipped.

**Graphics card:** ⚙️ AI → *Speed* shows whether your NVIDIA card is being used. With the NVIDIA driver
installed, *Set up GPU* adds the CUDA libraries (pip, no sudo) and clipper uses the card automatically –
falling back to the CPU if anything's wrong. `./clipper.py --gpu` shows the status and
`./clipper.py --speedtest some-video.mp4` compares CPU vs GPU on its first minute.

**Speed:** the web page's Fast / Good / Best use English-only speech models (`base.en`, `small.en`,
`medium.en`) which are faster and more accurate for English. All CPU cores are used. An NVIDIA card with
CUDA set up is several times faster again.

**Each video separately:** put a campaign's videos in its `source/` folder and press **⚡ Clip ALL new footage**
in the web page (or run `./clipper.py --batch --campaign my-campaign`). Every video that hasn't been clipped yet
gets done, with the number of clips based on its length (about one per 2½ minutes, 2–10).
`./clipper.py ~/Downloads/some-folder --batch --campaign my-campaign` moves a folder of videos into the campaign first.

Short videos still give you the number of clips you asked for – extra clips overlap but open on a different line.
Each run also writes a `clips.txt` you can edit and re-run with `--list clips.txt`.

### Ratings and results
Every clip gets ★ 1–5 (moment strength, the campaign's pay rate, sound, keywords, warnings, and what has
done well for you before) and clips are sorted best first. After posting, press 📊 on a clip to log the
link; update the views in the **📈 Results** tab every few days. Results shows totals, rough earnings,
campaigns, and what's working – and once 6 posts have views, clipper leans towards clips like your best ones.

### Posting planner
**🗓 Plan** on a batch spreads its clips (best first, 2★ and up) over the times you're free, on the
campaign's platforms that you have accounts for – a few a day per platform, hours apart, never more than
5 posts of a clip. Each row has *Caption* and *✓ Posted* (logs it in Results), and **📅 Add to calendar**
downloads the plan with a reminder before each post. Set your free times and accounts in the planner.

### Pre-flight checks
Every clip shows ⚠ warnings before you post: no sound, shorter than the campaign minimum, over 3 minutes,
caption not from the approved list, missing @tag or #ad, doesn't mention the campaign's keywords, or made
from low-res footage. Each campaign is also checked against its saved brief (music allowed when the brief
bans it, word-for-word captions off, on-screen text that's just a hashtag, missing tag/disclosure…).

### AI brain (optional)
Press **⚙️ AI** in the web page to switch on an AI that reads briefs, picks moments and writes captions:
- **Ollama** – free, runs on your PC (see below).
- **Claude** – your own API key from console.anthropic.com (pay as you go; Haiku is the cheap one).
  The key is stored only in `settings.json` next to clipper.py, which git ignores.

### Smarter picks with Ollama (optional, free, local AI)
```sh
sudo pacman -S ollama      # or see ollama.com for other distros
sudo systemctl enable --now ollama
ollama pull llama3.1
```
Then add `--ollama llama3.1`, or type `llama3.1` in the web page's AI box.
With a campaign selected it also writes a caption for each clip based on what's actually said.

---

## Caption styles

Pick a look in the web page (**Caption style**, with a live preview) or on the command line.
On the web page **✨ Auto** is the default – tap any look, or change any setting, and it switches to
your own style (shown exactly as you set it, remembered next time). *Switch back to ✨ Auto* undoes that.

| `--style` | Look |
|---|---|
| `auto` (default) | Picks the best look for each clip from its colours, brightness, busyness and how fast people talk |
| `highlight` | Big white words, the spoken word lights up, pop-in |
| `classic` | White with black outline |
| `box` | White text on a black box |
| `neon` | Glowing outline |
| `minimal` | Smaller, lower-case, near the bottom |
| `bold-pop` | Huge yellow words, 2 at a time, in the middle |

**Changing a clip afterwards:** every clip on the web page has a 🎨 button – it opens a pop-up with
that clip's current settings (or pick ✨ Auto). *Re-render this clip* redoes just that one: the card
shows the progress and flashes *✓ Restyled* when the new version is in. The old version stays
playable until then, and Cancel keeps it.
From the command line: `./clipper.py --restyle path/to/clip.mp4 --style box --cap-pos middle`.

`--match-colors` samples the clip and uses its main colour for the highlight / glow / box,
so captions fit the video's look (gold footage → gold captions).

Fine-tune anything: `--cap-font "Anton" --cap-size 100 --cap-color "#FFFFFF" --cap-highlight "#FF3D6E"
--cap-outline "#000000" --cap-pos middle --cap-caps on --cap-pop on --words 2`

**Fonts:** `./clipper.py --fonts` lists what you can use. For popular TikTok-style fonts download them
from [Google Fonts](https://fonts.google.com) (Montserrat, Anton, Bebas Neue, Poppins…) and drop the
`.ttf` files in the `fonts/` folder – they show up in the web page's font list after a restart.

A campaign can set its own look with a `"caption_style": {...}` block using the same names
(`font`, `size`, `text`, `highlight`, `outline`, `pos`, `upper`, `pop`, `words`).

---

## Campaigns

Rules for each clipping campaign live in `campaigns/<agency>/<campaign>/campaign.json`:

```
campaigns/
  some-agency/
    some-campaign/
      campaign.json   <- the rules
      source/         <- footage (videos get moved here automatically)
      clips/          <- finished clips, captions, POSTING-CHECKLIST.txt
```

**Easiest way:** press **+ New** next to the Campaign list in the web page, paste the whole campaign page
from Whop and press *Read the rules*. Clipper fills in the settings (with your local Ollama AI if you've
entered a model in the AI box, otherwise by pattern-matching), lists the videos and zips in your Downloads –
ticking the ones whose names match – and **🚀 Publish campaign** creates the campaign folder and moves the
ticked footage into it (videos inside zips are unpacked). ✏️ next to the list edits a campaign later.

If a brief has an **approved captions** list that must be copied word for word, clipper spots it and the
*Captions must be copied word for word* box is ticked – each clip then gets one approved caption exactly
as written, with nothing added.

Campaigns aren't included in this repo – make your own. Drop a `something.json` straight into
`campaigns/` and it gets filed under its `"agency"` automatically. Example:

```json
{
  "name": "Example Game Trailer",
  "agency": "Some Agency",
  "platforms": ["TikTok", "Instagram Reels"],
  "music": false,
  "min_len": 10,
  "len": "12-35",
  "mode": "blur",
  "captions": true,
  "on_screen": "Text burned on screen for the whole clip",
  "mention": "@brand",
  "required_lines": ["A line that must be in every caption"],
  "disclosure": "#Ad",
  "hashtags": ["#Game", "#Trailer"],
  "max_extra_hashtags": 3,
  "keywords": ["words", "the clip should mention"],
  "caption_rules": "Guidance for the AI caption writer (used with --ollama)",
  "example_captions": ["Caption idea one", "Caption idea two"],
  "checklist": ["Things to tick off before posting"]
}
```

**Always read each campaign's full rules yourself** and check every caption matches its clip before posting.

---

## Background music

Put your own or royalty-free tracks in `background-songs/` – they appear in the web page's music dropdown.
Commercial songs will get your clips claimed/muted; add those through the TikTok/Shorts app instead.

## Be sensible

Only clip content you're allowed to use (campaigns give you permission). Downloading from YouTube is against
YouTube's terms of service – campaigns that provide their own footage avoid that entirely.
