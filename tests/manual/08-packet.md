---
type: repo-doc
project: docist
description: "Manual hand-test checklist 08 — twelve checks of the Packet Builder page at /packet: mouse drag, touch drag from the handle and long-press selection on iOS and Android, keyboard-only arranging and build, a screen reader pass on moves, selection and the preview, card states, drafts, and a 40-file drop against the rate limit."
tags: [checklist, testing, packet-builder, accessibility]
updated: 2026-10-03
---

# 08 — Packet Builder

The page is `/packet`. No nav link points to it until the packet round's
[B4]; type the URL.

**Two servers.** Until the packet endpoints ([B1]) land, run the dev fake,
which serves the shapes in `tests/fixtures/packet-api.json` from memory and
converts with the real converters:

```bash
DOCIST_LIMIT_ANON=100000 .venv/bin/python tests/manual/packet_fake_server.py
# → http://127.0.0.1:5011/packet
```

The fake keeps nothing across a restart (every card then reads "Expired"),
and its build is a plain assembly without numbers or bookmarks. File names
drive its refusals: a name containing `fail` is refused, `quota` gets the
staging-quota refusal, `full` gets "Docist is busy", `busy` waits once,
`slow` takes three seconds. Before [B2] lands, run every check again against
the real server (`./run.sh` in default mode, see README) with B1 merged.

```bash
FX=tests/manual/fixtures      # make_fixtures.py writes these
```

Use a mix: two or three PDFs from `$FX` (one with 5+ pages), a `.docx`, a
`.md` and a photo. For the touch checks, open the page from a phone on the
same network (start the server with `DOCIST_HOST=0.0.0.0`, or the fake with
the same host change) — `crypto.subtle` needs HTTPS or localhost, so on a
phone use a tunnel with HTTPS; on plain HTTP the page says it needs a
secure connection.

## K1 ⚡ Drop, convert, arrange with the mouse
Drop five mixed files on the empty page.
**Expect:** a card per file at once, each "Uploading and converting…" in
turn (one at a time), then its thumbnail, type badge, page count and page
range. Ready cards can be dragged while others still convert. Drag a card by
its header to a new place: an insertion gap shows, the order changes, the
outline's page ranges follow. Expand a card (chevron) and drag a page tile
into another card's pages: that card splits around it, and the tile keeps
its source label ("scan.pdf p2"). A click with a small wobble still opens
the preview rather than dragging.
- [ ] Pass

## K2 ⚡ Preview pop-up
Click a card's name, then a page tile.
**Expect:** a dialog with the page large and "Page 7 of 42 · contract.pdf
p3". Left and Right step through the whole packet across files; Rotate
left/right turn the page (the tile turns too), Delete removes it with an
"Undo" toast, Move to… opens the position dialog. Esc closes and focus goes
back to what opened it. Clicking the expand chevron, the … menu, the ✕ or
the checkbox never opens the preview. A card still converting opens to its
status instead of a page.
- [ ] Pass

## K3 Multi-select and bulk actions
Ctrl/Cmd-click three tiles in two files, then Shift-click a fourth.
**Expect:** the range fills in; the bulk bar shows "N pages selected" with
Rotate left, Rotate right, Delete, Move to position…, Duplicate, Done.
Dragging one selected tile moves them all, in their packet order, with a
count badge on the dragged tile. Ctrl/Cmd-A selects every tile in view, Esc
clears. Rotate, then Delete: the toast offers Undo for about 8 seconds.
- [ ] Pass

## K4 ⚡ Keyboard only, through to the download
Unplug the mouse (or don't touch it). Tab to "Add files", add files, then
arrange using only the keyboard.
**Expect:** every card and tile has a … Move menu (Move to start, Earlier,
Later, Move to end, Move to position…), operated with arrows and Enter. On a
drag handle, Space picks the item up (dashed outline), arrows move it,
Space drops, Esc puts it back. Arrow keys move focus between tiles in the
grid. After every move, focus stays on the moved item, which flashes and
scrolls into view. Ctrl/Cmd-Z undoes, Ctrl/Cmd-Shift-Z or Ctrl-Y redoes.
Tab to Build packet, press Enter, then Download: the PDF opens with the
pages in the arranged order and rotations.
- [ ] Pass

## K5 Screen reader pass
With NVDA (Windows, Firefox or Chrome) and VoiceOver (macOS Safari):
**Expect:** a card reads like "Preview resume.docx, 2 pages, file 1 of 4";
a tile like "Preview page 3 of contract.pdf, packet page 7". Each move is
announced once ("Moved page 3 of contract.pdf to position 5 of 12."). In
grab mode, announcements are prompt and not doubled. Selecting through a
tile's checkbox announces the count. In NVDA browse mode, Space on a tile
does not select it (selection is the checkbox). The preview announces its
position on each step. Status changes ("resume.docx is ready.") are read.
- [ ] Pass

## K6 ⚡ Touch drag from the handle (iOS Safari, Android Chrome)
**Expect:** the page scrolls normally when you swipe over cards. A short
press on a card's or tile's grip (⋮⋮) then drag moves it; a swipe anywhere
else scrolls. The "+" buttons and … menus are visible without hover. Below
640 px wide the layout is one column and expanded pages sit two to a row.
- [ ] Pass on iOS
- [ ] Pass on Android

## K7 Long-press selection mode (iOS Safari, Android Chrome)
Long-press a tile (about half a second, away from its grip).
**Expect:** no iOS image callout; selection mode starts with that tile
selected, checkboxes show and the bulk bar sits at the bottom. A tap now
toggles a tile instead of previewing. "Done", or unticking the last one,
leaves selection mode, and a tap previews again. The preview is full
screen and a sideways swipe steps between pages.
- [ ] Pass on iOS
- [ ] Pass on Android

## K8 Card states
Drop `fail.md` (fake) or a corrupt PDF, an encrypted PDF, and a file of an
unsupported type (e.g. `.zip`).
**Expect:** each card says why ("Couldn't convert this file: …", "This PDF
needs a password…", "This file type isn't supported.") and offers Remove;
Retry where the file can be sent again. Other cards are unaffected and
Build waits ("1 file not ready yet."). Over your staging limit (`quota`
name on the fake) the card says how much this packet holds and how much is
held elsewhere, with Remove and Retry.
- [ ] Pass

## K9 Drafts and expiry
Arrange a packet, then reload the tab.
**Expect:** the arrangement returns. Open `/packet` in a new tab: it offers
"Restore your last packet?" and Restore brings the same cards back ready.
Restart the server (fake) or wait past the staging TTL (real): cards read
"Expired, re-add this file" and keep their places; Re-add with the same
file restores it in place with its page edits; Re-add with a different
file adds a new card after it and says so.
- [ ] Pass

## K10 Paper change
With a `.docx` in the packet, switch Paper from Letter to A4.
**Expect:** before switching, the options panel says how many files will
convert again and how many have page edits that may reset. After, the
`.docx` card converts again ("Converting again for A4…"); PDFs don't.
Undo switches back and the card converts for Letter.

Then, mid-conversion: drop a non-PDF that converts slowly (with the dev fake
server, a name containing `slow`, such as `slow-race.txt`) and switch the
paper while its card still says "Uploading and converting…".
**Expect:** when that request finishes, the card converts again for the new
paper on its own, then reads ready. It is never left on "Converting again
for …" with nothing in flight, and Build enables once it is ready.
- [ ] Pass

## K11 Operations left and the build refusals
In accounts mode (README), as a guest with 3 operations a day:
**Expect:** the footer shows "N operations left today"; it drops by one per
build, and arranging never uses one. At 0 the empty page says so with
Sign in and sign-up links, and Build is disabled with the reason, while the
arrangement stays. Sign in in another tab, then build here: the page says
"Reload the page. Your arrangement is saved."
- [ ] Pass

## K12 40 files against the rate limit
In accounts mode (10 POSTs a minute), drop 40 small PDFs at once.
**Expect:** the cards fill in one at a time; when the limit answers 429 the
waiting cards say "Waiting…" and carry on by themselves after the
Retry-After, with no card failing. The page stays usable while it works.
- [ ] Pass
