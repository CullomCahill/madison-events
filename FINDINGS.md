Session id from claude browser session I used to generate this: cse_01CBWxiDDPvBrXG7DCqfSjR1

# Five more studios: what I found and what I shipped

All five scripts match the Dragonfly shape and are drop-in for the same repo.
Same `.env`, same `calendar_service()`, same `YOGA_CALENDAR_ID`.

| Studio | File | `SOURCE_TAG` | Tier | Source |
|---|---|---|---|---|
| Perennial Yoga (Madison) | `perennial_yoga_sync.py` | `perennial-yoga-sync` | 1 | Mindbody "go" widget, Next.js server action |
| Sukha Somatics | `sukha_yoga_sync.py` | `sukha-yoga-sync` | 1 | Momence read-only host API |
| Main Street Yoga Center | `main_street_yoga_sync.py` | `main-street-yoga-sync` | 1 | WellnessLiving Explore REST API |
| Capital Fitness / Yoga Sangha | `capital_fitness_yoga_sync.py` | `capital-fitness-yoga-sync` | 2, revised | Studio's own website, server-rendered HTML (Mindbody classic is Cloudflare-blocked) |
| Yoga Co-op of Madison | `yoga_coop_sync.py` | `yoga-coop-sync` | 3 | Hardcoded from the term PDF |

Nobody needed a headless browser. Main Street looked like it would and didn't,
which is the most interesting result here.

**The window fix is in all five.** `week_window()` returns
`(max(this Monday 00:00, now), next Monday 00:00)`. `clear_window()` uses the
same clamped start, so a mid-week re-run repairs the rest of the week and
leaves already-elapsed classes alone rather than deleting them and not
rewriting them. I verified all five return byte-identical windows for the same
`now`, at five different times of week.

---

## How I verified

The sandbox I work in has no outbound network access to any of these hosts, so
I could not run the finished scripts end to end against the live sites. What I
did instead, and you should know the difference:

- **Live data, real requests.** Every endpoint below was called for real, from
  the browser, and I read the actual JSON and HTML. All record counts and field
  names in this document are from those responses, not from memory.
- **Real payloads, algorithm checked twice.** For Perennial and Main Street I
  ran the exact filtering and parsing logic against the live payloads in the
  browser first, then transliterated to Python and ran the Python against
  fixtures copied verbatim out of those payloads. Perennial's RSC parser,
  reference resolver, and 7-decimal timestamp handling all pass. Main Street's
  phantom-class filter reproduces the studio's own widget exactly.
- **Not verified: cold start.** Capital Fitness needs a session cookie, and
  Perennial does a GET-then-POST. In the browser those ran with cookies already
  present. A fresh `requests.Session()` should behave the same, since that is
  what the browser does on a first visit, but I could not prove it from here.
  Run each script once and watch the "Fetched N, kept M" line before you trust
  the cron.

Nothing here is a guess dressed up as a finding. Where I could not determine
something I say so.

---

## Perennial Yoga, Madison

**Tier 1, but the fragile one.** Read the module docstring before you touch it.

Perennial runs two separate Mindbody sites. Fitchburg is site 22526 on the old
platform. **Madison is a different site entirely, `mboStudioId` 5754596, on
Mindbody's newer platform**, and it has no classic schedule page:
`clients.mindbodyonline.com/classic/ws?studioid=5754596` returns a business
sign-in. The `/schedule---madison` page you gave me shows *workshops and
enrollments only* (a healcode widget, id `6c11543331b8`). The actual class
schedule is a separate iframe:

```
https://go.mindbodyonline.com/book/widgets/schedules/view/82543892fb7/schedule
```

That is a Next.js app. The schedule is not in the server-rendered HTML and
there is no REST endpoint. Classes load through a **Next.js server action**: a
POST back to the same URL with a `Next-Action` header and a multipart body.
Both values it needs are in the page's RSC flight payload on every load, so the
script scrapes them fresh:

```
"fetchClasses":"$h21"                          -> flight row 21
21:{"id":"<40 hex>","bound":"$@22"}            -> action id
22:["$@39"]                                    -> bound-args row
39:"<208 char encrypted blob>"                 -> the blob
```

Then POST `Next-Action: <id>`, multipart field `1` = the JSON-quoted blob,
field `0` = `["$@1",{"fromDate":...,"toDate":...}]`. The action takes an
arbitrary UTC range, so one call covers the whole week.

The response is RSC flight format, not JSON: rows are `<id>:<json>` or
`<id>:T<hex byte length>,<text>`, and the text rows contain real newlines, so
`parse_flight` walks bytes rather than splitting lines. Class objects use `$N`
references for shared values, which `resolve` expands.

**Real fields:** `id`, `name`, `description`, `startDateTime`, `endDateTime`,
`duration`, `staff[].displayLabel`, `location.{id,name}`, `cancelled`, `type`,
`roomName`, `sessionTypeId`, `capacity`, `numberRegistered`.

**Counts, live, 2026-09-07 to 2026-09-14: 32 returned, 29 kept.** Dropped: 1
cancelled, 1 `type: "Enrollment"` (a multi-week series), 1 `SOUND | VIBRATION`.

**Field-names-lie notes:**

- `isBookableOnline` is about online *booking*, not an online *class*. Do not
  use it as a virtual flag. I could not find any virtual/online flag on this
  widget at all. It is scoped to one physical location and all 32 records came
  back at `Perennial Yoga - Madison`, so the location check is what guards this.
- `startDateTime` has **seven** decimal places (`2026-09-07T14:00:00.0000000Z`).
  `fromisoformat` rejects that. `parse_utc` trims to microseconds.
- The Madison classes carry session types named `FITCHBURG | FLOW | GENTLE` and
  so on. The studio reuses Fitchburg-named session types at the Madison
  location. Do not filter on those names.

**Yoga filtering:** structured first, `type == "Class"` drops the enrollment
series. There is no usable structured discipline tag: the sound bath's session
type sits in the *same* `serviceGroupId` as the yoga flows, so the group is
worthless as a filter. On top of that it is an allowlist on the name, kept if it
contains `YOGA` or starts with `FLOW` / `GROUND`.

**Title:** `Perennial Madison - {name}`. Perennial has two locations, so even
though this widget is Madison-only, the title names it to keep the two apart if
you ever add Fitchburg.

**What will break it:** if Mindbody changes the widget internals, the failure
is loud and specific: `WidgetChanged: could not find the fetchClasses action`.
It does *not* break on an ordinary Mindbody redeploy, because the only
hardcoded value is the widget id.

---

## Sukha Somatics

**Tier 1, and the cleanest of the five.** Momence, host id 35255.

```
GET https://readonly-api.momence.com/host-plugins/host/35255/host-schedule/sessions
    ?sessionTypes[]=course-class&sessionTypes[]=fitness&sessionTypes[]=retreat
    &sessionTypes[]=special-event&sessionTypes[]=special-event-new
    &fromDate=<utc iso>&toDate=<utc iso>&pageSize=200&page=0&timeZone=America/Chicago
```

No auth, no cookies, no referer gate, and it honours an explicit `toDate`, so
one request covers the window. Response is `{payload: [...], pagination:
{page, pageSize, totalCount}}`; the script pages if a week ever exceeds one page.

**Real fields:** `id`, `sessionName`, `startsAt`, `endsAt` (UTC ISO with `Z`),
`teacher`, `location`, `inPerson`, `isCancelled`, `link`, `capacity`,
`remainingSpots`, `level`.

**Counts, live, 2026-09-07 to 2026-09-14: 8 returned, 8 in-person and
uncancelled, 6 kept** after the yoga filter.

**Field-names-lie note: `level` is not a level.** On every Sukha record it holds
the full class description ("intention: medium-energy, increase muscular
strength..."). There is no `description` field. The script uses `level` as the
description and says so. I cross-checked this across several records before
relying on it.

**Yoga filtering:** there is **no structured discipline tag**. Every Sukha
session comes back `type: "fitness"`, which is Momence's booking category, not a
subject. So this is name matching, and it is an allowlist: anything containing
"yoga", plus an editable `ALSO_YOGA` set. Over a sampled month (32 sessions, 8
distinct names) that keeps six and drops two: **All Levels TRE** (tension and
trauma release, not yoga) and **Mindful Movement for Joint Health** (a judgment
call). Both are named in a comment; add either to `ALSO_YOGA` in one line.

**Address:** the studio publishes "312 N Third St #2 Madison WI" with no zip.
I did not invent one. Google geocodes it fine.

---

## Main Street Yoga Center

**Tier 1, after a dead end.** This is the one worth reading.

The site embeds a WellnessLiving widget (`k_business=411951`, `k_skin=226777`).
The widget's own data call is:

```
GET /en-frame/Wl/Schedule/Schedule.json?...&s_data=<opaque>&csrf=<token>
```

`csrf` is scrapeable from the widget HTML. **`s_data` is not usable.** It is an
encrypted blob the widget's JavaScript builds client side. I captured two of
them for two different weeks and they share no common prefix, so it is not a
config string with a date appended, and there is no way to ask for an arbitrary
week without running their JS. I also tried plain `dt_date` / `dt_week` /
`dl_date` params (all return `status: "data-invalid"`, "Widget url is broken")
and five other `Wl/...` endpoint paths. That route is closed. It would have
meant Tier 4.

It doesn't, because WellnessLiving also runs its consumer Explore site on a
plain public REST API with no auth, no csrf, no cookies:

```
GET https://wl-explorer-be-prod.www.wellnessliving.com/api/course/schedule
    ?wLocationId=282709&wBookNowTabId=-4119511&dateMin=<utc>&dateMax=<utc>
```

`wLocationId` 282709 is Main Street's location id, confirmed via
`Wl/Location/List.json`, which also gave the street address verbatim:
`1882 E Main Street, Madison, Wisconsin 53704`.

**Real fields.** Response is `{settings, courses, holidayMessage}`. Each course
has `title`, `description`, `isVirtual`, `quickSearchTags`, `periods[]` and
`sessions[]`. Each session has `id`, `kClassPeriod`, `localDateTimeStart`,
`utcDateTimeStart`, `durationMinutes`, `isVirtual`, `isCancelled`,
`staffMembers[].fullName`, `bookingSlotsTotal`, `bookingSlotsUsed`.

### The bug in their API, and the fix

**`/api/course/schedule` projects every class weekly, including ones that
recur annually.** Main Street has two holiday classes, `Giving Thanks Flow`
(Thanksgiving) and `Recovery Day Flow` (the Friday after). The API returns them
on **every Thursday and Friday of the year**. I confirmed this on three
separate weeks: Sep 7, Sep 21, and Nov 23. The studio's own widget shows
neither.

If you shipped this endpoint straight, you would have put roughly a hundred
phantom classes a year on the calendar.

The fix is `real_occurrence()`. Each course also ships `periods[]` describing
its actual recurrence rule: `weekday` (ISO, Monday 1 through Sunday 7),
`localTimeStart`, `localDateStart`, `localDateEnd`, `repeatPeriod`. The
phantoms are the sessions whose only matching period is **open ended**
(`localDateEnd == "0000-00-00"`) **and not weekly** (`repeatPeriod != 7`).
Genuine one-off classes like `108 Sun Salutations` have a period bounded to a
single date (`2026-09-19` to `2026-09-19`) and survive.

I got this wrong twice before it was right, and both wrong versions looked
plausible:

- Requiring `repeatPeriod == 7` alone also killed `108 Sun Salutations`.
- Comparing weekdays with Python's `weekday()` / JS `getDay()` killed **every
  Sunday class**, because WellnessLiving uses ISO numbering where Sunday is 7.

**Counts, live.** Week of 2026-09-14: raw API returned **35 sessions**, the
filter kept **33**, and the per-day distribution (6/5/5/4/3/5/5) matches the
studio's own widget exactly, day by day. Week of 2026-09-07: 28 raw, 26 kept.

**Known cost:** on the real Thanksgiving week this filter will also skip the
real `Giving Thanks Flow`. One missed class a year against a hundred phantoms.
Worth it, but it is a real trade and you should know about it.

**Yoga filtering:** `quickSearchTags` would have been the structured signal, but
Main Street leaves it **empty on every course**, and `s_class_type` /
`k_class_type` are null. So it is name matching and it is an allowlist. Across
2026-09-07 to 2026-11-30 it keeps 19 course titles and drops five: Authentic
Relating Games, Beginners Circling Intro, Meditation, Monthly Circling Lab, and
Community Class: Meditation (Mantra & Pranayama). The full keep/drop list is in
the module docstring so you can see exactly what the rule does.

`isVirtual` exists on both the course and the individual session; the script
checks both, so a normally in-person class moved online for one week is skipped.

---

## Capital Fitness / Yoga Sangha

**Revised 2026-09-05, after the Mindbody route turned out to be blocked in
practice.** The Mindbody classic approach documented below tested clean from
the browser, but a first real run from a plain script hit a 403 from
`clients.mindbodyonline.com/classic/ws`: a Cloudflare "Security Check" page
carrying a `__cf_bm` challenge cookie. That's Cloudflare bot management, not a
missing-header problem -- confirmed by retrying with full browser-style
headers (`accept-language`, `referer`, `upgrade-insecure-requests`) and still
getting the same challenge page. It needs a real browser to pass and isn't
fixable from `requests` alone.

**Current script scrapes `capitalfitness.net/yoga-sangha` directly instead.**
That page carries its own weekly schedule text, server-rendered with no JS
needed and no bot protection at all -- confirmed by fetching it with a plain
GET and checking the class names showed up in the raw HTML. It's a flat
Monday-through-Sunday grid with no date attached, no instructor field, and
no live cancellation data (the page says outright: "Schedule is subject to
change. Please check Mindbody below for the most up to date schedule."). That
is a step down in freshness from a live booking API, but scraping the
studio's own page live means it updates automatically whenever they edit it
-- no manual refresh needed, unlike the Yoga Co-op script's hardcoded grid.

**Parsing:** it's a Wix page. Each day is an `<h6>` heading containing just
the day name, followed by a `<ul>` of `<li>` rows that render (after
stripping the styling spans) as `6:30am - 7:30am - Pilates Flow`. The parser
walks the page in document order via `find_all(['h6', 'li'])`, tracking the
current day as headings are hit. Verified live 2026-09-05: 38 `<li>` rows
fall inside the seven day sections (6/8/6/6/5/7/0 Mon-Sun) and all match the
time-range regex cleanly. There are 17 stray `<li>` elements elsewhere on the
page (nav menu items, a separate promo snippet) but all of them sit before
the first "Monday" heading in document order, so the day-tracking state
machine never picks them up -- confirmed by checking that none accumulate
before the first real day heading is seen.

**Yoga filtering.** The page has no discipline tag: it's one undifferentiated
list of "fitness classes" per day. But `(Group Fitness)` is a literal suffix
the studio puts on the actual non-yoga entries in that same list -- `CAPFIT
HIIT (Group Fitness)`, `RUN CLUB (Group Fitness)`, `ZUMBA (Group Fitness)`,
`BOOTCAMP (Group Fitness)`, `PUMP IT UP (Group Fitness)`, `TRX CIRCUIT (Group
Fitness)` -- so that's a real structured signal, not a guess. On top of that,
the same denylist as the old Mindbody version (`pilates`, `guided
meditation`), since Pilates Flow/Pilates Sculpt are in the same list and the
page has no separate Pilates category either. Verified live 2026-09-05: 38
rows, 7 dropped as Group Fitness, 4 dropped as Pilates, 27 kept.

**What's lost versus the Mindbody route:** no instructor names, no live
cancellations, no capacity/spots-left. The event description says plainly
that this is the studio's published grid, not a live feed, and points at the
booking page to confirm before going.

**What will break it:** a page redesign changing the day-heading tag away
from `<h6>`, or dropping the literal day names, or restructuring the
`<li>` text format. Any of those makes `parse_grid` return `[]`, and `main()`
raises `SystemExit` loudly rather than silently writing nothing (a bare "0
kept" for a studio that runs classes every day of the week is itself a strong
signal something broke).

**Note on the address:** their page also shows a second location, "West:
425 W Washington Ave", but the schedule grid isn't split by location and the
Butler St address is the one this studio's Mindbody site (1956, "Capital
Fitness- North Butler") uses, so `ADDRESS` stays the single Butler St value.

---

## Yoga Co-op of Madison

**Tier 3, as you predicted.** The Co-op publishes one PDF per term and nothing
else. No booking platform, no JSON, no HTML schedule. The grid is transcribed
into a list literal at the top of the file.

Source: `2026-SEPT-DEC-schedule.pdf`, read 2026-09-05. Nine weekly drop-in
classes and three registration series, transcribed verbatim including the
series date ranges (`Ageless Intro` runs 8/31-9/28 and 10/5-11/9, and so on).

**Maintenance: this needs a manual refresh roughly every four months.** The
Co-op runs Jan-Apr, May-Aug, and Sept-Dec terms. `VALID_THROUGH` is set to
2026-12-31 and `main()` prints a loud warning once the window passes it, so it
cannot quietly go stale. When the new PDF goes up, retype `DROP_IN` / `SERIES`
and move that date.

**Notes:**

- No filtering needed. Every class the Co-op runs is yoga.
- The Studio/Zoom classes are hybrid, not online-only, so they stay in.
- Cancellations are invisible to this script by construction. Every event
  description says so and points at the booking page.
- `INCLUDE_SERIES` is `True`, and series get a "(series)" suffix so they read
  differently at a glance. Flip it to `False` if you only want drop-ins.
- The PDF gives teachers by first name only. That is what goes on the calendar.

Local test output, four different run times: 12 classes for a Monday run of
the week of 9/7; 4 for a Thursday 2pm run of the same week (the clamp working);
11 for the week of 10/5 when the second Ageless ranges are active; 9 for the
week of 11/16 when all three series have ended.

---

## Things to watch

Ranked by how likely they are to bite you.

1. **Perennial's server action.** Most fragile thing here by a wide margin.
   Fails loudly with `WidgetChanged`, so you will know. If it goes, the options
   are a headless browser against the same widget, or asking Perennial whether
   they can turn on a classic schedule page for Madison.
2. **Main Street's phantom filter.** It depends on `periods[].repeatPeriod` and
   ISO weekday numbering. If WellnessLiving fixes their projection bug, the
   filter becomes a no-op and nothing breaks. If they change the period schema,
   you would start losing real classes silently. Worth eyeballing the
   "Fetched N, kept M" line occasionally.
3. **Capital Fitness's page layout.** Mindbody classic for site 1956 turned
   out to be Cloudflare-blocked (see the studio's section above), so the
   script scrapes `capitalfitness.net/yoga-sangha` directly instead. If that
   page's day headings stop being plain `<h6>Monday</h6>` text, `parse_grid`
   returns `[]` and `main()` raises `SystemExit` loudly rather than silently
   skipping the week. There's also no instructor or cancellation data on this
   route -- accepted as the cost of a source that isn't bot-blocked.
4. **The Yoga Co-op PDF expiring.** Handled by the warning, but it is on you.
5. **Every name-based yoga filter.** Sukha, Main Street and Perennial all fall
   back to names because none of them expose a usable discipline tag. New class
   names may need a one-line edit. Each list is documented with the exact
   keep/drop outcome I observed, so you can see what changed.

One more, easy to forget: all five write to the same calendar and each cleanup
pass deletes only its own `SOURCE_TAG`. I asserted all five tags are distinct in
testing. If you copy one of these files to add a sixth studio, change the tag
first.
