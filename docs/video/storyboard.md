> SUPERSEDED 2026-06-12: the launch video is now fully animated and built by
> docs/video/launch/build.sh + scenes/render_frames.py. This doc describes the
> older stills-based cut; kept for the shot-6 screen-recording guidance only.

# nable demo, 60-second storyboard

Seven slides in `slides/`, plus the terminal GIF (`nable-demo.gif`) and one screen
recording you capture. Narrative order, timing, and voiceover/caption below.
No em dashes, no filler. Built to be cut in Screen Studio, CapCut, or DaVinci (free).

## The cut (≈60s)

| # | Slide | Secs | On-screen / motion | Voiceover or caption |
|---|-------|------|--------------------|----------------------|
| 1 | 00-intro | 0:00–0:05 | wordmark fades in, slow 3% zoom | "Your cloud and AI bill, answered." |
| 2 | 00→terminal GIF | 0:05–0:18 | cut to `nable-demo.gif` full-frame | "One pip install. It runs on your machine, read-only." |
| 3 | 01-connect | 0:18–0:25 | slide in, cursor clicks Continue | "Connect your cloud in one step. Credentials stay in your keychain." |
| 4 | 04-trust | 0:25–0:32 | doctor checks tick in one by one | "Your data never leaves your machine. No server in the middle." |
| 5 | 05-multicloud | 0:32–0:39 | tiles stagger in | "Seventeen providers. Cloud, AI, and SaaS in one normalized view." |
| 6 | **Screen Studio clip** | 0:39–0:52 | real Claude answering a cost question | (let the product talk, no VO) |
| 7 | 03-answer | 0:52–0:57 | hold on the framed answer | "The why, not just the how much. Tied to runway." |
| 8 | 06-end | 0:57–1:00 | install command, hold | "nable. uvx --from finops-mcp finops welcome." |

Slide 02-found is the alternate climax if you do not yet have a Screen Studio
clip: drop it in place of shot 6 and the deck stands alone with zero recording.

## The one recording you capture (shot 6)

This is the payoff and the only piece I cannot make for you.
1. Open Claude Desktop with nable connected (or use a clean staged workspace).
2. Type: "what drove our AWS bill up this month, and what is it costing per customer?"
3. Let nable answer. Record in Screen Studio so it auto-zooms and smooths the cursor.
4. Trim to ~13s. Drop it into shot 6.

If you do not want to record live, use slide 03-answer (already built) as a
static stand-in and the video is 100% done from these assets.

## How to assemble

1. Import the seven PNGs and `nable-demo.gif` into Screen Studio or CapCut, in cut order.
2. Set each slide to its duration above.
3. Add a subtle Ken Burns (3–5% zoom) to each still and a 200ms cross-dissolve between cuts. That is what turns stills into the Novus-style motion.
4. Drop the Screen Studio clip into shot 6.
5. Voiceover: record the lines above (or use a clean TTS), or run them as on-screen captions if you want a no-audio cut.
6. Export 1920×1080, H.264, for Product Hunt / X / LinkedIn / the site hero.

## Re-rendering any slide

Edit the HTML in `slides/`, then:
```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless --disable-gpu --hide-scrollbars --window-size=1920,1080 \
  --force-device-scale-factor=1.5 --virtual-time-budget=2800 \
  --screenshot="slides/01-connect.png" "file://$PWD/slides/01-connect.html"
```
The terminal GIF re-renders with `vhs docs/video/nable-demo.tape`.
