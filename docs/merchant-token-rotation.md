# Merchant token rotation

The existing `POST /merchants/{merchant_id}/token/rotate` remains available for
legacy callers. It immediately revokes every active merchant token. New clients
should use the two-phase flow below so they can prove the replacement works
before the old credential is revoked.

`GET /merchants/{merchant_id}/token/status` is available to the current
merchant token only. It returns the authenticated token's creation and expiry
timestamps and a non-secret prefix for rotation scheduling.

## Prepare

Generate `replacement_token` locally with at least 256 bits of randomness and a
`shopping_merchant_` prefix. Generate a separate random URL-safe `rotation_id`
(at least 128 bits). Before sending the request, durably save both values in the
client's protected credential store, along with the merchant id and old-token
reference. Never put the replacement in command-line arguments or logs.

Send `POST /merchants/{merchant_id}/token/rotate/prepare` over HTTPS with the
current merchant credential as Bearer authentication (or `merchant_token` in
the body for compatible clients), plus `rotation_id` and `replacement_token`.
The server stores only the replacement token hash. The old token remains active
and the replacement is temporarily active for at most 15 minutes.

If the response is lost, resend the exact same rotation id and replacement
token while authenticating with the old token. A reused id with different
inputs is rejected. Only one pending rotation may target a given old token.

## Confirm

After saving the new credential as the client's active credential and verifying
it can authenticate, send `POST /merchants/{merchant_id}/token/rotate/confirm`
with `rotation_id`, authenticated by the replacement token. Confirmation is
idempotent and revokes only the specific old token used to prepare this rotation.
The new credential is the proof of possession; knowing the rotation id alone is
not sufficient.

If the 15-minute window expires, the replacement is rejected at authentication
and the old token remains active. Start a new rotation with a new id and
replacement. Keep the locally persisted pending credential until the client has
observed the confirmed response or can recover the status by retrying confirm.

## Client recovery boundary

This API supplies the server-side protocol only. It does not implement a client
credential vault, atomic local credential-file replacement, or restart recovery.
Clients must persist the generated replacement before calling prepare and must
keep enough local state to retry prepare or confirm after a crash. Do not use
plain-text shared config files unless the deployment explicitly protects them.
