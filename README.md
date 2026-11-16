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

## Setup (Windows)
Open **PowerShell** (Start menu → type *PowerShell*) and paste:

```powershell
irm https://raw.githubusercontent.com/SNXFFBXXTS/snxffclip/main/windows/install.ps1 | iex
```

It installs Python, Git, ffmpeg and yt-dlp (with winget), puts clipper in your user folder (`clipper`) and
adds a **Clipper** shortcut to the desktop – double-click it and the page opens. Keep the black window open
while you use it. If it says something "still isn't found", open a new PowerShell window and paste the line
again. ⟳ Update in the page works the same as on Linux. NVIDIA card: ⚙️ AI → *Set up GPU*.

## How clips are picked

Every possible clip gets a score from: a hook in the first line, loud/excited moments, punchy words,
**laughter and crowd reactions** (loud sound that isn't speech, bursting on and off like "ha-ha-ha" –
steady music doesn't count), **funny lines** ("I swear", "plot twist", "are you serious"…), **strong
opinions** ("overrated", "the worst", "hot take"…), a question answered inside the clip, and your
campaign keywords. Clips lose points for **filler** ("um", "you know", "basically") and long rambling
sentences. If people laugh right after the last line, the clip keeps the laugh. The reasons show under
each clip (😂 laughter, 🤣 funny line, 🔥 strong opinion, 🪝 hook opening, 😴 lots of filler).

## 🎯 Best for brief

Picking a campaign sets everything for you – clip length, style, music off when it's banned, captions,
and the ✨ Smart picking switches – from the campaign's settings and brief (e.g. trailers are judged on
action not words, "fast pacing" keeps pause-cutting on, podcasts in Zoom turn Follow face off). The green
line under the campaign says what it chose and why. Changed things by hand? Press **🎯 Best for brief**
to put them back.

## ✨ Smart picking switches

Under the Style buttons, **✨ Smart picking** has a tick box for each feature (remembered per browser):
😂 laugh & reaction detection · 🎤 keep the laugh after the punchline · 💬 stream chat highlights ·
🤣 funny lines & opinions (and skipping boring bits) · ✂️ cut pauses & ums · 🎙️ interview-aware endings.
Command-line equivalents: `--no-laughs --no-laugh-tail --no-chat --no-smart-words --keep-pauses
--no-interview-endings`. 🙂 Follow face appears under Style when Zoom is picked.

## Tighter clips, stream chat and variations

- **✂️ Cut pauses** (on by default, under Style): silent pauses longer than ~0.6 s and "um/uh" are cut
  out of each clip, and the captions follow the new timing. Laughs and reactions in a pause are kept.
  `--keep-pauses` turns it off.
- **💬 Stream chat:** when you paste a YouTube or Twitch stream/VOD link, clipper also grabs the chat
  replay and boosts the moments where chat exploded ("LMAO", "KEKW", 💀, "W"…). For a downloaded video,
  put its chat file next to it as `<video name>.live_chat.json` or `.rechat.json`.
- **🎲 Variations** on any clip: makes up to three new versions of the same moment – a tight *short* cut
  that ends on the payoff, a *long* one with more setup and reaction, and *look2* in the other style (on-screen
  text only when the brief asks for it). Great when a campaign only has a little footage.

## 🎬 Montage

The **🎬 Montage** button on a batch joins the best part of its top 3 clips (different moments only) into
one video, with each part's captions (plus the campaign's on-screen text, only if its brief asks for it). It's clearly an original edit – TikTok marks plain re-posts of campaign footage as "unoriginal" and
won't show them on For You. Command line: `--montage CLIP1.mp4 CLIP2.mp4 [CLIP3.mp4]`.

**➕ Montage of the funniest short bits** (tick box in Bulk folder scrape): while scraping, clipper also
collects short, complete moments (3.5–9 s, funniest first – laughs, funny lines), takes 3–5 from different
moments (max 2 per video, up to ~40 s) and joins them with soft cross-fades, saved as
`00-montage-funniest-bits.mp4` in the batch. Command line: `--bulk --bulk-montage`.

## Face tracking and trimming

**Zoom** style follows the speaker's face instead of always cutting out the middle of a wide video: it
holds steady while they talk, glides when they move and cuts straight across when the shot or speaker
changes. It needs OpenCV once:

```fish
pip install --user --break-system-packages opencv-python-headless
```

(Windows: the installer adds it.) The Speed box in **⚙️ AI** says whether it's on.

**✂️ Trim** on any clip moves its start or end in ¼-second or 1-second steps – the words either side of
each cut are shown so you can stop it chopping a sentence – then re-makes the clip in the same style.

## On your phone (📱)

Press **📱** at the top of the page to see the links that work from your phone. With
[Tailscale](https://tailscale.com) on the PC and the phone (same account) it works anywhere, not just at home.

One-time setup on the PC (Linux) – keeps clipper running in the background from boot and makes a
**secure https link**:

```fish
cd ~/clipper; ./clipper.py --phone-setup
```

(If Tailscale says *access denied*, run `sudo tailscale set --operator=$USER` and try again; if it shows
a link to turn on HTTPS, open it, switch HTTPS on, and run it again.) After that, restart clipper with
`systemctl --user restart clipper` (⟳ Update still works too).

On the phone open the **https** link in Chrome → ⋮ → **Add to Home screen** – Clipper becomes an app.
Every clip then has **↗** (sends the video straight to TikTok, Instagram or your gallery and copies the
caption) and **📲 Save** (downloads it).

## Your projects folder (📁 Projects)
The **📁 Projects** tab under the main form shows everything in one place: every campaign with how much
footage and how many clips it has, plus your *Free clips* (pasted links with no campaign), with 📂 buttons
to open each folder on the PC.

By default campaigns live in `campaigns/` next to clipper and free clips in `~/Clips`. Press
**Change folder…** to pick (or make) one main folder instead – e.g. *Documents\Clipping* – and tick
*Move my existing campaigns & clips into it* to bring everything along. Campaigns then go straight in that
folder and free clips in its *Free clips* sub-folder. **Back to default** undoes it.

## 🔎 Find work – where the campaigns are

The **🔎 Find work** tab lists the sites that pay for clips, so you always know where to look when one
runs dry:

| Site | Good for |
| --- | --- |
| **Whop – Content Rewards** | The biggest one. Most campaigns, pays per 1,000 views |
| **ClipAffiliates** | No application needed; views read straight from TikTok/YouTube/Instagram |
| **Vyro** | Bigger names and celebrity content |
| **Elev8or** | Clipping bounties, low minimum before you can withdraw |
| **Clipping.net** | Campaign based, works much like Content Rewards |
| **Clip Farm** | Runs its campaigns on Whop, so you're paid through Whop |
| **FindClout** | Steadier payout terms, smaller choice of campaigns |
| **Zula.chat** | TikTok and Instagram only – no YouTube Shorts |

Press **🔗 Open** to go to a site (that also ticks it off for the day), or **Mark as checked** if you had a
look elsewhere. Anything checked in the last 20 hours shows a green ✓, so you can see at a glance what
you've already been through today. Sites where you already run campaigns list them underneath.

Sign up to two or three. Then it's the same loop for all of them: find a campaign → copy its rules →
**+ New** in clipper → clip → post → submit the link.

## Setting up a campaign from a brief

Every campaign has a brief (the rules page on Whop, ClipAffiliates, Vyro…). Clipper needs those rules – music allowed or not,
who to tag, #ad, platforms, clip length, on-screen text – so it can make clips that get approved.
Pick whichever way suits you:

1. **Free, built in (no AI needed)** – in the page press **+ New**, paste the whole brief and press
   *Read the rules*. Clipper's own brief reader fills in the settings. Check every field (it can miss
   things), then **Publish only** – or **🚀 Publish & clip everything** to also move the footage from Downloads
   and start clipping. The green *✅ Campaign settings match the brief* line under the campaign picker
   flags anything that looks wrong.
2. **With a Claude API key** – press **⚙️ AI**, pick *Claude*, paste a key from console.anthropic.com
   (pay as you go, pennies per brief). The same **+ New** button then reads briefs much more accurately,
   and the AI also helps pick the best moments and write captions.
3. **With Ollama (free, runs on your PC)** – install Ollama, pull a model, pick *Ollama* in **⚙️ AI**.
   Slower than an API key and needs a decent PC, but costs nothing.
4. **Fully hands-off with Claude Code** – open Claude Code in the clipper folder and paste the brief.
   `CLAUDE.md` tells it exactly what to do: write the campaign, flag the tricky rules (music bans,
   required tags, deadlines, audience rules, pay), move the footage in from Downloads, check it and clip
   it. Then you just press 🎬 **Review**. Needs a Claude subscription (not an API key).

Good to know: if a brief wants **different tags on different apps** (e.g. `@thearenahq` on TikTok but
`@thearena` on Instagram) the reader fills in the per-app tag boxes, and each clip gets a *Copy · TikTok* /
*Copy · Instagram* button with the right tag. If the brief needs a **logo watermark**, use *choose a logo
image* in the campaign editor – it's saved into the campaign as `logo.png`. Clipper finds your real
Downloads folder, even when Windows has moved it into OneDrive.

After that it's the same for everyone: 🔎 **Scrape the whole folder** → 🎬 **Review** (keep/drop) →
post using **Copy caption** and the platform buttons under each clip → submit the link on Whop within
the time the brief gives.

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

**Silent b-roll + no music allowed:** bulk scrape also makes 2 *mix* clips – the sound and captions of the
best clips with sound, cutting to silent product shots from the same folder for a few seconds at a time
(never in the opening hook). `--mix N` sets how many (0 = off).

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

### Platform buttons and the link timer
Under every campaign clip there's a button for each platform the campaign pays on. Each one says when that
clip is next due to be posted there (from your 🗓 Plan times, best clips first, spaced out) – or *post now*.
After posting, tap the button: it starts a 30-minute countdown to submit the link on Whop. Tap it again to
paste the link; it turns green. Platforms you haven't ticked as your accounts show as *no account*.

### Views from your post links
In 📈 Results, **⟳ Refresh views** reads the views and likes of every logged post straight from its link
(with yt-dlp). It also does this quietly when the page opens, at most every 6 hours. TikTok works without
logging in; Instagram usually needs you logged in to Instagram in Firefox – otherwise type those views in.

### Logo watermark
If a campaign needs the brand's logo on every clip, put the logo image (PNG with a transparent background is
best) in the campaign folder and set *Logo watermark file* (e.g. `logo.png`). It goes just above the video in
blur mode – clear of captions and on-screen text – or top-right otherwise.

### Check-by-eye rules
Some rules can't be checked automatically (children using the product, renders shown as real footage, made-up
claims…). Clipper lists them from the brief under *Things to check by eye*, and in 🎬 Review you have to tick
that you've looked before ⭐ Keep works.

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

The brief reader also works out **which site the campaign came from** (Whop, ClipAffiliates, Vyro…) and
picks up the campaign's own link out of the brief, so you can get back to it to submit your posts. Both
show in the campaign editor as *Found on* and *Campaign page link*, and the **🔎 Find work** tab lists
your campaigns under the site they came from.

If a brief has an **approved captions** list that must be copied word for word, clipper spots it and the
*Captions must be copied word for word* box is ticked – each clip then gets one approved caption exactly
as written, with nothing added.

Campaigns aren't included in this repo – make your own. Drop a `something.json` straight into
`campaigns/` and it gets filed under its `"agency"` automatically. Example:

```json
{
  "name": "Example Game Trailer",
  "agency": "Some Agency",
  "site": "whop",
  "campaign_url": "https://whop.com/…  (the campaign's own page, where you submit your posts)",
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
