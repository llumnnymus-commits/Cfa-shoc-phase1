# Sales Playbook — CFA-SHOC (zero-spend routes)

Start this only after step 13 (green CI, public repo, package on PyPI). Selling a library that
doesn't import would burn the launch. Every listing, post or price going public is an owner
question first (one question, recommended default included).

Platform fees and rules change. Check each platform's current terms on the day of listing and
note what you checked in DECISIONS.md.

## What is being sold
**The pitch, one line:** "A control layer for AI agents. Every tool call is checked, approved,
leased, verified and logged in a tamper-evident chain, and comes with a signed certificate."

**Who pays:** small teams and solo builders shipping agents that send email, move money, edit
files, write to databases, or act on a customer's behalf, and who need to show what the agent
did and why.

**Honesty rule:** say it *produces audit evidence*. Never claim it makes anyone "compliant"
with any law or standard.

## The offer ladder (open core)
| # | Offer | Price idea (owner decides) | Where it's sold, free to list |
|---|---|---|---|
| 0 | The library, MIT | free | GitHub + PyPI |
| 1 | Sponsorship | any | GitHub Sponsors (`.github/FUNDING.yml`); Ko-fi as a backup |
| 2 | Policy Packs: ready-made contracts and tool schemas for risky tools (email send, payments, file delete, DB write, GitHub push, browser actions), each with tests | 29–79 per pack | Gumroad or Lemon Squeezy (both free to list; they take a per-sale cut) |
| 3 | "Agent Safety Setup" service: wire CFA-SHOC into a client's agent, register their tools, write their policies, hand over the audit export | 300 starter / 1,000 standard / custom | Fiverr gig, Upwork profile, Contra (free to list; they take a commission) |
| 4 | Provenance review: certificates for a client's agent runs plus a written review | per report | Same as 3 |
| 5 | Later, only after real users ask: hosted audit viewer (upload certificates, see the chain) | monthly | Built on a free cloud tier; nothing hosted on the owner's devices |

## What Claude Code builds (in the repo, after step 13)
1. `.github/FUNDING.yml` pointing at the owner's GitHub Sponsors (and Ko-fi if set up).
2. `policy_packs/` with one free sample pack in the public repo (email-send), plus the paid
   packs built and tested in a private folder that is **not** committed to the public repo.
   Give the owner the paid packs as individual files for upload to Gumroad. No zips.
3. A GitHub Pages landing page (`docs/site/index.html`). Black and white, with one neon or
   metallic accent for emphasis. Include the demo output, the pitch line, links to Sponsors,
   the packs and the service gig.
4. A social preview image (1280×640, same palette). The owner sets it in repo settings from the
   phone.
5. Launch drafts as individual `.md` files in `marketing/`:
   - "Show HN: CFA-SHOC – a transaction layer for AI agent tool calls" (plain, technical,
     links to the demo)
   - dev.to article: "Why your agent's tool calls need a state machine" (walk through the
     13 states with the demo)
   - LinkedIn post (short, one diagram)
   - Reddit posts. Read each subreddit's self-promotion rules first and note them in the draft.
   - Fiverr gig text, Upwork profile text, Gumroad descriptions for each pack
6. `examples/` for the integrations people search for (one plain-Python agent loop, one
   function-calling loop). Each is runnable in CI.

## What the owner does (phone)
1. Turn on GitHub Sponsors (github.com/sponsors) and complete the profile. Eligibility and payout
   rules depend on country. Check during sign-up.
2. Create the Gumroad (or Lemon Squeezy) account and upload each pack file Claude Code sends.
   Paste the descriptions from `marketing/`.
3. Create the Fiverr gig and Upwork profile from the drafts.
4. Post the launch drafts. Claude Code gives the posting order and timing: Show HN first, then
   dev.to, then LinkedIn and Reddit spread over a few days.
5. Answer comments with Claude's help. Collect every question asked and turn it into README
   FAQ entries, which Claude Code commits.

## Signals to watch (record weekly in PROJECT_STATE_VERIFIED.md)
GitHub stars and issues, PyPI downloads (pypistats.org), Sponsors count, pack sales, gig
inquiries. If service inquiries come before pack sales, put more effort into offer 3. If pack
sales come first, build more packs.
