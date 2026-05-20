# Deferred Transaction Boundary

The shopping-cli MVP is consultation-only.

Allowed progression:

```text
inquiry -> quote_request -> purchase_intent
```

`quote_request` and `purchase_intent` are only message intents inside a conversation. They do not create a commitment.

Do not add transaction state until the consultation network is reliable and merchant confirmation rules are designed explicitly.
