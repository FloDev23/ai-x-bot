# FlexDropin — human-first editorial plan

Status: approved and scheduled  
Period: September 25–October 31, 2026  
Language: English  
Timezone: America/New_York  
Primary audience: US gym owners and operators  
Secondary audience: people interested in training and flexible fitness access  

## Final distribution

- 94 scheduled posts across 37 days;
- 58 human-first thoughts with no forced product reference;
- 20 previously approved product and US gym-owner posts, redistributed across October;
- 5 previously approved Product Hunt posts retained around launch (launch moved from September 30 to October 7, 2026);
- 11 new research, blog, education, or founder posts;
- 17 days with two posts and 20 days with three posts;
- 37 consecutive 09:15 ET human-first openings;
- 20 additional midday human-first thoughts;
- one additional human-first evening on October 27.

Human-first content represents 61.7% of the calendar. The feed therefore leads with recognisable thoughts about training, routine, classes, recovery, travel, and gym culture. Product content is present, but it no longer defines the account's daily voice.

## Publishing rules

- Every day begins at 09:15 ET with a first-person human thought.
- Human posts can be complete observations; they do not need a question, CTA, image, hashtag, or link.
- Questions appear only when they naturally complete the thought.
- No fabricated workout, location, class attendance, mood, or real-time claim.
- Research keeps the represented year and avoids unsupported causal claims.
- Flexible access is complementary to membership, not a promised replacement.
- Direct links are spaced so no rolling seven-day period contains more than three linked root posts.
- Existing image posts retain their approved media binding.
- Redundant Product Hunt drafts remain approved but are no longer scheduled; they are not deleted.

## Daily cadence

| Date | 09:15 ET | Midday ET | 18:30 ET |
|---|---|---|---|
| Sep 25 | Human thought | Human thought, 12:35 | Product Hunt research thread |
| Sep 26 | Human thought | Human thought, 13:20 | Independent-gym research thread |
| Sep 27 | Human thought | Human thought, 12:50 | Blog: pricing a drop-in |
| Sep 28 | Human thought | Human thought, 13:35 | Product Hunt pre-launch thread |
| Sep 29 | Human thought | Product Hunt date change, 12:25 | Product journey: class list |
| Sep 30 | Human thought | Human thought, 13:05 | Research: 44,983 US locations |
| Oct 1 | Human thought | Human thought, 12:50 | Product journey: Explore |
| Oct 2 | Human thought | Human thought, 13:15 | US gym owner: empty spots |
| Oct 3 | Human thought | — | Product journey: gym detail |
| Oct 4 | Human thought | Human thought, 12:40 | US gym owner: manual DMs |
| Oct 5 | Human thought | — | Blog: controlled drop-in test |
| Oct 6 | Human thought | Human thought, 14:05 | Product Hunt launches tomorrow |
| Oct 7 | Human thought | — | Product Hunt launch day |
| Oct 8 | Human thought | Human thought, 13:25 | US gym owner: software vs channel |
| Oct 9 | Human thought | — | Blog: tourists, students, and travellers |
| Oct 10 | Human thought | Human thought, 12:30 | Product journey: class detail |
| Oct 11 | Human thought | — | US gym owner: partner pricing |
| Oct 12 | Human thought | Human thought, 13:45 | Product journey: payment |
| Oct 13 | Human thought | — | Research: multi-membership behaviour |
| Oct 14 | Human thought | Human thought, 12:20 | US gym owner: Stripe payouts |
| Oct 15 | Human thought | — | Research: annual retention |
| Oct 16 | Human thought | Human thought, 14:10 | Product journey: bookings |
| Oct 17 | Human thought | — | Blog: working out while travelling |
| Oct 18 | Human thought | Human thought, 13:10 | US gym owner: recurring classes |
| Oct 19 | Human thought | — | Research: 31.0% total penetration |
| Oct 20 | Human thought | Human thought, 12:45 | Product journey: widget |
| Oct 21 | Human thought | — | Blog: entry, day pass, open gym, drop-in |
| Oct 22 | Human thought | Human thought, 13:30 | US gym owner: operating metrics |
| Oct 23 | Human thought | — | Founder journey: Floriano and travel |
| Oct 24 | Human thought | Human thought, 12:25 | Product journey: four-step flow |
| Oct 25 | Human thought | — | Blog: membership vs drop-in |
| Oct 26 | Human thought | Human thought, 13:55 | US gym owner: onboarding |
| Oct 27 | Human thought | — | Human month-review thought |
| Oct 28 | Human thought | Human thought, 12:15 | Product journey: booking follow-through |
| Oct 29 | Human thought | — | US gym-owner controlled-test thread |
| Oct 30 | Human thought | — | Product-journey thread |
| Oct 31 | Human thought | — | US gym-owner manual-workflow thread |

## Product Hunt selection

The launch window originally contained nine planned campaign posts. Five remain scheduled because they provide distinct value and preserve launch timing:

- research thread on September 25;
- independent-gym thread on September 26;
- pre-launch thread on September 28;
- launch-eve post on October 6;
- launch-day post on October 7.

Four link-heavy or overlapping posts are removed from the publication plan but remain approved in the draft archive: travel data, small-gym staffing data, Maria's founder story, and product proof. Their strongest ideas are already covered elsewhere without creating a six-day promotional run.

## Product Hunt date change

On September 29, 2026 the Product Hunt launch moved from September 30 to October 7. The September 28 pre-launch thread had already been published with the old date and was left untouched. `scripts/update_product_hunt_launch_oct7.py` then:

- swapped the launch-eve post (Sep 29 → Oct 6) with the class-list product post (Oct 6 → Sep 29);
- swapped the launch-day post (Sep 30 → Oct 7) with the 44,983-locations research post (Oct 7 → Sep 30);
- swapped the September 30 and October 7 morning thoughts, so the "excitement and nerves" post runs on launch day;
- replaced the September 30 midday launch thought with a neutral human thought;
- changed the October 30 product thread's closing line to "launched on Product Hunt on October 7".

`scripts/announce_product_hunt_date_change.py` added a sixth Product Hunt post, a date-change announcement, in the September 29 12:25 slot. The human thought it displaced moved to a new October 1 midday slot at 12:50, so the calendar now holds 95 posts.

## Content ownership and verification

The exact approved English copy, schedule construction, safety checks, and idempotent database update live in `scripts/reschedule_human_first_campaign.py`.

Before a production update, the script verifies:

- all 37 dates have a morning and evening assignment;
- exactly 94 unique drafts are scheduled;
- every day has two or three posts;
- exactly 20 days have three posts;
- every new root post fits X's 280-character limit;
- linked root posts remain below the rolling weekly cap;
- existing drafts are still approved and unpublished;
- all scheduled media are present and reserved by the correct draft;
- database integrity remains `ok`.

## Source library

- FlexDropin Partner: https://flexdropin.com/partner
- FlexDropin Research: https://flexdropin.com/research
- FlexDropin Press: https://flexdropin.com/press
- FlexDropin: https://flexdropin.com/
- Product Hunt launch: https://www.producthunt.com/products/flexdropin?launch=flexdropin
