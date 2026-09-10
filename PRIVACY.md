# Privacy

`mac-dev-clean` is a local disk-inspection and cleanup utility.

## Data handling

- The command-line tools and native macOS app scan local file paths, sizes, and
  modification dates needed to identify supported cleanup targets.
- Scan results are displayed locally. They are not uploaded, sold, shared, or
  used for analytics, advertising, or tracking.
- The project contains no telemetry SDK, crash-reporting SDK, advertising SDK,
  or remote account system.
- JSON output is written only where the user explicitly redirects or saves it.
  It can contain local paths and should be reviewed before sharing publicly.

## Local action journal

Preview, refusal, failure, skipped dry-run, and successful cleanup outcomes are
recorded in the append-only local journal at
`~/Library/Logs/mac-dev-clean/actions.jsonl`. Records can contain local paths,
opaque recommendation IDs, exact command arguments, sizes, timestamps, and
bounded outcome details. The journal is never uploaded and is excluded from
shared diagnostics.

The file is size-bounded and retains one local rotated segment. You can inspect
it with `mac-dev-clean journal` and explicitly remove both segments with
`mac-dev-clean journal --clear`.

## Network and iCloud behavior

The cleanup engine does not require a network connection. The native app's
About page includes an explicit link to `https://ravenvector.com`; macOS opens
that URL only after the user chooses the link.

The app may open iCloud Drive in Finder to support manual review. It does not
upload, move, evict, or delete iCloud content and does not access an iCloud
account programmatically.

## Deletion

Opening the native app and running scan/report commands are read-only. Cleanup
requires an explicit command-line action or selected native-app categories plus
a confirmation. Review-only locations are never included in cleanup.

For security-sensitive reports, follow [SECURITY.md](SECURITY.md).

