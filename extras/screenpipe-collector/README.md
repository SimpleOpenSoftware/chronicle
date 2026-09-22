# Chronicle ScreenPipe companion

This service keeps ScreenPipe as the local capture store while forwarding compact,
event-driven observations and, only when enabled, completed audio chunks to Chronicle.
Long activities remain one observation with incremental novel-text samples. Screen
pixels and OCR are retrieved only for bounded jobs requested by Chronicle. See the full
[capture-node architecture](../../docs/screenpipe.md).

## Pair and run

1. Create a pairing code from Chronicle's **Timeline → Sources** panel.
2. Pair this device:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe pair \
     --backend https://kraken.example \
     --code PAIRING_CODE
   ```

3. Start ScreenPipe with Chronicle's privacy-oriented defaults. The example records
   both the default microphone and system output while leaving transcription to
   Chronicle:

   ```bash
   screenpipe record --audio-transcription-engine disabled \
     --use-system-default-audio true --use-all-monitors true \
     --idle-capture-interval-ms 20000 \
     --use-pii-removal true --disable-keyboard-capture \
     --disable-clipboard-capture --capture-scroll true \
     --prioritize-input-latency --pause-on-drm-content \
     --screenpipe-aec-enabled \
     --disable-telemetry \
     --video-quality balanced --retention-days 90 \
     --retention-mode media --api-auth true
   ```

   Set `SCREENPIPE_API_KEY` for both ScreenPipe and the pairing command so the
   companion can authenticate bounded local OCR queries.

   For system audio without the microphone, replace
   `--use-system-default-audio true` with
   `--use-system-default-audio false --audio-device "DEVICE (output)"`. Discover the
   exact platform device names with `screenpipe audio list --output json`. Use
   `--disable-audio` only for screen-only capture.

   Pair with `--forward-audio none|output|input|both` to independently control which
   locally recorded sources are uploaded. The guided setup asks for both the local
   capture mode and forwarding mode.

   Leave ScreenPipe's meeting detector **enabled** (no
   `--disable-meeting-detector`): on macOS and Windows it persists meetings —
   with titles — into its own database, and the companion mirrors those rows
   into Chronicle and tags forwarded audio chunks with the meeting interval,
   so the backend bounds the conversation on the real call instead of fixed
   time windows. Because the rows are persisted, bounds survive companion
   downtime and backfill retroactively. Use ScreenPipe's
   `ignored_meeting_apps` to exclude specific apps. On Linux, where ScreenPipe
   has no meeting sensor, the companion detects calls itself from the PipeWire
   graph (a known meeting app or browser holding a *running* microphone
   stream, browsers attributed through the current observation's URL) —
   live-only, so intervals only cover time the companion was running. Disable
   all of it at pairing time with `--no-meeting-detection`.

4. Run the companion:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe run
   ```

   After verification, install it as a separate user service:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe install-service
   ```

ScreenPipe itself can be installed independently with `screenpipe service install --record-args "..."`, using the recording arguments above.

Configuration is stored with mode `0600` under `~/.config/chronicle-screenpipe`;
checkpoints and crash-resumable observation state live under
`~/.local/state/chronicle-screenpipe`.

## Local privacy screening

Set `privacy_screening` to `true` in the collector configuration to run the local
Freepik screening worker. CPU inference is the default. Backend enforcement must
be deployed before activating a source; a running recorder alone does not mean
its captures are protected. Missing frames and unavailable models remain held
after activation. Sampling checks the first frame and every tenth frame per
display, with additional checks for elapsed time and context changes; unsampled
frames are not individually verified. The next capture after 30 seconds without a
check also triggers screening. Predictions use the full image and corner crops;
window/URL changes and adult-site or explicit adult-search text trigger extra checks.

Keep the recorder's idle capture interval at 20 seconds, as in the setup above.
This leaves headroom below the screening policy's 30-second capture-gap limit and
pins the cadence across power-profile changes. An actual gap over 30 seconds stays
unverified even when its available endpoints look ordinary. Changing the recorder
cadence affects new captures; it does not clear historical gaps.

The worker stores scheduling state, pending deliveries and up to 100,000 predictions
in local SQLite storage. Predictions can be reused only for identical model inputs
and the same model/execution settings. Perceptual similarity never establishes that
an image is safe. Model confidence is interpreted separately from cached predictions.

After source activation, screen exclusions also cover microphone and system audio
from that device. Raw captures remain stored, while processing uses only allowed
time ranges and preserves original capture timestamps. Audio without screen coverage
requires an explicit override. Independent acoustic detection is not enabled by
turning on screen screening; it requires its own reviewed evaluation and quality gates.

Chronicle's Timeline shows metadata-only private or unresolved spans with
**Allow processing** and **Keep excluded** controls. A stale review must be reloaded
before saving. Labels exported from the separate local review HTML evaluate the
detector; they do not grant processing permission.

The source status panel separates live captures from history/rechecks and shows
queue age, cache hits and screening timings. A completed job can still report a
missing original, a capture gap or an uncertain result. Queue progress therefore
does not imply that its time has become allowed. Failed work remains held through
retries, restarts and rollback; error diagnostics omit capture paths and contents.

On a Linux NVIDIA host, set `privacy_device` to `cuda` and explicitly select the
CUDA dependency group in both installation and the service command:

```bash
uv sync --project extras/screenpipe-collector --no-default-groups --group cuda
uv run --frozen --project extras/screenpipe-collector --no-default-groups --group cuda chronicle-screenpipe run
```

The GPU configuration uses full precision with TF32 disabled. Device capability,
inference-library versions and model checksum distinguish cached predictions.
An unavailable configured GPU holds work rather than changing inference settings.
Evaluate reviewed positives and ordinary controls on the intended runtime before
activating that source. The default CPU group and CUDA group are mutually exclusive.

On an Apple Silicon Mac, `privacy_device: "mps"` uses the local Apple GPU with the
default dependency group. It requires full precision, with
`PYTORCH_ENABLE_MPS_FALLBACK`, `PYTORCH_MPS_FAST_MATH` and
`PYTORCH_MPS_PREFER_METAL` unset or set to `0`. The chip, macOS version and execution
settings distinguish these cache entries from CPU/CUDA predictions. An unavailable
GPU or unsupported operation keeps the affected work held. Validate the intended
runtime against reviewed candidate and control images before changing the device.

Historical screening registers a durable coverage requirement before queueing
frames. Bounds must have explicit timezones and span at most 32 days:

```bash
uv run --project extras/screenpipe-collector chronicle-screenpipe screen-history \
  --start 2026-01-01T00:00:00+05:30 --end 2026-01-02T00:00:00+05:30
```

Every display observed in that range is conservatively required throughout it.
Gaps, an empty display inventory and unavailable originals stay held; historical
frames cannot establish exactly when a display was disconnected. Results reuse
the normal scheduler and cache, and retries preserve the sampling cadence.
The command changes processing eligibility, retains raw captures, and does not
rewrite published notes. Use the same CUDA flags for this command on CUDA hosts.
