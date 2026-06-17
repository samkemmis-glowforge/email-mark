# Social Playbook

Platform formats, caption structure, cadence, and the review workflow for
Glowforge organic social. This sits on top of `brand_voice.md` — the brand
voice rules always apply; this file adds the social-specific layer.

## Cadence (from the content calendar)

- Glowforge posts to social **twice a week: Monday and Friday**.
  (Wednesday/Saturday rows in the calendar are Email / ICYMI — those belong
  to email-mark, not social-mark.)
- Posts are themed around the week's holidays, seasons, and key dates
  ("Key Dates This Week" in the calendar). Lean into the timely hook.
- The calendar row is the brief: **Theme**, **Hook / Caption Angle**,
  **Audience Focus**, **Product Focus**, and the **Drive asset link(s)**.

## Caption structure

- **Lead with the maker / the creativity**, then the project, then Glowforge.
  Talk about what someone can make and why it's exciting — not the machine's
  specs.
- Keep it tight: 1–3 short sentences for a standard post. Social is the
  slightly-more-casual, fun Glowforge voice — energetic, never geeky or salesy.
- **One clear idea per post.** If there's a CTA, make it soft and singular
  ("Show us your first spring project 🌱"), not a hard sell.
- **Hashtags:** use the small approved set only, and only when they fit —
  `#laserthursday` `#whatmadethis` `#glowforge`. Max 1–2 per post. Don't
  invent new hashtags or make hashtag jokes.
- **Emoji:** sparingly — 1–2 max, only when they add energy.

## Platform notes (Meta)

- **Instagram** is image/video-first: the asset carries the post, the caption
  supports it. A strong first line matters (it shows before "more").
- **Facebook** tolerates slightly longer captions and links, but keep the same
  tight, lead-with-creativity shape.
- When a row doesn't specify platform, assume **Facebook + Instagram** and
  write one caption that works for both; call out if you'd tailor per platform.
- Every post needs an asset. If the calendar row has no Drive link, flag that
  in the draft so a human can attach one before posting.

## Words to avoid (carried from brand voice — easy to slip on social)

- No "fire / fires / burns / scorch" for the laser. Use print, make, create,
  engrave, cut.
- Not "users" — prefer "owners", "makers" (sparingly), or "you".
- Not "the Glowforge" — it's "your Glowforge" (the printer) or "Glowforge"
  (the company). It's a "3D laser printer", made **on** a Glowforge.
- Avoid puns (we punish coworkers with those instead), clichés, and
  superlatives ("best", "amazing", "very").

## Review workflow (v1 — draft only)

1. Pull what's due with `get_upcoming_social_posts`.
2. Draft the caption(s) and show them **in Slack chat first** with the date,
   platform(s), theme, and the asset link. Offer 2–3 options when useful.
3. Iterate on the user's feedback.
4. On approval, call `post_draft_to_review_channel` to hand the finalized
   draft to the review channel. A human posts it. **social-mark never
   publishes** — don't claim a post went live.
