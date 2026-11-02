# Circle (Meetcircle / Aura) API protocol

Reference for the HTTP APIs behind the Circle parental-controls service, as
exercised by the official Circle Android app (2.34.0.1, late 2025), and a
coverage map against what this MCP server implements today.

Everything here was derived by studying the app's behavior and re-stated in
our own words. Hosts, paths, parameter names, and JSON field names are
interoperability facts and are reproduced exactly; nothing else is.

The service has **two distinct API surfaces** with different credentials:

| Surface | Who calls it | Credential | Documented in |
|---------|--------------|------------|---------------|
| **Parent / management API** | The parent's phone, the web dashboard, this MCP | Aura login → admin token | Part B |
| **Child-device API** | The Circle app running in Kid mode on a managed phone/tablet ("Circle Go") | Device enrollment → device token (`gotoken`) | Part C |

Both surfaces share the same cloud host (`vc.meetcircle-blue.co`); the parent
API lives on port 443 and the child API on port 8995.

Status markers used in the tables:

| Marker | Meaning |
|--------|---------|
| ✅ | Implemented in `circle_mcp/provider.py` |
| ❌ | Not implemented yet |
| ⚠️ | Implemented in the MCP but **not** seen in the app (works live, origin unknown) |
| 🔒 | Write / destructive. Needs an explicit decision before exposing as a tool |
| 📱 | Needs a child-device credential (Part C), not the parent admin token |
| 🏠 | Only reachable on the local network / on the device itself, not via the cloud |

---

# Part A. Shared

## A.1 Hosts

The app picks a host set per environment (`prod`, `stage`, `qa`, `dev`).
Production values:

| Role | Host | Port | Used for |
|------|------|------|----------|
| `aura` | `api.aurasvc.io` | 443 | Aura identity: sign-in, refresh, sign-out |
| `auth` | `auth.meetcircle-blue.co` | 443 | Parent: Aura JWT → admin token. Child: device token refresh |
| `vc` | `vc.meetcircle-blue.co` | 443 | **Parent admin API**; also child enrollment |
| `gomi` | `vc.meetcircle-blue.co` | **8995** | **Child-device API** (same host, different port) |
| `register` | `register.meetcircle-blue.co` | 443 | Per-circle VC host lookup |
| `account` | `accounts.meetcircle-blue.co` | 443 | Subscription / account lifecycle |
| `location` | `location.meetcircle-blue.co` | 443 | Child location requests and reports |
| `push` | `push.meetcircle-blue.co` | 443 | Push-notification registration |
| `urldb` | `urldb.meetcircle-blue.co` | 443 | Domain → category lookup |
| `download` | `download.meetcircle-blue.co` | 443 | Firmware / platform DB downloads |
| `appinfo` | `appinfo.meetcircle-blue.co` | 443 | Minimum app version check |
| `platform_builder`, `history_buff` | `platform-builder…`, `history-buff…` | 443 | Internal tooling (app-signature building, log listing) |
| `redirect_cert` | `redirect-cert.meetcircle-blue.co` | 443 | CA certificate used by the block page |
| `mycircle` | `dashboard.meetcircle-blue.com` | 443 | Web dashboard (not an API) |
| `cognito`, `cognito_v2` | `signin…`, `subscribe…` | 443 | Legacy (pre-Aura) login |

Non-prod hosts follow `<role>.<env>.test.meetcircle-blue.co`; the Aura test
API is `api.test.aurasvc.io`. Everything is HTTPS.

**Per-circle VC host override.** Both the parent flow (B.1.4) and the child
enrollment (C.1) can return a `host` value. When present it replaces the
default `vc` host for all later calls on that surface. The MCP does the parent
lookup after each admin grant (B.1.4).

## A.2 Request style

The legacy API (paths of the form `/api/<VERB>/…` in capitals, and
`/api/v0.1/…`) is **GET with query parameters**, even for mutations. Nested
targets use dotted parameter names: `user.pid=<pid>`,
`hardware.cuuid=<id>`, `device.uid=<mac>`.

The newer `/api/v2/…` endpoints are RESTful JSON: `POST` creates, `PUT`
updates, `DELETE` removes, ids go in the path.

## A.3 Response envelope

A legacy call is considered successful when the HTTP status is 200 **or**
the body's `result` field is `true`, `success`, or `OK` (case-insensitive).
Failures carry a human-readable string in `error`. The admin grant uses `ok`
instead of `result` and nests its error under `error.message`. Child-device
check-ins report a `status` string instead (C.3).

The app also fabricates local `403` responses (body `result` = `fail` plus
an `error` string) when its own request circuit breaker has tripped or the
child device has been unmanaged, so treat `403` as "retry later / re-auth".

## A.4 Values

- `day` is **days ago** (`0` = today, `1` = yesterday); `ndays` counts days
  backward from there.
- Legacy `days` parameters are weekday indices concatenated with no
  separator, Sunday = `0` … Saturday = `6` (`12345` = weekdays).
- v2 JSON bodies use full English day names: `"Monday"`, `"Tuesday"`, …
- Times are `HH:mm`, 24-hour.
- Profile ids (`pid`) are strings. `0` is the built-in Home profile and `1`
  the Unmanaged profile.
- Device ids (`uid`) are MAC addresses.

## A.5 User-Agent

Any value is accepted. The app sends
`Circle-Android/<ver> <build> | Admin|Kid | <make> | <model> | Android <os> | Phone|Chromebook|Kindle`,
with `Admin` or `Kid` reflecting which surface it is acting as.

---

# Part B. Parent / management API

## B.1 Authentication

### B.1.1 Aura sign-in ✅

```
POST https://api.aurasvc.io/auth/session/signin
Authorization: Public <aura_public_key>
Content-Type: application/json

{"alias": "<email>", "password": "<password>"}
```

Response: `{"access_token": "<jwt>", "refresh_token": "<token>"}`.

The public key is baked into the app: `circle_mobile_auth:prod_cb763f`
(production) or `circle_mobile_auth:test_cfba89` (test). The MCP reads it
from `CIRCLE_AURA_PUBLIC_KEY`.

Errors (HTTP 401): `{"message":"NoCredential"}` when the `Public` header is
missing or wrong; `{"message":"InvalidCredentials"}` when the key is fine but
the login is not.

### B.1.2 Aura refresh ✅ and sign-out ❌

```
POST /auth/session/refresh_token   {"refresh_token": "<token>"}   → new token pair
POST /auth/session/signout         {"refresh_token": "<token>"}   → ends the session
```

Same `Public` header on both.

### B.1.3 Admin grant ✅

```
POST https://auth.meetcircle-blue.co/api/v2/grant/admin
Content-Type: application/json

{"deviceid": "<stable client id>", "jwt": "<aura access_token>"}
```

`deviceid` is any stable string naming the client; it is embedded in the
resulting token.

Response fields the app reads:

| Field | Meaning |
|-------|---------|
| `ok` | `true` on success |
| `token` (fallback `access-token`) | The admin bearer token (a JWT) |
| `refresh-token` | Present, but the app re-grants rather than refreshing |
| `circleid` | The circle (household) id |
| `status` | Subscription / entitlement status |
| `account-data` | `{account-id, email, display-name, bundled, subs[]}` |
| `error.message` | On failure |

The admin JWT payload carries `exp`, `type`, `circleid`, `deviceid`, and
`local-secret`; the MCP already checks `exp` before reuse.

**On a 401** from any admin call the app re-runs the grant (refreshing the
Aura token first if its `exp` has passed) and retries the request once. The
MCP behaves the same way.

### B.1.4 VC host discovery ✅

```
GET https://register.meetcircle-blue.co/api/v1.0/host?circleid=<id>&token=<admin_token>
```

Response: `{"host": "<vc host for this circle>"}`. Stored and used in place
of the default `vc` host when non-empty. The MCP looks it up after each fresh
grant, keeps the default on any failure, and skips it when `CIRCLE_VC_HOST` is set.

### B.1.5 Legacy Cognito login (informational)

Pre-Aura accounts authenticate against an AWS Cognito user pool
(`us-west-2_TiQsgtChT`, app client `fd7ail96985e3p3omb792ko0f`, SRP flow)
and then run the same admin grant with the Cognito id token as `jwt`.

### B.1.6 Headers on admin calls

`Authorization: Bearer <admin_token>` on the `vc` host.
The `account` host instead takes `Authorization: Bearer <aura access_token>`.

## B.2 Read: circle state

| Endpoint | Params | Returns | MCP |
|----------|--------|---------|-----|
| `/api/QUERY/all` | – | Full snapshot: `overall`, `users[]`, `devices[]`, `hardware[]`, `categories[]`, `version` | ✅ `get_circle_snapshot`, `list_profiles`, `get_circle_status` |
| `/api/QUERY/overall` | – | `overall` only (B.7.5) | ✅ (client only) |
| `/api/QUERY/devices` | – | `devices[]` (B.7.4) plus `overall` | ✅ `list_devices` |
| `/api/QUERY/hardware` | – | `hardware[]` Circle Home units (B.7.6) | ✅ `list_hardware` |
| `/api/QUERY/hardware/hardware/isOnline` | `hardware.cuuid` | Online flag for one unit | ✅ `list_hardware(check_online)` |
| `/api/QUERY/categories` | – | Filter category catalog (B.7.3) | ✅ `list_categories` |
| `/api/v2/categories` | – | Newer catalog with `apps[]`, `hidden`, `onrequest`, `enabled` | ✅ `list_categories` (names) |
| `/api/QUERY/users/user/photo` | `user.pid` | Profile avatar | ❌ |
| `/api/v2/profiles/badges` | – | `[{pid, badges[]}]` | ✅ `get_profile_badges` |
| `/api/v0.1/c/devStatus` (sent to **port 8995**) | `gotoken=<admin_token>` | Managed child devices: `{authStatus, totalDeviceCount, enabledDeviceCount, devices:[{deviceid, circleid, state, last}]}` | ✅ `list_go_devices` |
| `/api/QUERY/users` | – | `users[]`. Fallback for `list_profiles` when `/api/QUERY/all` fails; the app reads `users` from `/api/QUERY/all`. | ⚠️ |
| `/api/QUERY/circleinfo` | – | Fallback for `get_circle_status` when `/api/QUERY/all` fails; not in the app | ⚠️ |

`list_profiles` and `get_circle_status` read `/api/QUERY/all` first and fall
back to the older endpoints. `/api/QUERY/users/user/photo` is not implemented;
profiles carry `avatarUrl` when the snapshot has one.

## B.3 Read: usage and history

| Endpoint | Params | Returns | MCP |
|----------|--------|---------|-----|
| `/api/LOGS/usage/categories` | `user`, `day`, `ndays` | `report[]`, each with `data` = category id → minutes (B.7.7). MCP `get_usage_categories` adds `categoryNames` | ✅ |
| `/api/LOGS/usage/sites` | `user`, `day`, `ndays` | Per-site usage (declared by the app, never called; shape unverified). MCP `get_usage_by_site`, passed through raw | ✅ |
| `/api/LOGS/usage` | `user`, `day`, `ndays` | ⚠️ MCP `get_usage`; not in the app | ⚠️ |
| `/api/LOGS/history/{type}` | path `type` ∈ `allowed`, `blocked`; query `user`, `day`, `time`, `maxrecords`, `withcat` | Browsing history (B.7.8). The app sends `time=23:59`, `maxrecords=200`, `withcat=1`. MCP `get_history` flattens `report[].data[].sites[]` | ✅ |

**Time remaining for a profile** is not a single endpoint on either surface.
Compute it as the profile's `timeLimits` (B.7.1) plus today's
`extensions.limits`, minus the minutes in `/api/LOGS/usage/categories` for
`day=0`. MCP `get_time_remaining` does this (✅). How the limit fields read:

- A limit's `days` is the word `weekdays` or `weekend`, not day indices; a
  missing value means `weekdays`. A category can therefore have one limit of
  each kind. Today is a weekend day when its index (Sunday = `0`) appears in
  the profile's `weekend` string, judged in `overall.timezone`.
- `minutes` of `-1` means no limit for that day type.
- Category `T` is the total limit across all categories; its usage is the
  sum of every category in the day's report.

## B.4 Profiles 🔒

| Endpoint | Params | Notes | MCP |
|----------|--------|-------|-----|
| `/api/ADD/users/user` | `name`, `ageCategory`, `mode`, `toggles.defaultPlatforms` | Create; app sends `toggles.defaultPlatforms=Off`; response has `newPid` | ❌ |
| `/api/UPDATE/users/user/name` | `user.pid`, `value` | Rename | ❌ |
| `/api/UPDATE/users/user/ageCategory` | `user.pid`, `value` | `Pre-K`, `Kid`, `Teen`, `Adult`, `None` | ❌ |
| `/api/UPDATE/users/user/mode` | `user.pid`, `value` | `Filter` (normal) or `Pause` (internet paused) | ❌ |
| `/api/UPDATE/users/user/toggles/timeLimits` | `user.pid`, `value` | `On` / `Off` | ❌ |
| `/api/UPDATE/users/user/toggles/offTimes` | `user.pid`, `value` | `On` / `Off` | ❌ |
| `/api/UPDATE/users/user/photo` | **POST form**: `token`, `user.pid`, `photo` (base64) | Avatar upload | ❌ |
| `/api/DELETE/users/user` | `user.pid` | Delete profile | ❌ |
| `/api/ADD/users/user/relatedDevices/relatedDevice` | `user.pid`, `mac` | Assign a device to a profile | ❌ |

### B.4.1 Filter switches 🔒

Switches override the age-category default per category, platform (app), or
custom site. `state` ∈ `On`, `Off`, `Unmanaged`. `clear=true` replaces the
whole list; the app always sends that with comma-separated parallel lists.

| Endpoint | Params | MCP |
|----------|--------|-----|
| `/api/ADD/users/user/categorySwitches/switch` | `user.pid`, `clear`, `category` (ids), `state` | ❌ |
| `/api/ADD/users/user/platformSwitches/switch` | `user.pid`, `clear`, `platform` (ids), `state` | ❌ |
| `/api/DELETE/users/user/platformSwitches/switch` | `user.pid`, `switch.platform` | ❌ |
| `/api/ADD/users/user/customSwitches/switch` | `user.pid`, `site`, `toggle` (`On`/`Off`), `clear` | ❌ |
| `/api/DELETE/users/user/customSwitches/switch` | `user.pid`, `switch.site` | ❌ |

### B.4.2 Time limits and rewards 🔒

| Endpoint | Params | Notes | MCP |
|----------|--------|-------|-----|
| `/api/ADD/users/user/timeLimits/limit` | `user.pid`, `clear=true`, `category` (comma list of ids), `limit` (minutes, parallel), `days` (day-index strings, parallel), `weekend` (day-index string), `toggle` (`On`/`Off`) | Replaces every limit in one call | ❌ |
| `/api/ADD/users/user/extensions/limits/extension` | `user.pid`, `clear=true`, `category`, `minutes` (comma lists) | Extra minutes today per category | ❌ |
| `/api/ADD/users/user/extensions/offTimes/extension` | `user.pid`, `clear=true`, `offTimeId`, `startMinutes`, `endMinutes` (comma lists) | One-off change to today's off-time window | ❌ |

### B.4.3 Off times (legacy bedtime) 🔒

| Endpoint | Params | MCP |
|----------|--------|-----|
| `/api/ADD/users/user/flexOffTimes/offTime` | `user.pid`, `name`, `type` (`bedtime_weekday`, `bedtime_weekend`, `offtime`), `start`, `end`, `days`, `toggle` (`enabled`/`disabled`), `id`, `clear=true` — all comma-separated parallel lists | ❌ |

### B.4.4 Bedtimes v2 🔒 (JSON)

| Method / path | Body | MCP |
|---------------|------|-----|
| `GET /api/v2/profiles/bedtimes` | → `[{pid, bedtimes:[BedTime]}]` | ❌ |
| `POST /api/v2/profiles/{pid}/bedtimes` | BedTime | ❌ |
| `PUT /api/v2/profiles/{pid}/bedtimes/{id}` | BedTime | ❌ |

BedTime = `{id, pid, name, start, end, days:[dayNames], state}`,
`state` ∈ `enabled`, `disabled`.

### B.4.5 Focus times v2 🔒 (JSON)

| Method / path | Body | MCP |
|---------------|------|-----|
| `GET /api/v2/profiles/focustimes` | → `[{pid, focustimes:[FocusTime]}]` | ❌ |
| `GET /api/v2/focustimes/defaults` | → default filter sets | ❌ |
| `POST /api/v2/profiles/{pid}/focustimes` | FocusTime | ❌ |
| `PUT /api/v2/profiles/{pid}/focustimes/{id}` | FocusTime | ❌ |
| `DELETE /api/v2/profiles/{pid}/focustimes/{id}` | – | ❌ |

FocusTime = `{id, pid, name, start, end, days:[dayNames], enabled,
applyFilters, filters:{allowed:[catId], blocked:[catId], allowedURLs:[],
blockedURLs:[]}}`.

## B.5 Devices, circle settings, hardware 🔒

| Endpoint | Params | Notes | MCP |
|----------|--------|-------|-----|
| `/api/UPDATE/devices/device/displayName` | `device.uid`, `value` | Rename | ❌ |
| `/api/UPDATE/devices/device/mode` | `device.uid`, `value` | `Filter` / `Pause` per device | ❌ |
| `/api/DELETE/devices/device` | `device.uid` | Forget device | ❌ |
| `/api/UPDATE/overall/mode` | `value` | Pause / resume the whole home | ❌ |
| `/api/UPDATE/overall/timezone` | `value` | IANA zone id | ❌ |
| `/api/UPDATE/overall/locale` | `value` | `en`, `es`, … | ❌ |
| `/api/UPDATE/overall/defaultAssignedProfile` | `value` | pid for newly seen devices | ❌ |
| `/api/ADD/overall/activeNotifications/notification` | `id`, `clear=true` | Dismiss a dashboard notification | ❌ |
| `/api/UPDATE/hardware/hardware/isEnabled` | `hardware.cuuid`, `value` (`true`/`false`) | Enable / disable a Circle Home unit | ❌ |
| `/api/UPDATE/hardware/hardware/compatibleMode` | `hardware.cuuid`, `value` | | ❌ |
| `/api/POWER/reboot` | `cuuid` | Reboot a unit | ❌ |
| `/api/DELETE/hardware/hardware` | `hardware.cuuid`, `circleid` | Remove a unit | ❌ |
| `/api/AUTHORIZE/hardware` | `cuuid`, `sign-token=true` | Pair a unit; returns `access-token`, `refresh-token`, `localToken` for it | ❌ 🔒 |
| `/api/AUTHORIZE/godevice` | `appid`, `deviceid` | Parent-side approval of a child device (see C.1) | ❌ 🔒 |
| `/api/DELETE/account` | – | Deletes the whole account. Never expose. | ❌ 🔒 |

## B.6 Other parent-side hosts

| Host | Endpoint | Auth | Purpose | MCP |
|------|----------|------|---------|-----|
| account | `GET /api/v1.0/account/subscribe` | Bearer Aura JWT | `{ok, status, account-data:{account-id, email, bundled, subs:[{store, product-identifier, expires-date, auto-renew-enabled, is-trial-period, is-in-intro-offer-period, original-purchase-date, term-key}]}}` | ❌ |
| account | `POST /api/v1.0/account/subscribe` | Bearer Aura JWT | Attach a store receipt: `{store: playstore\|chargify\|recharge, receipt, term}` | ❌ 🔒 |
| account | `DELETE /api/v1.0/account/delete` | Bearer Aura JWT | Delete account | ❌ 🔒 |
| location | `GET /api/v1.0/locatechild` | query `circleid`, `parent_deviceid`, `child_deviceid`, `provider` | Ask a child device for its location; the answer arrives by push after the child posts C.5 | ❌ |
| push | `GET /register` | query `app`, `circleid`, `cuuid`, `deviceid`, `platform`, `token`, `signature`, `provider` | Register the parent phone for push | ❌ |
| urldb | `GET /circle/v1/category` | query `d` (domain), `model` | Category for a domain; response `{status, results:[{domain, catid}]}` | ❌ |
| appinfo | `GET /v1/appversion/environments/{env}/platforms/android` | none | Minimum app version | ❌ |
| redirect_cert | `GET /api/v1.0/cert.pem` | none | Block-page CA certificate | ❌ |

## B.7 Data shapes

Field names are exact; many numbers arrive as strings.

### B.7.1 Profile (`users[]` items)

```
pid, name, mode, ageCategory, role, weekend
toggles:          {timeLimits, offTimes, safeSearch, defaultPlatforms}   ("On"/"Off")
timeLimits:       [{id, category, minutes, days}]
flexOffTimes:     [{id, name, type, start, end, days}]
extensions:       {limits:   [{id, category, minutes, date}],
                   offTimes: [{id, offTimeId, startMinutes, endMinutes, date}]}
categorySwitches: [{category, state}]
platformSwitches: [{platform, state}]
customSwitches:   [{site, toggle}]
relatedDevices:   [{uid}]
```

`weekend` is the day-index string for weekend days; weekdays are its
complement.

### B.7.2 Enumerations

- `ageCategory`: `Pre-K`, `Kid`, `Teen`, `Adult`, `None`.
- `mode` (profile, device, overall): `Filter` or `Pause`.
- Switch `state`: `On`, `Off`, `Unmanaged`.

### B.7.3 Category

```
id, name, mainCategory, description, website,
visibleFor: [ageCategory], defaultOffFor: [ageCategory], timeLimits
```
The v2 catalog adds `hidden`, `onrequest`, `enabled`, and
`apps: [{id, name, description, hidden, onrequest}]`.

### B.7.4 Device (`devices[]` items)

```
uid, displayName, hostname, ip, manufacturer, brand, model, mode, type,
classification, rank, alias, isGo, newlyAdded, defaultPlatforms, pid
```
`isGo` marks a device managed by the child app (Part C).

### B.7.5 Overall

```
circleName, mode, enabled, locked, timezone, debugEnabled, coppaAgreement,
defaultAssignedProfile, compatibleMode, circleUUID,
activeNotifications: [{id, name}], version: {firmware, platforms}
```

### B.7.6 Hardware (`hardware[]` items)

```
cuuid, isOnline, isEnabled, compatibleMode, model,
network: {status}, hotspot: {ssid, password}, wifi: {ssid},
stats: {wifi: {rssi}}, version: {firmware}
```

### B.7.7 Usage by category

```
{"result": …, "report": [{"type", "start", "data": {"<categoryId>": "<minutes>", …}}]}
```
The headline total is the sum of every value in `report[0].data`.

### B.7.8 History

```
{"result": …, "report": [{"type", "subtype", "start", "withCategory",
                          "data": [{"uid", "sites": [...]}]}]}
```
Each `sites` entry carries at least `site`, `time`, `state`, and a category
id when `withcat=1`. Exact per-entry fields are unverified; capture a live
response before building on it.

---

# Part C. Child-device API ("Circle Go")

When the Circle app runs in **Kid mode** on a child's phone or tablet it
becomes an enforcement client: it runs a local VPN, filters traffic on the
device using rules it downloads, and reports back to the cloud. Its cloud
surface is small and is about enrollment, check-in, and fetching the rule
bundle. **It does not query usage or time remaining from the cloud**: those
are enforced locally from the downloaded bundle, and the cloud learns about
usage from the device's own reporting.

An integration can therefore impersonate a child device only to the extent
of enrolling one and pulling its rule bundle. Anything about "what is this
child allowed right now" is better answered from the parent API (Part B).

## C.1 Enrollment

Enrollment is a three-way handshake between the child device, the cloud, and
the parent. All calls are GET with query parameters on the `vc` host, port
443, with no auth header.

**Step 1 – announce the device (child).**

```
GET /api/REGISTER/newdevice?appid=<install id>&devicename=<name>
```

`appid` is a random id the app generates once per install; `devicename` is
the phone's name. Response: `{result, deviceid, vctoken}`. The app keeps
both: `deviceid` becomes the device's identity and `vctoken` is a short-lived
pairing token.

**Step 2 – pairing (parent).** The parent, signed in with their admin token,
approves the device:

```
GET /api/AUTHORIZE/godevice?appid=<install id>&deviceid=<deviceid>      (admin bearer, port 443)
```

The child can also ask the cloud to issue a pairing passcode the parent types
in instead:

```
GET /api/REGISTER/passcode?vctoken=<vctoken>
```

**Step 3 – claim the device token (child).**

```
GET /api/REGISTER/godevice?appid=…&devicename=…&deviceid=…&vctoken=…&sign-token=true|false
```

Response fields the app reads:

| Field | Meaning |
|-------|---------|
| `result` | success flag (A.3) |
| `gotoken` (fallbacks `access-token`, `access_token`) | The **device bearer token** used for every later call |
| `refresh-token` (fallback `refresh_token`) | Used to renew `gotoken` (C.2) |
| `circleid` | The circle the device now belongs to |
| `host` | VC host to use from now on (replaces the default) |
| `vctoken` | Refreshed pairing token |
| `error` | On failure |

**Step 4 – describe the device (child, port 8995, device auth).**

```
GET /api/v0.1/a/registerDevice?devname=<name>&homeSSID=<wifi ssid>&cuuid=<circle hardware uuid, may be empty>
```

## C.2 Authentication

Every child-device call to port 8995 carries:

- `Authorization: Bearer <gotoken>`
- query parameters `circleid=<circleid>` and `deviceid=<deviceid>`, appended
  automatically to every request.

**Refresh.** On a 401 the app renews the device token and retries once:

```
GET https://auth.meetcircle-blue.co/api/v1.0/refresh
Authorization: Bearer <refresh-token>
```

Response: `{gotoken | access-token, refresh-token, disable-management}`.
`disable-management` = `"true"` means the parent has removed this device's
management; the app then stops the VPN and answers every call locally with
a locally generated `403` whose `error` text mentions the disable-management flag.

The parent-side `/api/v0.1/c/devStatus` call (B.2) hits the same port 8995
but with the **admin** token in the `gotoken` query parameter.

## C.3 Check-in and settings (port 8995)

| Endpoint | Params | Returns | Notes |
|----------|--------|---------|-------|
| `/api/v0.1/a/ping` | `devname` | `{"status": "<state>"}` | Heartbeat. `status` ∈ `registered`, `ok`, `awol` are all "fine"; `not registered` (or an error body saying the device is not registered) means the parent removed the device. The app records the time of each successful ping. 📱 |
| `/api/v0.1/a/settings` | – | **ZIP archive** | The rule bundle. Entries are matched by suffix: one ending in `bin` → `settings.bin` (the profile's filter / time-limit / off-time rules), one ending in `customs` → `customs.bin` (custom site switches), one ending in `userinfo` → `userinfo.bin`. All three are opaque binaries consumed by the native filter engine. The app notes the download time and re-fetches on push notifications. 📱 |
| `/api/v0.1/a/notify/guestMode` | `profilename` | `{result}` | Tells the cloud the device switched to a guest profile 📱 |
| `/api/v0.1/a/notify/devOptions` | `profilename` | `{result}` | Reports that developer options were enabled (tamper signal) 📱 |
| `/api/v0.1/a/notify/timeZoneChange` | – | `{result}` | Reports a time-zone change (tamper / travel signal) 📱 |
| `POST /api/v2/logs/zip` (port 443, multipart field `log`) | – | 200 / 401 | Uploads a diagnostics archive 📱 |

The `profilename` parameter is the username stored at enrollment (C.6).

## C.4 Declared but unused

These paths are defined in the app's child-side client but have no caller
in 2.34.0.1. They look like an older in-cloud variant of C.6:

| Endpoint | Params | Probable purpose |
|----------|--------|------------------|
| `/api/USERINFO` | – | Profile info for the device (same shape as C.6) |
| `/api/USERPHOTO` | – | Profile avatar |
| `/api/v1.0/godevice/userinfo` | – | Same, v1.0 |
| `/api/v1.0/godevice/userphoto` | `deviceid` | Same, v1.0 |

Worth probing live with a device token; they are the only candidates for a
cloud-side "what applies to this child right now" query.

## C.5 Location (location host, port 443)

| Endpoint | Auth | Body | Purpose |
|----------|------|------|---------|
| `POST /api/v1.0/notifyparent?provider=<fcm\|pushy>` | device bearer + `circleid`/`deviceid` query | form: `circleid`, `parent_deviceid`, `child_deviceid`, `latitude`, `longitude`, `timestamp` | Child answers a parent's `locatechild` request (B.6) 📱 |

## C.6 On-device services 🏠

While the VPN is active the child app binds three local HTTP servers on
`127.0.0.1`, `::1`, and the device's own address:

| Port | Role |
|------|------|
| 45080 | HTTP block page (where filtered connections are redirected) |
| 45443 | HTTPS block page |
| 4444 | **User-info server**: `GET /api/USERINFO` → `{"info": {deviceId, deviceName, circleUUID, deviceManufacturer, username, age, circleTime, circleDay}}` |

`circleTime` and `circleDay` are stamped in at request time (current
`HH:mm` and weekday index). The block page reads this to personalize itself.
The app's own health check calls `http://localhost:4444/api/USERINFO` with a
500 ms timeout and restarts the servers if it fails.

The native filter engine exposes the parsed rule bundle to the UI as JSON
(`getSettingsJson`), which is how Kid-mode screens know the active profile,
limits, and off times without any network call.

## C.7 Other child-side downloads

| Host | Endpoint | Params | Purpose |
|------|----------|--------|---------|
| download | `GET /api/v1.0/app/getplatforms` | `DEVID`, `VER` | Platform (app-signature) database used for app-level filtering |
| urldb | `GET /circle/v1/category` | `d`, `model=android` | Looked up by the filter engine for uncategorized domains; `{status, results:[{domain, catid}]}` |
| download | `GET /dev/firmware/check_version.php`, `/dev/firmware/get_firmware.php` | `APP=1` / `devid` | Firmware for Circle Home units (parent side, during setup) |

## C.8 Local hardware setup 🏠

During Circle Home / Home Plus setup the parent app talks to the unit over
its own Wi-Fi hotspot with a `token` query parameter obtained from
`/api/AUTHORIZE/hardware`:

| Endpoint | Params |
|----------|--------|
| `/api/CIRCLEUUID` | `host=android` → `{circleUUID}` |
| `/api/SCAN` | `token` → `{scan:[{ssid, signal}]}` |
| `/api/QUERY/wifi`, `/api/QUERY/version` | `token` |
| `/api/UPDATE/wifi` | `ssid`, `key`, `test=true`, `token` |
| `/api/PROVISION` | `cloudhost`, `access-token`, `refresh-token`, `hwtoken`, `localtoken`, `circleid`, `token` |
| `/api/TEST/compatstart` (`timeout=60`), `/api/TEST/compatstop` | `token` |
| `/api/DEBUG/enable`, `/api/DEBUG/disable` | – |
| `/api/VIRTUAL/report` | `deviceid`, `vctoken` |

---

# Part D. Coverage summary

**Implemented (5 calls):** Aura sign-in, Aura refresh, admin grant,
`/api/LOGS/usage/categories`, plus `/api/QUERY/users`, `/api/LOGS/usage`,
`/api/QUERY/circleinfo` (work live, but are not what the app uses).

**Parent-side read gaps, in suggested order:**

1. `/api/QUERY/all` – profiles, devices, hardware, overall, categories in one
   call; could replace `list_profiles` and `get_circle_status`.
2. `/api/LOGS/history/{allowed|blocked}` – per-site browsing history.
3. `/api/v2/profiles/bedtimes`, `/api/v2/profiles/focustimes` – schedules.
4. `/api/QUERY/categories` or `/api/v2/categories` – names for the category
   ids in usage reports.
5. A derived "time remaining" tool (B.3) built from limits, extensions, and
   today's usage.
6. `/api/v1.0/host` on the register host – correct VC host per circle.
7. `/api/v0.1/c/devStatus` – which managed child devices are online.
8. `GET /api/v1.0/account/subscribe` – subscription status.

**Parent-side writes, by usefulness (each needs a decision):**

1. Pause / resume a profile, a device, or the whole home (`UPDATE/*/mode`).
2. Grant extra time today (`extensions/limits/extension`).
3. Edit time limits, bedtimes, focus times, filter switches.
4. Rename / assign devices; create / delete profiles.

**Child-side:** enrolling a synthetic child device (C.1) is possible but only
yields the opaque rule bundle and check-in; the unused endpoints in C.4 are
the one thing worth probing. Everything a kid-facing feature would need
(usage, limits, time left) is available from the parent API.

**Never expose:** account deletion, hardware deletion, hardware pairing.
