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
Each run also writes a `clips.txt` you can edit and re-run with `--list clips.txt`.

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

Pick a look in the web page (**Caption style**, with a live preview) or on the command line:

| `--style` | Look |
|---|---|
| `highlight` (default) | Big white words, the spoken word lights up, pop-in |
| `classic` | White with black outline |
| `box` | White text on a black box |
| `neon` | Glowing outline |
| `minimal` | Smaller, lower-case, near the bottom |
| `bold-pop` | Huge yellow words, 2 at a time, in the middle |

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
