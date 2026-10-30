# ERP sign-in: the verify service the assistant calls

**For the ERP team.** Staff sign in to the Knowledge Assistant with the same user name and password they use for
the company system (BIS / BiMob). The assistant does not read or compare passwords itself: it sends what the user
typed to one endpoint that the ERP team runs, and the endpoint answers *yes, this is user X* or *no*. This page is
the whole contract. A working stand-in (`scripts/mock_erp_verify.py`) shows the expected behaviour and is what the
assistant's tests run against.

## The endpoint

```
POST https://<erp host>/<path you choose>
Content-Type: application/json
Authorization: Bearer <a shared key the ERP team issues to the assistant>

{"username": "<as the user typed it, spaces at either end removed>", "password": "<as typed>"}
```

### Answer: the account is valid

HTTP 200:

```json
{
  "valid": true,
  "user_code": "U0123",
  "user_name": "jdoe",
  "name": "John Doe",
  "employee_code": "E0456",
  "company_id": "02",
  "unit_id": "1"
}
```

`valid`, `user_code` and `user_name` are required; the others are optional (send them when you have them).
`user_name` is the account's user name as the ERP knows it, in plain text. `name` is shown in the assistant as
"Signed in as …".

### Answer: anything else

HTTP 200 with `{"valid": false}` — for a wrong password, an unknown user name, an inactive account, and a user
name that more than one active account shares. **The same answer for all of them**, so the endpoint cannot be used
to find out which user names exist.

Other statuses mean the check could not be made, and the user is told "the sign-in service cannot be reached":
`401`/`403` (the assistant's key is wrong), `429` (rate limited), `5xx`, or no answer within 5 seconds.

## What the check must be

The same check the ERP's own sign-in makes — `SystemManager.dbo.sp_UserAuthenticationService`, option 1: the
user name and password match one row of `dbo.tblUser` with `IsActive = 1`, compared the way the ERP application
compares them (including whatever encoding it applies before comparing).

## Requirements

1. **HTTPS only.** The password is in the request body. The assistant refuses a plain `http://` address unless
   `ERP_VERIFY_ALLOW_HTTP=true` is set on purpose (a test stand-in on the same machine).
2. **Never return the password**, a hash of it, the stored value, or anything derived from them.
3. **Do not write the password to any log** — the request body included.
4. **Accept only the assistant's key** (`Authorization: Bearer …`), and preferably only from the assistant's
   server, `192.168.10.21`.
5. **Rate-limit** failed attempts per user name and per caller. The assistant also locks a user name after 5
   failures in 10 minutes.
6. **Do not mark the user as signed in to the ERP** (`tblUser.IsLoggedIn`, `LogonTime`): signing in to the
   assistant is not an ERP session. Recording the attempt in the ERP's own audit log is the ERP team's choice.
7. **Answer within 5 seconds.**

## What the assistant does with the answer

- The user's identity in the assistant is `user_name` (lower case); `name` is the name it shows.
- What the user may see is decided by the assistant's own configuration, not by the ERP: today every ERP user gets
  the same scope as the current open access (every factory, `auth.erp.groups`), and the user names listed in
  `auth.erp.admins` also see the admin details. Per-company or per-factory rules can be added later from
  `company_id` / `unit_id`.
- A signed-in user stays signed in on that browser for `auth.session_hours` (12 by default), so a page reload does
  not ask again. Signing out ends it on every browser. An account the ERP deactivates keeps its assistant session
  until that session expires.
- Every attempt is logged in the assistant's `logs/auth.csv`: time, user name, result, duration. Never the password.

## Switching it on (assistant side)

In `.env` (the assistant's server):

```
ERP_VERIFY_URL=https://<erp host>/<path>
ERP_VERIFY_API_KEY=<the key the ERP team issues>
# optional: ERP_VERIFY_CA_FILE=<PEM of the ERP server's CA, if it is not a public one>
```

In `config/settings.yaml`: `auth.provider: erp`. Then recreate the app (`docker compose up -d --no-deps app`).
To switch back: `auth.provider: none`.
