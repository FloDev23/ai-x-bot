# FlexDropin Like Suggestions and Following Tracking Design

Date: 2026-09-15

Status: approved in chat by the user on 2026-09-15 (sections 1–3)

## Objective

Refocus the manual growth workflow on what grows @FlexDropin among gyms, the
B2B side of the FlexDropin marketplace:

1. Suggest posts to like instead of replies to write.
2. Suggest only gyms and fitness studios to follow.
3. Track the account's real following list, detect follow-backs, and propose
   unfollowing non-gym accounts that never followed back. Gyms are never
   proposed for unfollow.

## Approved Product Decisions

- **Every engagement action stays manual.** The user first asked for fully
  automatic likes, follows and unfollows. X's automation rules prohibit
  automated likes and automated follow-back monitoring or follower churning,
  so a violation could get @FlexDropin suspended. After being told this, the
  user chose "manual assisted": the bot suggests, Telegram opens the X URL,
  the operator acts on X and optionally records the decision locally. No
  like, follow or unfollow endpoint is ever called.
- Follow suggestions include only managed fitness facilities (segment
  `primary`), with US accounts first.
- Like sources: gyms already followed, gyms suggested today, and gym
  operators discussing operational pain. The B2C intent query
  (`fitness_intent_model`) is removed.
- Unfollow proposals: non-gym accounts followed for at least 30 days that do
  not follow back, at most 5 per Rome week.
- Reply Copilot is switched off and detached from the digest.

## External Interaction Boundary

- New X read: `GET /2/users/:id/following`, metered as `owned_read`, paged
  like the existing follower traversal.
- Existing reads reused: follower traversal, home timeline
  (`read_following_timeline`), `gym_operator_pain` recent search, discovery
  reads.
- Removed read: the `fitness_intent_model` recent search.
- `test_x_write_safety` must keep proving that the only X write path is
  `Publisher` for editorial posts.

## Section 1 — Following Tracking

### Table `x_following` (additive)

| Column | Meaning |
|---|---|
| `user_id` TEXT PK | canonical X id |
| `username` TEXT | latest known handle |
| `profile_json` TEXT | canonical growth profile (bio, location, metrics) |
| `first_seen_following_at` TEXT | first complete sync that listed the account |
| `last_seen_following_at` TEXT | latest complete sync that listed the account |
| `unfollowed_at` TEXT NULL | first complete sync where the account was missing |
| `follows_back` INTEGER | 1 if present in the latest complete follower run |
| `follows_back_checked_at` TEXT NULL | timestamp of that follower run |
| `is_gym` INTEGER | `has_managed_fitness_facility_context(profile)` |
| `gym_override` TEXT NULL | `gym`, `not_gym` or NULL |
| `unfollow_decision` TEXT NULL | `keep`, `unfollowed_manually` or NULL |
| `unfollow_decision_at` TEXT NULL | decision timestamp |
| `keep_until` TEXT NULL | `keep` suppression end (90 days) |

If an account is followed again after `unfollowed_at`, the row is reset:
`first_seen_following_at` becomes the new sync time, and `unfollowed_at` and
`unfollow_decision` are cleared.

Effective gym status: `gym_override = 'gym'` OR (`is_gym = 1` AND
`gym_override IS NOT 'not_gym'`).

### Sync

- `TwitterClient.read_following_profiles() -> FollowingProfilesRead(profiles,
  complete)` mirrors `read_followers_profiles` (1000 per page, at most 100
  pages, loop-safe pagination tokens, canonical `_profile_dict`).
- `Analytics.capture_follower_snapshot` runs the following sync right after
  the follower snapshot in the existing 23:15 job. There is no new scheduler
  job.
- **Fail-closed:** if the following read is incomplete, rows are not inserted,
  no `unfollowed_at` is written and no follow-back is recomputed. If the
  follower read is incomplete, `follows_back` stays unchanged.
- Legacy linkage: after a complete sync, a `growth_candidates` row whose
  account is now followed moves to `decision = 'followed_manually'` with
  `manual_followed_at = first_seen_following_at` (only when the decision is
  `new` or `saved`). Existing follow-back attribution and weekly analytics
  keep working.
- If the operator marked `unfollowed_manually` but the account is still listed
  on the next complete sync, the bot sends one Telegram notice ("@x risulta
  ancora seguito") and clears the decision.

### Weekly report additions (`/stats`)

Following total, gyms followed, follow-back rate for gyms versus non-gyms,
unfollows completed this week, and likes recorded by source.

## Section 2 — Like Suggestions

The digest `post` kind becomes the "Like" section. The persisted kind stays
`post` because `growth_suggestions.kind` has a CHECK constraint that cannot be
altered without a table rebuild.

### Sources and quotas (total at most 10 per Rome day)

| Source code | Quota | Origin | Eligibility |
|---|---|---|---|
| `followed_gym` | 4 | home timeline page already read by the digest | author in `x_following`, effective gym, not unfollowed; original post ≤ 72 h |
| `suggested_gym` | 3 | `latest_post` of the gym accounts suggested in the same digest | ≤ 7 days; no extra read |
| `operator_pain` | 3 | `gym_operator_pain` search | `score_growth_post` not `None` |

- Unused slots are filled by the remaining eligible posts in source order
  `followed_gym`, `operator_pain`, `suggested_gym`, then by score.
- `followed_gym` posts skip the keyword relevance gate, because being a
  followed gym is enough. They still require English, a canonical id, a
  canonical author, no noise pattern, and age ≤ 72 h.
- At most one post per author per digest.
- Author cooldown: an author whose post was suggested in the last 3 Rome days
  is skipped. Existing 30-day post-object cooldown stays.
- Payload gains `source` (one of the three codes) and `created_at`.

### Telegram card

```
Like — palestra seguita
@username · 5 h fa
Estratto: …
```
Buttons: `Apri post su X` (URL), `Like messo` (callback → decision
`liked_manually`), `Salta` (callback → `skipped`), navigation.

The suggested-gym account card gains `Apri ultimo post` (URL to
`latest_activity_id`).

## Section 3 — Follow, Unfollow, Telegram, Removals

### Follow suggestions (kind `account`, at most 5 per day)

- `_account_rows` keeps only `segment == 'primary'` and skips user ids present
  in `x_following` with `unfollowed_at IS NULL`.
- Card buttons: `Apri profilo`, `Apri ultimo post`, `Non pertinente`
  (decision `not_relevant`; suppresses the candidate for 30 days).
- `Segnala come seguito` is removed because the sync detects follows.

### Unfollow proposals (kind `reevaluate`, at most 5 per Rome week)

Eligibility, evaluated at digest build time:
- row in `x_following`, `unfollowed_at IS NULL`;
- `first_seen_following_at` ≤ now − 30 days;
- `follows_back = 0` and `follows_back_checked_at` ≥ `first_seen_following_at`
  + 30 days (at least one complete follower run after maturity);
- not an effective gym;
- `unfollow_decision IS NULL`, or `keep` with `keep_until` in the past;
- the Rome week (Monday–Sunday) has fewer than 5 `reevaluate` suggestions.

Oldest `first_seen_following_at` first. Reason code:
`no_follow_back_after_30_days`.

Card buttons: `Apri profilo`, `Unfollow fatto` (`unfollowed_manually`),
`Tieni` (`keep`, 90 days), `È una palestra` (`gym_override = 'gym'`, never
proposed again).

`get_growth_reevaluation_candidates` is replaced by this query.
`GROWTH_UNFOLLOW_REVIEW_DAYS` defaults to 30 and is validated to at least 30.

### Digest header

```
Growth giornaliero — azioni manuali su X
Palestre da seguire: N
Like: N
Unfollow proposti: N
[Seguire] [Like] [Unfollow]
```

### Allowed decisions

| Kind | Decisions |
|---|---|
| `account` | `not_relevant` |
| `post` | `liked_manually`, `skipped` |
| `reevaluate` | `unfollowed_manually`, `keep`, `marked_gym` |

All callbacks stay revision-bound and idempotent (`duplicate` on replay).

### Removals

- `main.growth_digest_cycle` no longer calls Reply Copilot; `/replies` is not
  registered; `ENABLE_REPLY_COPILOT` is ignored (logged once if true). The
  module and its table stay and can be removed later.
- `POST_QUERY_PORTFOLIO` keeps only `gym_operator_pain`;
  `GROWTH_POST_QUERY_BUDGET` maximum becomes 1.
- README and SETUP describe the new manual flow and keep the statement that the
  bot never likes, follows, unfollows, replies or sends DMs.

## Error Handling

- Following sync incomplete: no mutations, warning log, no unfollow proposals
  that depend on it.
- Home timeline read failure: the `followed_gym` quota is filled by the other
  sources; the digest still completes. This differs from today, where a failed
  post read marks the digest incomplete. The `gym_operator_pain` search keeps
  the existing fail-closed behavior.
- Unknown callback decisions or stale revisions: existing
  "Azione non disponibile o già sostituita." reply.

## Testing (TDD)

- Following sync: new row, still following, disappeared → `unfollowed_at`,
  refollow reset, incomplete read → no mutation, legacy candidate linkage.
- Gym classification and overrides.
- Like selection: quotas, fill order, one post per author, 3-day author
  cooldown, 72 h / 7 day age limits, timeline failure fallback.
- Follow suggestions: only `primary`, already-followed excluded.
- Unfollow: 30-day maturity, follower-run requirement, gym exemption, `keep`
  90 days, weekly cap of 5, `marked_gym`.
- Telegram callbacks for every new decision, replay idempotency, stale
  revision.
- End-to-end dry run: digest without Reply Copilot, zero X writes, ledger
  contains only reads.
- `test_x_write_safety` unchanged and passing.

## Out of Scope

- Any automated engagement write.
- Verifying likes through the X API.
- Deleting the Reply Copilot module or table.
- VPS deployment; this follows the existing `deploy.sh` procedure after merge.
