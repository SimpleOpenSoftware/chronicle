package com.chronicle.duplexaudio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.audiofx.AcousticEchoCanceler
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaRecorder
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.SystemClock
import android.util.Base64
import androidx.annotation.RequiresApi
import androidx.core.content.ContextCompat
import androidx.core.os.bundleOf
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

@RequiresApi(Build.VERSION_CODES.S)
class ChronicleDuplexAudioModule : Module() {
  private val captureExecutor = Executors.newSingleThreadExecutor()
  private val playbackExecutor = Executors.newSingleThreadExecutor()
  private val capturing = AtomicBoolean(false)
  private var captureEpoch = 0
  private var recorder: AudioRecord? = null
  private var opusEncoder: MediaCodec? = null
  private var player: AudioTrack? = null
  private var echoCanceler: AcousticEchoCanceler? = null
  private var noiseSuppressor: NoiseSuppressor? = null
  private var audioManager: AudioManager? = null
  private var previousMode = AudioManager.MODE_NORMAL
  private var previousDevice: AudioDeviceInfo? = null
  private var focusRequest: AudioFocusRequest? = null
  @Volatile
  private var captureSuppressed = false

  @Volatile
  private var currentResponse: EpochResponse? = null
  private class PlaybackStream(val binding: EpochResponse, var skip: Int) {
    val packets = ArrayBlockingQueue<ByteArray>(100)
    val total = AtomicLong(-1)
    var sequence = 0
    @Volatile var rendered = 0L
    @Volatile var decoded = 0L
  }
  @Volatile private var currentStream: PlaybackStream? = null


  override fun definition() = ModuleDefinition {
    Name("ChronicleDuplexAudio")
    Events("onOpusFrame", "onPlaybackState", "onRouteChange")

    OnDestroy {
      tearDownEngine(restoreRouting = true)
      captureExecutor.shutdownNow()
      playbackExecutor.shutdownNow()
    }

    AsyncFunction("startVoiceSession") { options: Map<String, Any> ->
      val epoch = (options["captureEpoch"] as? Number)?.toInt()
        ?: throw CodedException("invalid_capture_epoch", "captureEpoch is required", null)
      if (epoch < 0) {
        throw CodedException("invalid_capture_epoch", "captureEpoch must be non-negative", null)
      }
      start(epoch)
      capabilities()
    }

    AsyncFunction("beginResponse") { response: Map<String, Any> -> beginResponse(response) }
    AsyncFunction("appendResponse") { packet: Map<String, Any> -> appendResponse(packet) }
    AsyncFunction("finishResponse") { response: Map<String, Any> -> finishResponse(response) }

    AsyncFunction("cancelResponse") { responseId: String, generation: Int ->
      val current = currentResponse
      if (DuplexAudioPolicy.shouldCancel(current, responseId, generation)) {
        cancelCurrent(null)
      }
    }

    AsyncFunction("stopVoiceSession") {
      val restored = tearDownEngine(restoreRouting = true)
      mapOf(
        "restorationSucceeded" to restored,
        "failureCode" to if (restored) null else "far_field_restore_failed",
      )
    }
  }

  private fun context(): Context = appContext.reactContext
    ?: throw CodedException("engine_unavailable", "React context unavailable", null)

  private fun start(epoch: Int) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
      throw CodedException("platform_unavailable", "Android API 31 is required", null)
    }
    val context = context()
    if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
      throw CodedException("permission_denied", "Microphone permission denied", null)
    }
    val continuingSession = audioManager != null
    tearDownEngine(restoreRouting = false)
    captureEpoch = epoch
    val manager = audioManager
      ?: (context.getSystemService(Context.AUDIO_SERVICE) as AudioManager).also {
        audioManager = it
      }
    if (!continuingSession) {
      previousMode = manager.mode
      previousDevice = manager.communicationDevice
      manager.mode = AudioManager.MODE_IN_COMMUNICATION
      selectCommunicationRoute(manager)
      requestFocus(manager)
    }

    val inputBuffer = maxOf(
      AudioRecord.getMinBufferSize(16_000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT),
      3_200,
    )
    val newRecorder = AudioRecord.Builder()
      .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
      .setAudioFormat(
        AudioFormat.Builder()
          .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
          .setSampleRate(16_000)
          .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
          .build()
      )
      .setBufferSizeInBytes(inputBuffer)
      .build()
    if (newRecorder.state != AudioRecord.STATE_INITIALIZED) {
      newRecorder.release()
      throw CodedException("engine_unavailable", "AudioRecord did not initialize", null)
    }
    recorder = newRecorder
    opusEncoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS).apply {
      configure(
        MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 16_000, 1).apply {
          setInteger(MediaFormat.KEY_BIT_RATE, 24_000)
          setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 640)
        },
        null,
        null,
        MediaCodec.CONFIGURE_FLAG_ENCODE,
      )
      start()
    }
    echoCanceler = if (AcousticEchoCanceler.isAvailable()) {
      AcousticEchoCanceler.create(newRecorder.audioSessionId)?.apply { enabled = true }
    } else null
    noiseSuppressor = if (NoiseSuppressor.isAvailable()) {
      NoiseSuppressor.create(newRecorder.audioSessionId)?.apply { enabled = true }
    } else null

    val outputBuffer = maxOf(
      AudioTrack.getMinBufferSize(24_000, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT),
      3_200,
    )
    player = AudioTrack.Builder()
      .setAudioAttributes(
        AudioAttributes.Builder()
          .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
          .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
          .build()
      )
      .setAudioFormat(
        AudioFormat.Builder()
          .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
          .setSampleRate(24_000)
          .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
          .build()
      )
      .setBufferSizeInBytes(outputBuffer)
      .setTransferMode(AudioTrack.MODE_STREAM)
      .build()
    if (player?.state != AudioTrack.STATE_INITIALIZED) {
      tearDownEngine(restoreRouting = true)
      throw CodedException("playback_unavailable", "AudioTrack did not initialize", null)
    }
    registerRouteCallbacks(manager)
    capturing.set(true)
    newRecorder.startRecording()
    captureExecutor.execute { captureLoop(newRecorder, epoch) }
  }

  private fun captureLoop(activeRecorder: AudioRecord, epoch: Int) {
    val frame = ByteArray(640)
    val encoder = opusEncoder ?: return
    val outputInfo = MediaCodec.BufferInfo()
    while (capturing.get() && recorder === activeRecorder && captureEpoch == epoch) {
      val count = activeRecorder.read(frame, 0, frame.size, AudioRecord.READ_BLOCKING)
      if (count <= 0 || captureSuppressed) continue
      val durationMs = count.toDouble() / (16_000 * 2) * 1_000
      val capturedUs = SystemClock.elapsedRealtimeNanos() / 1_000 - (durationMs * 1_000).toLong()
      val inputIndex = encoder.dequeueInputBuffer(10_000)
      if (inputIndex >= 0) {
        encoder.getInputBuffer(inputIndex)?.apply { clear(); put(frame, 0, count) }
        encoder.queueInputBuffer(
          inputIndex, 0, count, capturedUs, 0
        )
      }
      while (true) {
        val outputIndex = encoder.dequeueOutputBuffer(outputInfo, 0)
        if (outputIndex < 0) break
        if (outputInfo.size > 0 && outputInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
          val packet = ByteArray(outputInfo.size)
          encoder.getOutputBuffer(outputIndex)?.apply {
            position(outputInfo.offset)
            limit(outputInfo.offset + outputInfo.size)
            get(packet)
          }
          sendEvent(
            "onOpusFrame",
            bundleOf(
              "captureEpoch" to epoch,
              "capturedAtMs" to System.currentTimeMillis().toDouble()
                - (SystemClock.elapsedRealtime().toDouble() - outputInfo.presentationTimeUs / 1_000.0),
              "monotonicTimestampMs" to outputInfo.presentationTimeUs / 1_000.0,
              "sampleRate" to 16_000,
              "channels" to 1,
              "frameDurationMs" to durationMs,
              "audioLevel" to DuplexAudioPolicy.audioLevel(frame, count),
              "opusBase64" to Base64.encodeToString(packet, Base64.NO_WRAP),
            ),
          )
        }
        encoder.releaseOutputBuffer(outputIndex, false)
      }
    }
  }

  @Synchronized private fun beginResponse(response: Map<String, Any>) {
    val id = response["responseId"] as? String ?: error("Missing response identity")
    val generation = (response["generation"] as? Number)?.toInt() ?: error("Missing generation")
    val epoch = (response["captureEpoch"] as? Number)?.toInt() ?: error("Missing epoch")
    val skip = (response["preSkipSamples"] as? Number)?.toInt() ?: error("Missing pre-skip")
    require(id.isNotEmpty() && epoch == captureEpoch && skip in 0..48_000)
    val activePlayer = player ?: error("AudioTrack unavailable")
    cancelCurrent(null)
    val stream = PlaybackStream(EpochResponse(id, generation, epoch), skip)
    currentResponse = stream.binding
    currentStream = stream
    captureSuppressed = capabilities()["mode"] == "duplex_half"
    activePlayer.pause()
    activePlayer.flush()
    activePlayer.play()
    playbackExecutor.execute { playOpusStream(stream, activePlayer) }
  }

  private fun requireStream(value: Map<String, Any>): PlaybackStream {
    val stream = currentStream ?: error("No active playback")
    require(value["responseId"] == stream.binding.id &&
      (value["generation"] as? Number)?.toInt() == stream.binding.generation &&
      (value["captureEpoch"] as? Number)?.toInt() == stream.binding.captureEpoch)
    return stream
  }

  @Synchronized private fun appendResponse(packet: Map<String, Any>) {
    val stream = requireStream(packet)
    require(stream.total.get() < 0 && (packet["sequence"] as? Number)?.toInt() == stream.sequence)
    val bytes = Base64.decode(packet["opusBase64"] as? String ?: error("Missing Opus packet"), Base64.DEFAULT)
    require(bytes.size in 1..4096)
    require(stream.packets.offer(bytes)) { "Playback queue exceeded two seconds" }
    stream.sequence += 1
  }

  @Synchronized private fun finishResponse(response: Map<String, Any>) {
    val stream = requireStream(response)
    val total = (response["totalSamples"] as? Number)?.toLong() ?: error("Missing playback total")
    require(total >= 0 && stream.total.compareAndSet(-1, total))
  }

  private fun playOpusStream(stream: PlaybackStream, activePlayer: AudioTrack) {
    val binding = stream.binding
    var ownedDecoder: MediaCodec? = null
    try {
      val decoder = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
      ownedDecoder = decoder
      val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 24_000, 1)
      format.setInteger(MediaFormat.KEY_PCM_ENCODING, AudioFormat.ENCODING_PCM_16BIT)
      val head = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN)
      head.put("OpusHead".toByteArray(Charsets.US_ASCII)).put(1.toByte()).put(1.toByte())
      head.putShort(0.toShort()).putInt(24_000).putShort(0.toShort()).put(0.toByte()).flip()
      format.setByteBuffer("csd-0", head)
      format.setByteBuffer("csd-1", ByteBuffer.allocate(8).order(ByteOrder.nativeOrder()).putLong(0).apply { flip() })
      format.setByteBuffer("csd-2", ByteBuffer.allocate(8).order(ByteOrder.nativeOrder()).putLong(0).apply { flip() })
      decoder.configure(format, null, null, 0)
      decoder.start()
      val info = MediaCodec.BufferInfo()
      var inputSequence = 0L
      var endedInput = false
      var outputEnded = false
      var writtenFrames = 0L
      var started = false
      var lastReport = 0L
      var outputRate = 24_000
      var tail = ByteArray(0)
      var next: ByteArray? = null
      val initialHead = activePlayer.playbackHeadPosition.toLong() and 0xffffffffL
      fun observe() {
        val played = minOf(writtenFrames, ((activePlayer.playbackHeadPosition.toLong() and 0xffffffffL) - initialHead) and 0xffffffffL)
        stream.rendered = played
        if (!started && played > 0) {
          started = true
          lastReport = played
          emitPlayback(binding.id, binding.generation, "started", null,
            SystemClock.elapsedRealtime().toDouble() - played * 1_000.0 / 24_000)
        } else if (started && played - lastReport >= 2400) {
          lastReport = played
          emitPlayback(binding.id, binding.generation, "progress", null)
        }
      }
      fun write(pcm: ByteArray) {
        var offset = 0
        while (offset < pcm.size && currentStream === stream) {
          val written = synchronized(this@ChronicleDuplexAudioModule) {
            if (currentStream !== stream) return
            activePlayer.write(pcm, offset, pcm.size - offset, AudioTrack.WRITE_NON_BLOCKING)
          }
          check(written >= 0) { "AudioTrack write failed" }
          if (written == 0) { observe(); Thread.sleep(5); continue }
          offset += written
          writtenFrames += written / 2
          observe()
        }
      }
      while (!outputEnded && currentStream === stream) {
        observe()
        if (!endedInput) {
          if (next == null) next = stream.packets.poll()
          if (next != null || stream.total.get() >= 0) {
            val index = decoder.dequeueInputBuffer(5_000)
            if (index >= 0) {
              val packet = next ?: ByteArray(0)
              decoder.getInputBuffer(index)!!.apply { clear(); put(packet) }
              endedInput = next == null
              decoder.queueInputBuffer(index, 0, packet.size, inputSequence * 20_000,
                if (endedInput) MediaCodec.BUFFER_FLAG_END_OF_STREAM else 0)
              if (!endedInput) inputSequence += 1
              next = null
            }
          }
        }
        val index = decoder.dequeueOutputBuffer(info, 5_000)
        if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
          outputRate = decoder.outputFormat.getInteger(MediaFormat.KEY_SAMPLE_RATE)
          check(outputRate in setOf(24_000, 48_000) && decoder.outputFormat.getInteger(MediaFormat.KEY_CHANNEL_COUNT) == 1)
        } else if (index >= 0) {
          if (info.size > 0) {
            val raw = ByteArray(info.size)
            decoder.getOutputBuffer(index)!!.apply { position(info.offset); limit(info.offset + info.size); get(raw) }
            val pcm = if (outputRate == 24_000) raw else {
              check(raw.size % 4 == 0)
              ByteArray(raw.size / 2).also { out ->
                for (i in 0 until raw.size / 4) { out[i * 2] = raw[i * 4]; out[i * 2 + 1] = raw[i * 4 + 1] }
              }
            }
            check(pcm.size % 960 == 0) { "Unexpected decoded packet duration" }
            for (offset in pcm.indices step 960) {
              val skip = minOf(stream.skip, 480)
              stream.skip -= skip
              val values = pcm.copyOfRange(offset + skip * 2, offset + 960)
              stream.decoded += values.size / 2
              check(stream.decoded - stream.rendered <= 48_000) { "Playback reservoir exceeded two seconds" }
              if (values.isNotEmpty()) { write(tail); tail = values }
            }
          }
          outputEnded = info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
          decoder.releaseOutputBuffer(index, false)
        }
      }
      if (currentStream !== stream) return
      val total = stream.total.get()
      check(stream.skip == 0 && total >= writtenFrames && total <= stream.decoded && stream.decoded - total < 480)
      write(tail.copyOfRange(0, ((total - writtenFrames) * 2).toInt()))
      val deadline = SystemClock.elapsedRealtime() + 5_000
      while (currentStream === stream && stream.rendered < total) {
        observe()
        check(SystemClock.elapsedRealtime() < deadline) { "Playback drain timed out" }
        Thread.sleep(5)
      }
      synchronized(this) { if (currentStream === stream) {
        emitPlayback(binding.id, binding.generation, "done", null)
        currentStream = null
        currentResponse = null
        captureSuppressed = false
      } }
    } catch (_: Exception) {
      synchronized(this) { if (currentStream === stream) {
        emitPlayback(binding.id, binding.generation, "failed", "decode_failed")
        currentStream = null
        currentResponse = null
        captureSuppressed = false
        activePlayer.pause()
        activePlayer.flush()
      } }
    } finally {
      runCatching { ownedDecoder?.stop() }
      runCatching { ownedDecoder?.release() }
    }
  }

  @Synchronized private fun cancelCurrent(errorCode: String?) {
    val current = currentResponse ?: return
    val stream = currentStream
    currentResponse = null
    currentStream = null
    captureSuppressed = false
    player?.pause()
    player?.flush()
    emitPlayback(current.id, current.generation, "cancelled", errorCode, stream = stream)
  }

  private fun emitPlayback(responseId: String, generation: Int, state: String, errorCode: String?,
                           timestampMs: Double = SystemClock.elapsedRealtime().toDouble(), stream: PlaybackStream? = currentStream) {
    sendEvent(
      "onPlaybackState",
      bundleOf(
        "responseId" to responseId,
        "generation" to generation,
        "captureEpoch" to (stream?.binding?.captureEpoch ?: captureEpoch),
        "state" to state,
        "monotonicTimestampMs" to timestampMs,
        "errorCode" to errorCode,
        "renderedSamples" to (stream?.rendered ?: 0L),
        "bufferedSamples" to if (state in setOf("done", "cancelled", "failed")) 0L else maxOf(0L, (stream?.decoded ?: 0L) - (stream?.rendered ?: 0L)),
      ),
    )
  }

  private fun capabilities(): Map<String, Any?> {
    val device = audioManager?.communicationDevice
    val isolated = device?.type in setOf(
      AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
      AudioDeviceInfo.TYPE_WIRED_HEADSET,
      AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
      AudioDeviceInfo.TYPE_BLE_HEADSET,
      AudioDeviceInfo.TYPE_USB_HEADSET,
    )
    val speaker = device?.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
    val aecEnabled = echoCanceler?.enabled == true
    val mode = DuplexAudioPolicy.mode(isolated, speaker, aecEnabled)
    val full = mode == DuplexMode.FULL
    return mapOf(
      "mode" to when (mode) {
        DuplexMode.FULL -> "duplex_full"
        DuplexMode.ISOLATED -> "duplex_isolated"
        DuplexMode.HALF -> "duplex_half"
      },
      "input_route" to inputRoute(device),
      "output_route" to outputRoute(device),
      "native_sample_rate" to 16_000,
      "incremental_playback" to true,
      "aec" to mapOf(
        "requested" to speaker,
        "available" to AcousticEchoCanceler.isAvailable(),
        "enabled" to full,
      ),
      "noise_suppression" to mapOf(
        "requested" to !isolated,
        "available" to NoiseSuppressor.isAvailable(),
        "enabled" to (noiseSuppressor?.enabled == true && !isolated),
      ),
      "fallback_reason" to if (!isolated && !full) "aec_unavailable" else null,
    )
  }

  private fun inputRoute(device: AudioDeviceInfo?): String = when (device?.type) {
    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER,
    AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "built_in_mic"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
    AudioDeviceInfo.TYPE_BLE_HEADSET -> "bluetooth_hfp"
    AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired_mic"
    AudioDeviceInfo.TYPE_USB_HEADSET -> "usb"
    else -> "unknown"
  }

  private fun outputRoute(device: AudioDeviceInfo?): String = when (device?.type) {
    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "speakerphone"
    AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "earpiece"
    AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
    AudioDeviceInfo.TYPE_WIRED_HEADSET -> "headphones"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
    AudioDeviceInfo.TYPE_BLE_HEADSET -> "bluetooth_hfp"
    AudioDeviceInfo.TYPE_USB_HEADSET -> "usb"
    else -> "unknown"
  }

  private fun selectCommunicationRoute(manager: AudioManager) {
    if (manager.communicationDevice != null) return
    manager.availableCommunicationDevices
      .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
      ?.let(manager::setCommunicationDevice)
  }

  private fun requestFocus(manager: AudioManager) {
    val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
      .setAudioAttributes(
        AudioAttributes.Builder()
          .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
          .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
          .build()
      )
      .setOnAudioFocusChangeListener { change ->
        if (change <= AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
          suspendForTransition("audio_focus_lost", "playback_unavailable")
        }
      }
      .build()
    focusRequest = request
    manager.requestAudioFocus(request)
  }

  private val deviceCallback = object : AudioDeviceCallback() {
    override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) = routeChanged()
    override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) = routeChanged()
  }

  private val communicationDeviceListener = AudioManager.OnCommunicationDeviceChangedListener {
    routeChanged()
  }

  private fun registerRouteCallbacks(manager: AudioManager) {
    manager.registerAudioDeviceCallback(deviceCallback, null)
    manager.addOnCommunicationDeviceChangedListener(context().mainExecutor, communicationDeviceListener)
  }

  private fun routeChanged() {
    suspendForTransition("route_changed", "route_changed")
  }

  private fun suspendForTransition(reason: String, playbackError: String) {
    if (audioManager == null || recorder == null) return
    val changedCapabilities = capabilities()
    cancelCurrent(playbackError)
    tearDownEngine(restoreRouting = false)
    sendEvent(
      "onRouteChange",
      bundleOf(
        "captureEpoch" to captureEpoch,
        "reason" to reason,
        "capabilities" to changedCapabilities,
      ),
    )
  }

  private fun tearDownEngine(restoreRouting: Boolean): Boolean {
    var restored = true
    capturing.set(false)
    cancelCurrent(null)
    recorder?.runCatching { stop() }
    recorder?.release()
    recorder = null
    opusEncoder?.let { encoder ->
      runCatching { encoder.stop() }
      encoder.release()
    }
    opusEncoder = null
    echoCanceler?.release()
    echoCanceler = null
    noiseSuppressor?.release()
    noiseSuppressor = null
    player?.runCatching { stop() }
    player?.release()
    player = null
    audioManager?.let { manager ->
      runCatching { manager.unregisterAudioDeviceCallback(deviceCallback) }
      runCatching { manager.removeOnCommunicationDeviceChangedListener(communicationDeviceListener) }
      if (restoreRouting) {
        focusRequest?.let {
          if (runCatching { manager.abandonAudioFocusRequest(it) }.isFailure) {
            restored = false
          }
        }
        val deviceRestored = runCatching {
          previousDevice?.let(manager::setCommunicationDevice) ?: run {
            manager.clearCommunicationDevice()
            true
          }
        }.getOrDefault(false)
        restored = restored && deviceRestored
        if (runCatching { manager.mode = previousMode }.isFailure) restored = false
      }
    }
    if (restoreRouting) {
      focusRequest = null
      audioManager = null
      previousDevice = null
    }
    captureSuppressed = false
    return restored
  }

}
