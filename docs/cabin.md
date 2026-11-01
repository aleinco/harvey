# Running Harvey from your own dashboard (the cabin)

Harvey's dashboard only answers on `127.0.0.1`. If your team already works
from a company dashboard (a CRM, an ERP, an internal admin), you can feed
Harvey from it and steer the outreach there instead. Harvey still never
listens on the network: once a cycle it makes **one outbound HTTPS call** to
your dashboard.

```yaml
# harvey.local.yaml (or harvey.yaml)
cabin:
  enabled: true
  url: "https://dashboard.example/api/v1/harvey"   # https only (http only for localhost)
```

```bash
# .env
CABIN_TOKEN=<a long random token your dashboard also knows>
```

## The call

`POST <url>/sync` with `Authorization: Bearer <CABIN_TOKEN>`.

**Harvey sends:**

| Key | What |
|---|---|
| `events` | What happened since the last successful call: `email_sent`, `reply` (with `intent`), `reply_queued`, `opt_out`, `bounce`, `escalation`, `manual_send`, `write_failed`, `reply_after_opt_out`. Each has `id`, `type`, `at`, `email`, and `external_id` when the contact came from your dashboard. |
| `outbox` | Every message waiting for approval: `id`, `external_id`, `to`, `subject`, `body`, `kind`, `step`, `held` (Harvey held it because of a foreign link or a quote of its instructions). |
| `import_results` | What happened to the records you sent last time (`created`, `updated`, `skipped`, `errors`, each keyed by your `external_id`), or `null`. |
| `status` | `sending_paused` (the reason, or empty) and the configured `product_keys`. |

**Your dashboard answers** (every key optional):

| Key | What |
|---|---|
| `records` | Prospects to import, up to 500 a call. See below. |
| `decisions` | `[{"outbox_id": "...", "decision": "approve" \| "reject"}]` for messages in the outbox. |
| `control` | `{"sending_paused": true}` pauses all sending. `false` resumes only a pause the cabin set: if Harvey stopped itself (bounce rate), resume it in Harvey, on purpose. |

The events cursor moves only after a 2xx answer, so a failed call is resent
next cycle. Your endpoint should therefore treat events as idempotent by `id`.

## Records

The same rows `harvey import <file.json|file.jsonl>` takes:

```json
{"external_id": "crm:1234",
 "company": "Example Bakery",
 "website": "https://example-bakery.example",
 "email": "hello@example-bakery.example",
 "email_status": "risky",
 "phone": "+34 600 000 000",
 "industry": "bakery",
 "location": "Example Town",
 "product_key": "hosting",
 "facts": {"Homepage response time": "1.4 s", "ChatGPT crawler": "blocked by robots.txt"},
 "notes": "Old customer, 2023 website project."}
```

- `external_id` (required) is your id; events and results come back with it.
- `product_key` must be one of the configured products (`products:` in the config).
- `facts` are what you measured. The Writer cites them and nothing else.
- `email_status`: `verified`, `risky` (default) or `guess` (stored, never sent).
- A contact who opted out, replied or is past their first email is never changed by
  an import; one nobody has written to yet can be re-routed to another product.
- Rows on `compliance.do_not_contact` are skipped. One contact per company.
