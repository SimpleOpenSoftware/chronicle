# Speaker reference assets

A deliberately enrolled audio clip and its embedding are independent speaker-reference
assets. The source recording is optional provenance, not a continuing dependency.
Archiving, deleting, trimming, or changing the privacy of that source does not revoke
a completed enrollment or recognition that used it.

When Chronicle enrolls a clip from a recording, it checks access before reading the
source and again while publishing the enrollment. It retains the original conversation
IDs, capture records, and admission revisions in the enrollment journal and provider
binding. Directly supplied clips may omit this source metadata. Missing historical
links must remain absent rather than being inferred from filenames or upload times.

The staged enrollment journal still owns preparation, activation, idempotent retries,
and compensation for incomplete writes. Explicit enrollment quarantine remains effective.
Recovery quarantines abandoned or uncertain writes; it does not revisit source privacy
for completed assets. Gallery revisions and tenant ownership remain checked during
recognition. The recording being recognized and any separate corpus-reference audio
retain their own privacy admission checks.

The gallery checks the enrolled profile and clip records independently of whether an
operation journal exists. Existing direct uploads do not need a fabricated enrollment
operation. A stored clip is not the original source recording: either can exist without
the other, and a filename alone is not a source-recording identity.
