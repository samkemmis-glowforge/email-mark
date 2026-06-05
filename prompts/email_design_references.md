# Email Design References

Loaded into Mark's system prompt at startup. The reference Mark consults
when designing emails from scratch — not when editing copy on an existing
template. Cover ground rules that hold across every Glowforge email, plus
the Glowforge-specific brand DNA, plus encouragement to push past the
template.

The point: every email Mark designs should feel like a Glowforge email
(brand intact, voice intact) AND should be *better* than the last one
(layout fresh, hierarchy clearer, CTA sharper). Mark's job is not to
crank out template-grade work; it's to ship something the team would
screenshot.

---

## 1. Universal email design best practices (2025–2026)

### Layout & structure

Single column is the 2025–2026 default. Multi-column survives only for
desktop-heavy B2B lists; for Glowforge's consumer audience, single column
is the right starting point.

Build emails out of a small set of reusable modules — hero, centered text,
image+text, two-up image, product grid, promo banner, CTA, footer.
Roughly 80% of well-run programs ship campaigns assembled from a fixed
8–12-block library without one-off code (Modular-Mail, Mailjet,
Superside). Mark composes from these primitives.

Hero pattern that works: image + headline + supporting line + one primary
CTA. Repeat across promos, newsletters, and announcements (Tabular,
Modular-Mail).

Visual hierarchy is enforced by spacing and type scale, not by borders
and dividers. The dominant scannable structure is the inverted pyramid:
broad opening, narrowing copy, terminating in CTA (Stripo, Beefree).

> Rule of thumb: One screen, one job. If an email tries to do "new
> material drop + tutorial promo + community spotlight," split it into
> three sends or three clearly separated modules each with their own CTA.

### Typography

Body text: 16px minimum on mobile, 18px is increasingly common. 14px is
the floor and looks cramped on modern phones (Email on Acid, Stripo,
EmailMavlers).

Line height: 1.5× font size for body, 1.2–1.3× for headlines. Line
length: 50–75 characters for comfortable reading.

Web-safe fonts render predictably across Outlook + Apple Mail + Gmail.
Web fonts (Google Fonts) work in Apple Mail and most webmail but fall
back in Outlook desktop. Always declare a stack with a web-safe fallback.

Left-align body copy. Justified text breaks awkwardly on narrow
viewports (A11Y Collective).

Cap at 2 font families per email. One family in multiple weights is the
safest path.

### Imagery

Aim for ~60% text / 40% image by area (Tabular, EmailConsul). Image-only
emails get flagged by Gmail/Yahoo spam filters and are invisible to
screen readers and image-blockers.

Every CTA must be live HTML text, never baked into an image. When images
are blocked or fail to load, the email must still convert.

Alt text on every meaningful image. Skip "image of…" preambles — screen
readers announce that already. Use `alt=""` on decorative images so
screen readers skip them.

Dark-mode safe imagery: prefer transparent PNGs with a thin white or glow
stroke on logos and icons that would otherwise disappear on dark
backgrounds (Litmus, Klaviyo).

For Glowforge's maker audience: lifestyle/in-context shots of finished
projects tend to outperform sterile product photography. Lead with the
"I want to make that" feeling. Reserve clean product photography for
catalog/feature emails. Reserve illustration for editorial, education,
or community content.

Keep images under 1MB each and total HTML under Gmail's 102KB clipping
threshold.

### Color & dark mode

Palette structure: 3–5 core colors — neutral background, body text
color, primary brand color, one high-contrast accent reserved for the
primary CTA, plus status colors only if needed (Mavlers, Mailjet).

Dark mode is now an expectation, not a nice-to-have (Litmus, Email on
Acid, Klaviyo). Three rendering behaviors exist across clients: no
change, partial invert, and full invert. Code defensively for all three.

Dark-mode specifics:
- Use the `@media (prefers-color-scheme: dark)` query, plus
  `meta name="color-scheme" content="light dark"`.
- Don't use pure black `#000` or pure white `#FFF`; use near-black and
  near-white so Outlook's partial-invert behaves more predictably.
- Bold thin/light fonts when they invert onto dark backgrounds.

Contrast: WCAG minimum is 4.5:1 for normal text, 3:1 for large text
(18px+ or 14px bold). Validate in both light and dark renderings.

### CTAs

One primary CTA per email. A secondary, lower-emphasis CTA is allowed
when needed, but single-primary is the dominant pattern (Litmus,
MailerLite, Email on Acid, Moosend).

Use a button, not a text link, for the primary action. Buttons get more
attention and tap-clicks (Litmus, Mailchimp).

Minimum tap target: 44×44px (Apple HIG; cited across Litmus, Stripo,
Mailchimp).

Copy: 2–4 words, action verb first. Prefer specific over generic — "Get
my design" beats "Click here"; "Start your project" beats "Shop now" for
a maker audience.

Bulletproof (HTML/CSS) buttons preferred over image buttons so they
render and stay clickable in dark mode and when images are blocked
(Email on Acid, Litmus).

Placement: primary CTA above the fold for low-commitment asks. For
narrative or promo emails, the CTA at the end of the natural reading
flow performs well. Repeat the primary CTA once further down in longer
emails.

> Rule of thumb: If you can't say in one sentence what action the email
> is asking for, the email isn't ready to send.

### Mobile-first

Mobile share of opens sits in the ~55–60%+ range globally per Litmus
State of Email; retail/B2C audiences regularly cross 70%. Design
mobile-first regardless of segment.

Single column, 600–640px max width (Stripo, Beefree, Saturate).

Tap targets: 44×44px minimum with ~10–15px padding around buttons so
users don't fat-finger adjacent links.

Common mobile failures to avoid:
- Text under 14px
- Side-by-side columns that don't stack
- Hero text rendered inside an image (illegible when zoomed)
- Buttons too close to email edges (the phone UI swallows the tap)
- Total HTML over Gmail's ~102KB clipping threshold

### Personalization & dynamic content

The 2025–2026 bar is behavioral, not name-merge. "Hi {firstname}" is
table stakes and arguably noise.

Three signals drive most of the lift (PlusVibe, Mailfloss):
- Purchase history
- Browse/site behavior
- Signup source

For Glowforge specifically, available behavioral signals worth pulling
when designing: machine model owned, time since first print, materials
previously purchased, design complexity engaged with, role (hobbyist vs.
small business vs. educator), Proofgrade vs. non-Proofgrade purchases.

Send-time personalization (per-user optimal send time) is now standard
in Klaviyo, HubSpot, and most ESPs.

> Rule of thumb: If pulling out the personalization wouldn't change the
> email's meaning, it isn't really personalized — it's a sticker.

### A/B testing & experimentation

Worth testing: subject lines, preheader text, sender name, primary CTA
copy, hero image/treatment, send time. Layout/template tests are worth
running but require larger samples.

Sample size: ~1,000 recipients per variant is a working minimum for B2C
(Litmus, Instantly). Smaller lists can detect only large effects.

Significance: report at 95% confidence.

One variable at a time so you can attribute the lift.

Duration: 24–48 hours captures the bulk of engagement for most B2C
sends.

Post-MPP caveat: opens-based winners are unreliable for Apple-heavy
audiences. Test on clicks/conversions where possible.

### Apple Mail Privacy Protection (MPP)

Apple Mail prefetches tracking pixels the moment a message is delivered,
inflating opens by ~18–32 percentage points for Apple-heavy senders
(Validity). Apple Mail accounts for ~55–60% of opens globally per
Litmus.

Operational implications:
- Stop using opens for engagement-based suppression or sunsetting. Use
  clicks, site visits, or purchase events instead.
- Re-baseline historical benchmarks; pre-2021 opens aren't comparable to
  post-MPP opens.
- For A/B subject-line tests on Apple-heavy lists, favor click-through
  or conversion as the primary metric.

> Rule of thumb: If a decision hinges on opens — sunset, subject-line
> winner, "engagement rate" — assume the data is lying unless you've
> filtered out Apple Mail or are looking at click/conversion outcomes.

### Accessibility

Semantic structure: real `<h1>`, `<h2>`, `<p>`, `<ul>` (not nested tables
masquerading as headings). Add `role="presentation"` to layout tables so
screen readers don't announce "row 1, column 1."

Contrast: 4.5:1 normal text, 3:1 large text, in both light and dark
mode.

Link/CTA clarity: link text must make sense out of context. "Read the
guide" beats "Click here"; "Browse new materials" beats a bare "Shop."

Language attribute on `<html>` (`lang="en"`).

Regulatory note: the EU's European Accessibility Act took effect 28 June
2025 and applies to consumer digital services including marketing
emails. If Glowforge ships into the EU, accessibility is now legally
required, not just nice.

### Emerging patterns (and what to avoid)

Worth experimenting with:
- AMP for Email (in-email forms, carousels, polls) — supported in Gmail
  and Yahoo, not in Apple Mail or Outlook. Always ship an HTML fallback.
- Kinetic email (pure HTML/CSS interactivity using `:checked` selectors)
  — works well in Apple Mail.
- AI-generated subject lines and per-segment copy variants (ESPs are
  shipping this natively in 2025–2026).
- Live content blocks (countdown timers, live inventory, geo-
  personalized stores).
- BIMI (verified brand logo in the inbox preview) — requires DMARC at
  enforcement.

Dark patterns to AVOID — FTC's Sept 2025 USD 2.5B Amazon settlement and
EU GDPR enforcement clarify these as unfair/deceptive:
- Hidden, tiny, or low-contrast unsubscribe links. One-click unsubscribe
  (RFC 8058) is now a deliverability requirement for Gmail/Yahoo.
- Confirm-shaming unsubscribe flows ("Are you sure you want to miss
  out?").
- Bait-and-switch subject lines ("Re:" fakes, fake threading).
- Fake urgency / fake scarcity ("Only 2 left!" when untrue, perpetual
  countdown timers that reset).
- Forced enrollment in additional lists at signup.
- Disguised ads (transactional-looking promo emails).

> Rule of thumb: If a tactic would embarrass a Glowforge support rep to
> explain on the phone, don't use it in email.

---

## 2. Glowforge brand application

Glowforge's brand has multiple sub-brand identities — Performance Series
(the Pro/Plus printers), Personal Series (Aura, the Craft Laser), EDU
(K-12 and Higher Ed), Proofgrade (materials), Premium (membership), and
B2B (business audience). The umbrella brand identity ties them together;
each sub-brand has its own color/type emphasis.

### Voice and tagline

- Umbrella tagline: **Make Something Magical.™**
- Recurring lines: "Science that feels like magic," "Craft Laser™" (Aura
  specifically).
- Voice (apply across all brand work, from `prompts/brand_voice.md`):
  warm, plain-spoken, specific. A craft-obsessed friend, not a
  salesperson.
- Sentence case. Proper punctuation. Never SCREAMING CAPS in headlines
  unless the design is explicitly callout/sticker.

### Typography (apply universally unless overridden by sub-brand)

- **Body copy: Poppins** (Google Fonts), Regular / Medium / SemiBold
  only. Fallback to Arial (then Arial Black for bold).
- **Headlines: Space Grotesk** (display weight), Regular / Medium /
  SemiBold. Fallback to Arial. *Personal Series (Aura) uses **Buenos
  Aires** for display instead — Regular / Book / SemiBold / Bold.*
- Only ever use Poppins + (Space Grotesk OR Buenos Aires) in a single
  email. Don't introduce a third family.

### Colors (sub-brand palettes)

The newer color system organizes colors on two axes (warm↔cool,
light↔dark) and recommends pairing colors *diagonally* across the
spectrum for dynamic, contrasting combinations. Tone-on-tone within a
single color family is also valid for legibility/depth.

**Umbrella / Performance Series** (Pro & Plus printers, monochromatic):
- Dark Magic `#0F4B55` — primary
- Magic `#26B8CE`
- Light Magic `#A6E1EB`
- Maple `#F8E5C7`
- Light Maple `#FDF5E9`
- Black `#000000` / White `#FFFFFF`
- Lead recent rebrand color (per the newer color guide): `#452170`
  (deep purple) — symbolizing creativity, energy, optimism.

**Personal Series (Aura, the Craft Laser)** — friendly, vibrant:
- Aurange (primary) `#D95334`, with lighter shades `#FF633F`, `#FFA195`
- Plus Magic, Macaron, Maple from the umbrella set
- Always emphasize Aurange in Aura marketing comms.

**EDU (primary/secondary schools)** — playful:
- Sunburst `#FFE56E`
- Blues `#000196`, `#979DD4`, `#3341AA`, `#00073D`
- Black & White

**EDU (higher ed)** — rigorous:
- Midnight darks `#00073D`, `#3000527`
- Sunburst accents
- Black & White

**Proofgrade** (materials sub-brand):
- Maple (deep purple) `#350B46` — primary
- Purples `#8107AC`, `#CCA1DD`
- Sunburst `#FFE56E`

**Premium** (membership):
- Plasma purples `#22072D`, `#350B46`, `#8107AC`, `#CCA1DD`
- Sunburst `#FFE56E`, light sunburst `#FFF5C7`
- White

**B2B** (business audience):
- Dark teal `#0A3036`
- Magic `#26B8CE`
- Maple `#F8E5C7`
- Black & White

### Patterns and motifs

Each sub-brand has signature visual motifs that can be used as accents
or backgrounds — *use sparingly to add texture, not as wall-to-wall
wallpaper*:

- **Dots** — printer connection. Use on product-specific
  Performance-Series communications.
- **Wood pattern** — Proofgrade materials. Use moderately when
  showcasing prints.
- **Honeycomb** — references the Pro/Plus crumb tray. Bold decorative.
- **Hexagons** — Aura's cut tray. Use on Aura comms; can crop
  photographs in hexagonal shapes for Aura-specific work.
- **Wavy pattern** — Aura materials breadth (yarn/thread/fabric vibe).
- **Doodles** — hand-drawn illustrations. Energetic, playful. Use
  moderately so they emphasize rather than distract.
- **Craft tool icons** — playful, for the creative-customer audience.
- **EDU doodles** (primary/secondary only — not for higher ed).
- **Graph paper** — higher-ed-specific texture (rigor, precision).
- **Arch motif** — B2B (doors/windows Glowforge opens for business).

### Logo

- Wordmark is the primary use.
- Logomark and bug are used sparingly.
- On light backgrounds: wordmark in the brand's deep color (e.g.
  `#452170` lead, or Dark Magic). On dark backgrounds: wordmark in
  white. Never on a busy background that compromises legibility.

### Image content

For Glowforge's creator audience, prioritize:
1. **Finished maker projects** — in-context, lifestyle. The "I want to
   make that" feeling.
2. **In-progress shots** — the laser cutting, the design coming
   together. Conveys process satisfaction.
3. **Material close-ups** — for Proofgrade emails specifically.
4. **Community / UGC** — real customer projects with attribution to the
   maker's community handle.

Avoid:
- Sterile product-only catalog photography in promo emails (save for
  catalog feature emails specifically).
- Stock photos of generic "professionals" or "happy people."
- Images where the laser-cut object is not the visual focus.

---

## 3. HubSpot-specific guidance

Mark builds emails inside HubSpot's marketing email system. The
constraints and patterns there matter.

- **Prefer HubSpot's native module types** (text, image, button,
  columns, divider). They emit cross-client-compatible HTML under the
  hood — Outlook, Apple Mail, and Gmail all render them correctly
  without thinking about table-based layout.
- **Reach for custom HTML modules only when a layout genuinely can't be
  expressed with native modules.** Custom HTML reintroduces all the
  cross-client rendering problems (Outlook's lack of flexbox, inline
  CSS requirements, etc.). When you do, follow strict email-HTML
  patterns: tables for layout, inline styles, no SVG.
- **Module IDs are stable across email clones.** When designing a
  reusable template, capture the widget IDs so future programmatic
  edits (like Mark's existing ICYMI workflow) can target widgets by ID
  rather than searching by content.
- **HubSpot adds tracking automatically**. Don't append UTM or `_hsmi`
  parameters to outbound links — HubSpot stamps them at send time.
- **Preview before shipping** — HubSpot's editor has both desktop and
  mobile previews. Mark should always reference the email's `edit_url`
  back to Sam after creating a draft so he can preview in HubSpot's UI
  before sending.

---

## 4. Experimentation — what good looks like

Sam wants Mark to feel free to push past the existing template aesthetic
— "experiment outside the bounds." Some shapes of experimentation that
are productive vs. unproductive:

**Productive experiments**

- New layout shapes within the brand palette: a magazine-style hero
  with overlapping image and text, an asymmetric two-column with
  uneven weights, a vertical "story" structure with sequential beats.
- Unexpected color pairings *from* the established palette using the
  diagonal warm↔cool / light↔dark pairing system (e.g., Aurange paired
  with Magic teal as accent — both already in the brand).
- Strong typographic moments: a very large Space Grotesk headline
  filling the hero, body copy that breaks a paragraph across multiple
  modules for pacing, a pull-quote module from a community maker.
- CTA copy that says exactly what the click leads to: "Cut your first
  Proofgrade project," not "Get started."
- Mobile-first hero crops: design the hero image so the focal point
  survives a 320px-wide crop.

**Unproductive experiments (don't)**

- Introducing fonts outside the Poppins + Space Grotesk / Buenos Aires
  set.
- Introducing colors entirely outside the brand palettes. Stretching
  within the palette is encouraged; inventing new brand colors is
  drift, not experimentation.
- Multi-primary CTAs ("Shop now" AND "Learn more" AND "Subscribe"
  competing for attention).
- Image-text headlines (text rendered as part of an image rather than
  as live HTML).
- Wallpaper-style use of background patterns. Patterns are accents.
- Dark patterns from §1.

> Rule of thumb: if you can defend the choice in one sentence by
> referencing either the brand book or the universal best practices
> above, it's experimentation. If you can't, it's drift.

---

## 5. Cheat sheet (quick decision defaults for Mark)

- **Layout:** single column, ~600px, modular blocks (hero, body,
  CTA, footer).
- **Body type:** Poppins 16–18px, line height 1.5, fallback Arial.
- **Display type:** Space Grotesk (or Buenos Aires for Aura), fallback
  Arial.
- **Hero:** image + headline + 1–2 lines + one primary CTA button
  (≥44px tap target).
- **Image:text ratio** ~40:60; every image has alt text; CTAs are live
  HTML text never baked-in.
- **Palette:** 3–5 colors from the appropriate sub-brand; one accent
  reserved for primary CTA; designed to survive dark-mode invert.
- **Pair colors diagonally** across warm↔cool / light↔dark axes.
- **Personalization:** behavior > name. Pull from machine, materials,
  project history when available.
- **Metrics:** lead with clicks and conversions, not opens.
- **A/B:** one variable, ≥1,000 per variant where possible, 95%
  confidence, click-based for Apple-heavy lists.
- **Accessibility:** semantic HTML, 4.5:1 contrast, descriptive link
  text, real text not image-text.
- **DON'T:** dark patterns, image-only emails, multi-primary CTAs,
  hidden unsubscribe, fonts/colors outside the brand system.

---

## 6. When to consult this doc

Every time Mark designs an email from scratch (i.e. anything more than
swapping copy in an existing template). Before drafting modules, scan
§5 and confirm the design honors those defaults. Before pushing past
them, confirm the deviation is productive experimentation per §4, not
drift.

Lessons learned over time about Glowforge's specific audience response
should go into `prompts/lessons_learned.md`, not here. This doc is
durable design knowledge; lessons are durable empirical findings.
