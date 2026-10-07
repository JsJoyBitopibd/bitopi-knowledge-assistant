# Bitopi Knowledge Assistant: design system

All surfaces of the assistant use one visual language: a dark green sidebar or header, a warm light-grey page, white
cards, IBM Plex type, and a source shown next to every number. The designer's export is in this folder. Each new
surface (web app, Android app, website widget) starts from the screen listed for it below and takes its values from
the tokens here. Do not eyeball colours from the PNGs.

| File | What it is |
|---|---|
| `Bitopi_Knowledge_Assistant_designs.html` | The original export: one self-contained file that shows all 5 screens. Open it in a browser |
| `Bitopi_Knowledge_Assistant_designs.pdf` | The same 5 screens as a PDF (image only) |
| `screens/*.png` | One render per screen (110 dpi), for quick viewing |
| `source/*.html` | Each screen's markup with its inline styles, unpacked from the export: the exact values for every component. Rebuild with `python scripts/unpack_design.py <export.html> docs/design/source` |

## Surface map: which screen to build from

| # | Screen | Size | Surface | Status |
|---|---|---|---|---|
| 1 | Home briefing (`01-home-briefing`) | 1440×1280 | Web app, empty conversation: greeting, data-age banner, 4 KPI cards, "Needs your attention", "Try asking" | Built (Streamlit, `src/ragbot/ui/`) |
| 2 | Conversation (`02-conversation`) | 1440×1360 | Web app, a conversation: thread on the left, **Sources** rail on the right | Built (Streamlit) |
| 3 | Mobile chat (`03-mobile-chat`) | 390×844 | Android / mobile app | Next |
| 4 | Website widget, minimised (`04-website-widget-minimised`) | 1440×900 | Company website: floating launcher with label and unread badge | Not started |
| 5 | Website widget, open (`05-website-widget-open`) | 1440×900 | Company website: 400×640 chat panel above the launcher | Not started |

Screen 5's own file only imports the website page. Its markup is in `05-website-widget-open--embedded-1.html`, and
screen 4 holds the same component, closed.

## Tokens

The accent is a parameter in the export. The default is green `#0E5E50`; the designer also offered `#1F4E79` (blue)
and `#7A2E1F` (rust). Every surface uses the default.

| Role | Value | Where |
|---|---|---|
| Ink (text) | `#18201D` | body text, headings |
| Text, secondary | `#3C4641` | table headers, action buttons, chips |
| Text, muted | `#5B645F` | dates, captions, meta lines, labels |
| Page | `#F4F5F2` | main background; also table header rows, mobile input |
| Card | `#FFFFFF` | cards, header bar, input card, secondary buttons |
| Border | `#DADDD6` | cards, pills, header rule |
| Border, strong | `#C5CBC6` | secondary buttons, input card, table total rule |
| Rule, light | `#ECEEEA` | table rows, list rows, health-ring track |
| Chip | `#EEF1EE` | scope chip in the input card |
| Accent | `#0E5E50` | primary button, send button, avatar, logo, links |
| Accent, dark | `#0A4A3F` | link hover, source badge text |
| Source badge | `#E3EFEB` | numbered badge on a source card |
| Sidebar | `#14211D` | sidebar, user bubble, mobile and widget header |
| Sidebar, raised | `#1C2C27` | Data status card |
| Sidebar, selected | `#22332D` | current item in Recent |
| Sidebar rule | `#3A4A44` | pill borders, card rule |
| Sidebar text | `#E8ECE9` / `#C9D2CE` | primary / item text |
| Sidebar label | `#9AA8A2` | section labels, subtitle |
| Status value, fresh | `#8FD3B6` | Data status times |
| Status value, stale | `#F5B97A` | "16 days old" |
| User bubble text | `#F2F5F3` | on `#14211D` |
| Warning fill / border / text | `#FDF1E3` / `#F2D2AC` / `#6E3A06` | banners, cautions under an answer |
| Watch badge text, warning mark | `#7A3E06`, `#D9822B` | "Watch" badge, stale square |
| Critical badge fill / text | `#FBE7E5` / `#8E1B12` | "Critical" badge |
| Red | `#B42318` | health ring arc, red count, unread badge |
| OK green / text | `#1D7A45` / `#155C34` | status dot, check marks, fresh dot |
| Online dot | `#4CC38A` | widget header |

As CSS custom properties (the Streamlit stylesheet `src/ragbot/ui/style.css` declares these):

```css
:root {
  --ink: #18201D; --ink-2: #3C4641; --muted: #5B645F;
  --page: #F4F5F2; --card: #FFFFFF; --line: #DADDD6; --line-strong: #C5CBC6; --rule: #ECEEEA; --chip: #EEF1EE;
  --accent: #0E5E50; --accent-dark: #0A4A3F; --badge: #E3EFEB;
  --side: #14211D; --side-raised: #1C2C27; --side-selected: #22332D; --side-rule: #3A4A44;
  --side-text: #E8ECE9; --side-text-2: #C9D2CE; --side-label: #9AA8A2; --side-fresh: #8FD3B6; --side-stale: #F5B97A;
  --warn-bg: #FDF1E3; --warn-line: #F2D2AC; --warn-text: #6E3A06; --watch-text: #7A3E06; --warn-mark: #D9822B;
  --crit-bg: #FBE7E5; --crit-text: #8E1B12; --red: #B42318; --ok: #1D7A45; --ok-text: #155C34; --online: #4CC38A;
}
```

### Type

IBM Plex Sans for everything, IBM Plex Mono for machine text: view and table names, SQL, timestamps in the Data
status card, and the `database · view · as of` meta line. Numbers in tables use `font-variant-numeric: tabular-nums`.
Plex has no Bengali, so Bangla falls back to the platform font.

| Use | Size / weight |
|---|---|
| Home greeting (h1) | 36 / 600, line-height 1.15, letter-spacing −0.01em |
| KPI number | 40–44 / 600 |
| Conversation title, section heading | 18 / 600 |
| Answer body | 15 / 400, line-height 1.65 |
| Buttons, list items, table cells | 14 / 500 (cells 400) |
| Meta, captions, card labels | 13 / 400–500 |
| Section labels (sidebar, "Sources") | 11–13 / 600, uppercase, letter-spacing 0.08em |
| Badges, chips | 12 / 600 (badges), 12 / 400 (chips) |

### Shape and spacing

- Radii: cards 10–12 px, buttons 8 px, badges 6 px, pills 16–20 px (fully round), input card 14 px, widget panel 16 px.
- Shadows: input card `0 6px 24px rgba(20,33,29,.08)`. Widget panel `0 24px 64px rgba(20,33,29,.22), 0 4px 12px rgba(20,33,29,.08)`. Launcher `0 10px 28px rgba(14,94,80,.4)`.
- Tap targets are at least 40 px high (send buttons and mobile controls 44 px).
- Desktop: sidebar 272 px, main padding 32–40 px, gap 28 px between thread items, 32 px between thread and rail.

## Components

Exact markup for each is in the named source file.

- **Sidebar** (01, 02): B logo square (36 px, accent, radius 8) with "Knowledge Assistant / Bitopi Group · Pilot"; white
  **New chat** button (44 px); **Factory** pills (All + factories; selected = filled `#E8ECE9` with dark text); **Search in**;
  **Recent** (selected row `#22332D`, others `#C9D2CE`, ellipsis); **Data status** card at the bottom (mono values,
  fresh/stale colours, a rule line, then "Open access · no sign-in").
- **Status pill** (01): white, border, 8 px green dot, "Ready · connected to …".
- **Banner** (01, 02): warning fill and border, triangle icon, a bold lead phrase, a link on the right.
- **KPI card** (01): white card, muted label "Export LCs expiring · 14 days", big number, one-line explanation, an
  underlined action link that asks the question. The first card is the **health ring**: SVG circle r=34, stroke 8,
  track `#ECEEEA`, arc in red, length = score % of 213.6, rotated −90°; red/amber/green counts below.
- **Attention row** (01): white card row with a badge (Critical / Watch), **factory** · title, a mono meta line, and an "Ask
  about this" button. The first row uses the primary (filled accent) style, the others the secondary (bordered) style.
  "Show N more" link; "N items · sorted by severity" on the right of the heading.
- **Suggestion / follow-up pills** (01, 02, 03, 05): white, border `#DADDD6`, radius 20, 40 px high, 14/400.
- **Input card** (01, 02): white, strong border, radius 14, shadow; 16 px textarea; a chip ("TAL · Data + Documents"),
  "Every answer shows its source", and a 44 px accent send button with an up arrow. Mobile and widget use a round
  pill input (radius 22, page-grey fill) beside a round send button.
- **User bubble** (02, 03): right-aligned, `#14211D`, radius `14px 14px 4px 14px`, max 560 px (300 px on mobile).
- **Assistant turn** (02): a 32 px B avatar and a column holding the **Queried** disclosure (green check, "Queried
  `rag.vw_X` · N rows"), the answer with superscript `[1]` links to the source card, the **data table** (white card,
  page-grey header row, light rules, bold total row), a warning caution, the action row (Copy, Download N rows (CSV),
  thumbs on the right), and the **Follow up** pills.
- **Source card** (02): white card, radius 12; numbered badge + "Live data · <database>"; mono view name; a definition
  list (As of, Rows, Filter); a "Show query" link. On mobile (03) a source is one tappable row with a chevron.
- **Data freshness card** (02): rows with a coloured dot (green round = fresh, amber square = stale) and a value.
  A one-line disclaimer sits under the cards.
- **Header bar** (02): white, bottom border; title 18/600 and "Scope: … · Live data + Documents"; bordered Export and Share buttons.
- **Website launcher** (04): 64 px round accent button at bottom right, a red unread badge, and a white label card
  ("Ask Bitopi Assistant / Orders, SOPs, IT policies"; "Continue your chat" once a chat exists).
- **Website widget** (05): 400×640 panel, radius 16. Dark header with logo, "Knowledge Assistant", online dot and "Online · answers cite
  sources", plus expand (opens the full app) and minimise buttons. Below it: a scope row with chips, a greeting card, suggestion pills, a pill input, and the
  footnote "Pilot · check figures against the source before acting".

## The Streamlit web app (screens 1 and 2)

Built in `src/ragbot/app.py` with `src/ragbot/ui/`. The parts come from:

- **Theme** (`.streamlit/config.toml`): page, card, text, accent, border colours, radius, the dark sidebar
  (`[theme.sidebar]`), and the fonts. The fonts are the IBM Plex files from the export, served from
  `src/ragbot/static/fonts/` (SIL Open Font License, `OFL.txt`), so staff browsers make no call to Google Fonts.
- **Stylesheet** (`src/ragbot/ui/style.css`): everything the theme cannot do (user bubble, avatar, badges, cards,
  pills, input card, source rail). It targets elements by their `key` (`.st-key-…`) wherever Streamlit allows.
- **HTML pieces** (`src/ragbot/ui/view.py`): ring, badges, source cards and freshness rows, all escaped and unit-tested.

Where the app differs from the design, and why:

- **Share** is left out: conversations are not stored, so there is nothing to link to. It needs saved conversations first.
- **Recent** lists the conversations of the current browser session. New chat moves the current one there. Nothing is kept
  across sessions, and the shared open-access user makes a log-based list mix everyone's questions.
- **Search in** is shown as information, not as checkboxes: the router decides between documents and live data, and
  nothing lets a user force one.
- The **Factory** pills narrow the user's scope in code (they can never widen it), so retrieval filters, SQL
  predicates and caches follow the pick.
- On a narrow window Streamlit stacks the columns, so the Sources rail moves under the answer. The proper phone layout is
  screen 3.
