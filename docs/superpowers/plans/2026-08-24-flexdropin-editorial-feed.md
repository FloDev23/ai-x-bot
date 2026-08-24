# FlexDropin Editorial Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a strict, versioned, English-only JSON feed of canonical FlexDropin blog articles at `https://flexdropin.com/api/editorial-feed`.

**Architecture:** A pure TypeScript builder validates and projects `BLOG_POSTS` into a small public schema. A Next.js route delegates entirely to that builder and adds bounded public-cache headers; no rendered HTML scraping or Italian duplication is involved.

**Tech Stack:** Next.js 16 App Router, TypeScript 5, Node test runner through `tsx`, existing `BLOG_POSTS` data.

## Global Constraints

- Work in `/Users/floriano/flo_mobile_app/flexDropin-website` on `main` only after verifying a clean worktree.
- The production origin is exactly `https://flexdropin.com`.
- The endpoint is exactly `/api/editorial-feed`.
- The top-level schema has exactly `version`, `language`, and `items`.
- Each item has exactly `slug`, `url`, `title`, `summary`, and `published_at`.
- Emit English canonical records only; do not emit Italian text, full content blocks, SEO keyword arrays, media paths, or secrets.
- Emit at most 100 items, newest first, and reject the build boundary on malformed or duplicate records.
- Use test-driven development and commit each completed task in the website repository.

---

### Task 1: Pure editorial-feed projection and validation

**Files:**
- Create: `data/editorial-feed.ts`
- Create: `tests/editorialFeed.test.ts`
- Read: `data/blog.ts`

**Interfaces:**
- Consumes: `BlogPost` and `getAllBlogPosts()` from `data/blog.ts`.
- Produces: `EditorialFeed`, `EditorialFeedItem`, and `buildEditorialFeed(posts: BlogPost[], today?: Date): EditorialFeed`.

- [ ] **Step 1: Write the failing canonical projection test**

```ts
import assert from 'node:assert/strict'
import test from 'node:test'

import { BLOG_POSTS } from '../data/blog'
import { buildEditorialFeed } from '../data/editorial-feed'

test('projects only canonical English editorial fields newest first', () => {
  const feed = buildEditorialFeed(BLOG_POSTS, new Date('2026-08-24T12:00:00Z'))

  assert.equal(feed.version, 1)
  assert.equal(feed.language, 'en')
  assert.equal(feed.items.length, BLOG_POSTS.length)
  assert.deepEqual(Object.keys(feed).sort(), ['items', 'language', 'version'])
  assert.deepEqual(
    Object.keys(feed.items[0]).sort(),
    ['published_at', 'slug', 'summary', 'title', 'url'],
  )
  assert.deepEqual(
    feed.items.map((item) => item.published_at),
    [...feed.items.map((item) => item.published_at)].sort().reverse(),
  )
  for (const item of feed.items) {
    assert.equal(item.url, `https://flexdropin.com/blog/${item.slug}`)
    assert.ok(!JSON.stringify(item).includes('slugIt'))
    assert.ok(!JSON.stringify(item).includes('excerptIt'))
    assert.ok(!JSON.stringify(item).includes('blocks'))
  }
})
```

- [ ] **Step 2: Run the canonical test to verify RED**

Run: `npx tsx --test tests/editorialFeed.test.ts`

Expected: FAIL with `Cannot find module '../data/editorial-feed'`.

- [ ] **Step 3: Add the malformed-record matrix before implementation**

Add tests that clone one valid `BlogPost` and prove `buildEditorialFeed` throws for:

```ts
const mutations = [
  (post: BlogPost) => ({ ...post, slugEn: 'Upper Case' }),
  (post: BlogPost) => ({ ...post, slugEn: '../escape' }),
  (post: BlogPost) => ({ ...post, date: '2026-02-30' }),
  (post: BlogPost) => ({ ...post, date: '2026-08-25' }),
  (post: BlogPost) => ({ ...post, content: { ...post.content, titleEn: ' ' } }),
  (post: BlogPost) => ({ ...post, content: { ...post.content, excerptEn: ' ' } }),
  (post: BlogPost) => ({ ...post, content: { ...post.content, titleEn: 'x'.repeat(201) } }),
  (post: BlogPost) => ({ ...post, content: { ...post.content, excerptEn: 'x'.repeat(1001) } }),
]
```

Also assert a runtime non-object post, missing `content`, duplicate `slugEn`,
duplicate canonical URLs, 101 records, an invalid `Date`, and a valid record
dated exactly `2026-08-24`.

- [ ] **Step 4: Run the expanded test to verify RED**

Run: `npx tsx --test tests/editorialFeed.test.ts`

Expected: FAIL because the builder and exported types do not exist.

- [ ] **Step 5: Implement the minimal pure builder**

Create `data/editorial-feed.ts` with these public shapes and validation constants:

```ts
import type { BlogPost } from './blog'

export type EditorialFeedItem = {
  slug: string
  url: string
  title: string
  summary: string
  published_at: string
}

export type EditorialFeed = {
  version: 1
  language: 'en'
  items: EditorialFeedItem[]
}

const ORIGIN = 'https://flexdropin.com'
const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export function buildEditorialFeed(
  posts: BlogPost[],
  today = new Date(),
): EditorialFeed {
  if (!Array.isArray(posts) || posts.length > 100 || Number.isNaN(today.getTime())) {
    throw new Error('invalid_editorial_feed_input')
  }
  const todayIso = today.toISOString().slice(0, 10)
  const seen = new Set<string>()
  const items = posts.map((post): EditorialFeedItem => {
    if (
      post === null
      || typeof post !== 'object'
      || post.content === null
      || typeof post.content !== 'object'
    ) {
      throw new Error('invalid_editorial_feed_item')
    }
    const slug = post.slugEn
    const title = post.content.titleEn
    const summary = post.content.excerptEn
    const publishedAt = post.date
    const parsedDate = new Date(`${publishedAt}T00:00:00Z`)
    if (
      !SLUG.test(slug)
      || !ISO_DATE.test(publishedAt)
      || Number.isNaN(parsedDate.getTime())
      || parsedDate.toISOString().slice(0, 10) !== publishedAt
      || publishedAt > todayIso
      || typeof title !== 'string'
      || title !== title.trim()
      || title.length < 1
      || title.length > 200
      || typeof summary !== 'string'
      || summary !== summary.trim()
      || summary.length < 1
      || summary.length > 1000
      || seen.has(slug)
    ) {
      throw new Error('invalid_editorial_feed_item')
    }
    seen.add(slug)
    return {
      slug,
      url: `${ORIGIN}/blog/${slug}`,
      title,
      summary,
      published_at: publishedAt,
    }
  })
  items.sort((left, right) =>
    right.published_at.localeCompare(left.published_at)
    || left.slug.localeCompare(right.slug),
  )
  return { version: 1, language: 'en', items }
}
```

Use one `Set` for both slug and URL uniqueness if the final implementation ever accepts a configurable origin; with the fixed origin, slug uniqueness proves URL uniqueness.

- [ ] **Step 6: Run the feed tests to verify GREEN**

Run: `npx tsx --test tests/editorialFeed.test.ts`

Expected: all editorial-feed tests PASS.

- [ ] **Step 7: Run existing blog regression tests**

Run: `npx tsx --test tests/blogInfrastructure.test.ts tests/blogContent.test.ts`

Expected: PASS with existing blog ordering, localized pages, and cover checks unchanged.

- [ ] **Step 8: Commit the pure builder**

```bash
git add data/editorial-feed.ts tests/editorialFeed.test.ts
git commit -m "feat: build canonical editorial feed"
```

### Task 2: Public Next.js feed route

**Files:**
- Create: `app/api/editorial-feed/route.ts`
- Modify: `tests/editorialFeed.test.ts`

**Interfaces:**
- Consumes: `buildEditorialFeed(getAllBlogPosts())`.
- Produces: `GET(): Response` for `/api/editorial-feed`.

- [ ] **Step 1: Write the failing route contract test**

```ts
import { GET } from '../app/api/editorial-feed/route'

test('serves the exact feed with bounded public caching', async () => {
  const response = await GET()
  const body = await response.json()

  assert.equal(response.status, 200)
  assert.match(response.headers.get('content-type') ?? '', /^application\/json/)
  assert.equal(
    response.headers.get('cache-control'),
    'public, s-maxage=3600, stale-while-revalidate=86400',
  )
  assert.deepEqual(body, buildEditorialFeed(BLOG_POSTS))
})
```

- [ ] **Step 2: Run the route test to verify RED**

Run: `npx tsx --test tests/editorialFeed.test.ts`

Expected: FAIL with missing `app/api/editorial-feed/route.ts`.

- [ ] **Step 3: Implement the route without request-dependent input**

```ts
import { NextResponse } from 'next/server'

import { getAllBlogPosts } from '@/data/blog'
import { buildEditorialFeed } from '@/data/editorial-feed'

export const dynamic = 'force-static'

export async function GET() {
  return NextResponse.json(buildEditorialFeed(getAllBlogPosts()), {
    headers: {
      'Cache-Control': 'public, s-maxage=3600, stale-while-revalidate=86400',
    },
  })
}
```

Do not read query parameters, request headers, cookies, locale, or environment variables in this route.

- [ ] **Step 4: Run focused and full website tests**

Run: `npx tsx --test tests/editorialFeed.test.ts`

Expected: PASS.

Run: `npm test`

Expected: all website tests PASS.

- [ ] **Step 5: Build the production website**

Run: `npm run build`

Expected: Next.js build exits 0 and lists `/api/editorial-feed` as a static route.

- [ ] **Step 6: Check diff and commit**

Run: `git diff --check`

Expected: no output.

```bash
git add app/api/editorial-feed/route.ts tests/editorialFeed.test.ts
git commit -m "feat: expose editorial feed endpoint"
```

- [ ] **Step 7: Record the website SHA for the bot deployment gate**

Run: `git rev-parse HEAD`

Expected: one 40-character SHA. Save it in the implementation report; do not deploy yet.
