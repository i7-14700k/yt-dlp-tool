# Ingest design notes

These are the rules the interface is built against. They started as a study of
what makes software look machine-made, then got turned around into decisions.

## What reads as "AI-made", and what we do instead

Sources: the Impeccable "slop" ruleset, 925 Studios' write-up on AI tells,
UX Collective's "de-slopify" piece, and the general complaints about the
Tailwind/shadcn default look. The recurring tells, grouped:

| Tell | Why it reads as generated | What Ingest does |
| --- | --- | --- |
| Inter or Geist everywhere, one family, flat hierarchy | It's the median of the training data | Instrument Sans for UI text, Fragment Mono for technical readouts (codecs, rates, timecode). Size and weight carry hierarchy, not color |
| Indigo to violet gradients, neon glow, glass blur | Tailwind `indigo-500` and Dribbble defaults | Pure black and white. No gradients, no glow, no blur. Grey only as a surface step or for secondary text |
| "Second wave" defaults: cream + terracotta, black + acid green | Models swap one default for the next | No accent color at all. Selection is inversion: white fill, black text |
| Hairline borders plus soft shadows on every card | Component-library default | No borders or outlines anywhere. Grouping comes from spacing and three surface steps |
| Three equal cards in a row, cards inside cards | Grid filler | Asymmetric layout built around one job: a wide queue, a fixed inspector. No card grids |
| Huge radius on small elements | `rounded-2xl` everywhere | 4 px on small parts, 6 px on controls, 8 px on panels |
| Eyebrow labels, numbered "01 / 02" section tags, pill badges above headings | Decoration that repeats the heading | Plain sentence-case labels, only where a control needs naming |
| Icon in a rounded tile above every heading, generic line icons | Icon packs used as ornament | A small hand-drawn glyph set used only where a symbol is faster than a word (pause, retry, reveal). Everything else is text |
| Pulsing dots, bouncing buttons, hover lift | Motion as decoration | Motion only shows a state change: 120 to 180 ms, ease-out, no overshoot |
| Hero metric: giant number + tiny label | Dashboard template | Numbers are live data at reading size, tabular figures, aligned to the right edge of their row |
| Marketing copy, em-dashes, "supercharge", forced contrasts | Generated prose | Terse technical copy: what happened, what to do. "Converting to H.264, 2.4× realtime" |
| Empty states with illustrations and sparkles | Template filler | Empty states say how to use the screen: shortcuts and accepted input |
| Forms without validation or error states | Surface-level generation | Every control has disabled, hover, pressed, focus and error behaviour, and every failure carries a reason and a next step |

## Tokens

Color (all on `#000000`, OLED black):

| Token | Value | Use |
| --- | --- | --- |
| `bg` | `#000000` | Window, pages |
| `s1` | `#0B0B0B` | Inspector panel, list hover |
| `s2` | `#141414` | Controls, inputs, selected row |
| `s3` | `#1E1E1E` | Control hover, progress track |
| `s4` | `#2A2A2A` | Pressed, scrollbar thumb |
| `fg` | `#FFFFFF` | Primary text, selected fill, progress fill |
| `fg2` | `#A6A6A6` | Secondary text (8.9:1 on black) |
| `fg3` | `#7A7A7A` | Tertiary text, placeholders (4.8:1) |
| `fg4` | `#4A4A4A` | Disabled |

Type: Instrument Sans 400/500/600, Fragment Mono 400. Pixel sizes 11, 12, 13,
14, 16, 20. Tabular figures on every number that changes.

Spacing: 4 px grid. Related items 4 to 8 px apart, groups 16 to 24 px, regions 32 px.

Corners: 4 / 6 / 8 px.

## Status without color

| State | Treatment |
| --- | --- |
| Active | White text, filling white bar |
| Queued | Secondary text, empty track |
| Paused | Bar dims to 40 % white |
| Failed | Diagonal hatch on the bar, reason in white, one action button |
| Done | Bar disappears, verification line ("Pr ✓  Ae ✓") replaces it |

## Windows

Native caption kept for snap layouts and accessibility, painted black through
DWM on Windows 11 (caption color, text color, border color set to none). Taskbar
button shows aggregate progress. Finished items drag straight into Premiere's
project panel.

## Review rounds

Each round was a full set of screenshots (Linux offscreen and the frozen
Windows build on CI) checked against the table above.

1. Page headings repeated the active tab ("Queue" under the Queue tab), the
   label-repeats-heading tell. Removed. Paused and failed rows said the same
   thing twice (status text and the right column); the right column now holds
   either the number or the row's actions. A disabled HDR control with no
   explanation became one sentence about tone-mapping.
2. Scrollbars read as thin vertical rules, which is an outline by another
   name. They now stay invisible until the area is scrolled or hovered.
3. The Inspect page had empty space beside the thumbnail. It now states the
   plan in words: what was picked and whether it will be copied or converted.
4. Dropdowns used one row height for every option; rows now size to their
   content. Dialogs got a dimmed backdrop rather than a drop shadow. The
   progress percent sets its sign smaller on the same baseline.
5. At 980 × 600 (a 1366 × 768 laptop at 125 %) the setup page squeezed its
   rows together; it scrolls now, and the window opens at most 92 % of the
   screen.
6. Keyboard focus was a one-step fill change, about 1.1:1 against the
   resting state, so Tab users couldn't find it. Focused controls now get a
   2 px bar inside their bottom edge, the same mark as the active tab, black
   on white controls and white on dark ones. It shows only when focus came
   from Tab, not when Qt hands focus to the first control of a new page. The
   Inspect format tables and the folder row became reachable by keyboard.
   Two grey lines in a row in the Output panel (the HDR note, then a grey
   field label) read as one paragraph; field labels are white now.
