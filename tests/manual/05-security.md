# 05 — Watermark & Security (`/security`)

Server: default mode. Encrypted fixture password: `docist-test`.

## S1 ⚡ Diagonal watermark
`report-12p.pdf`, Watermark, text `CONFIDENTIAL`, position center,
defaults (opacity 0.15, size 48, rotation 45).
**Expect:** faint gray diagonal `CONFIDENTIAL` across every page; body
text still readable. Output name carries a random suffix, e.g.
`report-12p-watermarked-<hex>.pdf`.
- [ ] Pass

## S2 Watermark options
Same file, position header, opacity 0.5, rotation 0, font size 24.
**Expect:** darker horizontal text at the top of each page — all four
knobs visibly honored.
- [ ] Pass

## S3 Header/footer with placeholders
`report-12p.pdf`, Header/Footer: header-left `Docist QA`, footer-center
`Page {page} of {pages}`.
**Expect:** every page stamped, footer reading `Page 1 of 12` …
`Page 12 of 12`. Then submit with **all six slots empty** → error that
at least one slot is required.
- [ ] Pass

## S4 ⚡ Bates numbering
`report-12p.pdf`, Bates: prefix `ACME`, start 1, digits 6, bottom-right.
**Expect:** pages stamped `ACME000001` … `ACME000012`; the success
message reports that exact range.
- [ ] Pass

## S5 ⚡ Protect (AES-256)
`report-12p.pdf`, Protect, password `docist-test`.
**Expect:** download prompts for the password when opened; correct
password opens it. (06-api A8 verifies the AES-256 claim
programmatically.)
- [ ] Pass

## S6 ⚡ Unlock (Docist's own AES-256)
`encrypted-aes256.pdf`, Unlock, password `docist-test`.
**Expect:** downloaded PDF opens with no password prompt; content is
the 3-page SECRET DOC.
- [ ] Pass

## S7 Unlock a legacy scheme
`encrypted-rc4.pdf` (RC4-128, as older tools produce), Unlock, password
`docist-test`.
**Expect:** works the same as S6.
- [ ] Pass

## S8 Unlock error paths
a) `encrypted-aes256.pdf` with password `wrong` → 400: `Incorrect
password`.
b) Unlock plain `report-12p.pdf` → 400: `PDF is not
password-protected`.
- [ ] Pass

## S9 Empty password is blocked client-side
Protect with an empty password field.
**Expect:** the page itself refuses (`Password is required`) — no
network request needed.
- [ ] Pass

## S10 Spoofed extension is sniffed
`fake.pdf`, any operation.
**Expect:** 400: `The file does not look like a .pdf file (its content
doesn't match the extension).`
- [ ] Pass
