import AVFoundation
import ExpoModulesCore

public final class ChronicleDuplexAudioModule: Module {
  private let engine = AVAudioEngine()
  private let player = AVAudioPlayerNode()
  private let controlQueue = DispatchQueue(label: "chronicle.duplex.audio")
  private let captureDiagnosticLock = NSLock()
  private let captureMetricsLock = NSLock()
  private var converter: AVAudioConverter?
  private var opusEncoder: ChronicleOpusPacketEncoder?
  private var pcmPacketizer: ChroniclePcm16Packetizer?
  private var emittedCaptureDiagnosticStages = Set<String>()
  private var captureEpoch = 0
  private var tapFrameCount = 0
  private var convertedFrameCount = 0
  private var opusPacketCount = 0
  private var opusByteCount = 0
  private var peakAudioLevel = 0.0
  private var systemChangeCount = 0
  private var lastSystemChangeReason = "none"
  private var watchdogEvaluationCount = 0
  private var captureWatchdogGeneration = 0
  private var diagnosticProfile = DuplexDiagnosticProfile.production
  private var voiceProcessingFallbackForced = false
  private var voiceProcessingEnabled = false
  private var captureSuppressed = false
  private var currentResponse: (id: String, generation: Int)?
  private var observers: [NSObjectProtocol] = []
  private var tapInstalled = false
  private var sessionRunning = false
  private var sessionConfigured = false
  private var previousCategory: AVAudioSession.Category?
  private var previousMode: AVAudioSession.Mode?
  private var previousOptions: AVAudioSession.CategoryOptions = []

  public func definition() -> ModuleDefinition {
    Name("ChronicleDuplexAudio")
    Events("onOpusFrame", "onCaptureDiagnostic", "onPlaybackState", "onRouteChange")

    OnCreate { [weak self] in
      self?.installObservers()
    }

    OnDestroy { [weak self] in
      self?.controlQueue.sync {
        self?.tearDownEngine(deactivateSession: true)
      }
      self?.removeObservers()
    }

    AsyncFunction("startVoiceSession") { (options: [String: Any]) async throws -> [String: Any] in
      guard let epoch = options["captureEpoch"] as? Int, epoch >= 0 else {
        throw Exception(name: "invalid_capture_epoch", description: "captureEpoch must be non-negative")
      }
      let profileName = options["diagnosticProfile"] as? String ?? DuplexDiagnosticProfile.production.rawValue
      guard let profile = DuplexDiagnosticProfile(rawValue: profileName) else {
        throw Exception(name: "invalid_diagnostic_profile", description: "Unknown diagnosticProfile")
      }
      guard await self.requestRecordPermission() else {
        throw Exception(name: "permission_denied", description: "Microphone permission denied")
      }
      return try await self.onControlQueue {
        try self.startEngine(captureEpoch: epoch, diagnosticProfile: profile)
        return self.capabilities()
      }
    }

    AsyncFunction("getVoiceSessionDiagnostics") { () async -> [String: Any] in
      await self.onControlQueueValue {
        self.voiceSessionDiagnostics()
      }
    }

    AsyncFunction("scheduleResponse") { (response: [String: Any]) async throws in
      try await self.onControlQueue {
        try self.schedule(response: response)
      }
    }

    AsyncFunction("cancelResponse") { (responseId: String, generation: Int) async in
      await self.onControlQueueNoThrow {
        let current = self.currentResponse.map {
          DuplexResponseBinding(
            id: $0.id,
            generation: $0.generation,
            captureEpoch: self.captureEpoch
          )
        }
        guard DuplexCancellationPolicy.shouldCancel(
          current: current,
          responseId: responseId,
          cancellationGeneration: generation
        ) else { return }
        self.cancelCurrent(errorCode: nil)
      }
    }

    AsyncFunction("stopVoiceSession") { () async -> [String: Any?] in
      let restored = await self.onControlQueueValue {
        let restored = self.tearDownEngine(deactivateSession: true)
        self.voiceProcessingFallbackForced = false
        return restored
      }
      return [
        "restorationSucceeded": restored,
        "failureCode": restored ? nil : "far_field_restore_failed",
      ]
    }
  }

  private func onControlQueue<T>(_ work: @escaping () throws -> T) async throws -> T {
    try await withCheckedThrowingContinuation { continuation in
      controlQueue.async {
        do { continuation.resume(returning: try work()) }
        catch { continuation.resume(throwing: error) }
      }
    }
  }

  private func requestRecordPermission() async -> Bool {
    let session = AVAudioSession.sharedInstance()
    switch session.recordPermission {
    case .granted: return true
    case .denied: return false
    case .undetermined:
      return await withCheckedContinuation { continuation in
        session.requestRecordPermission { continuation.resume(returning: $0) }
      }
    @unknown default: return false
    }
  }

  private func onControlQueueNoThrow(_ work: @escaping () -> Void) async {
    await withCheckedContinuation { continuation in
      controlQueue.async {
        work()
        continuation.resume()
      }
    }
  }

  private func onControlQueueValue<T>(_ work: @escaping () -> T) async -> T {
    await withCheckedContinuation { continuation in
      controlQueue.async {
        continuation.resume(returning: work())
      }
    }
  }

  private func startEngine(
    captureEpoch: Int,
    diagnosticProfile: DuplexDiagnosticProfile
  ) throws {
    tearDownEngine(deactivateSession: false)
    self.captureEpoch = captureEpoch
    self.diagnosticProfile = diagnosticProfile
    captureDiagnosticLock.lock()
    emittedCaptureDiagnosticStages.removeAll()
    captureDiagnosticLock.unlock()

    let session = AVAudioSession.sharedInstance()
    if !sessionConfigured {
      previousCategory = session.category
      previousMode = session.mode
      previousOptions = session.categoryOptions
      sessionConfigured = true
    }
    try session.setCategory(
      .playAndRecord,
      mode: .voiceChat,
      options: [.allowBluetoothHFP, .defaultToSpeaker]
    )
    try session.setPreferredIOBufferDuration(0.02)
    try session.setActive(true)

    engine.attach(player)
    let outputFormat = engine.outputNode.outputFormat(forBus: 0)
    engine.connect(player, to: engine.mainMixerNode, format: outputFormat)

    let input = engine.inputNode
    if let forcedVoiceProcessing = diagnosticProfile.forcedVoiceProcessing {
      if forcedVoiceProcessing {
        do {
          try input.setVoiceProcessingEnabled(true)
          voiceProcessingEnabled = input.isVoiceProcessingEnabled
        } catch {
          voiceProcessingEnabled = false
          emitCaptureDiagnostic(
            stage: "capture_failed",
            detail: "voice processing could not be enabled: \(error)"
          )
        }
      } else {
        try? input.setVoiceProcessingEnabled(false)
        voiceProcessingEnabled = false
      }
    } else if voiceProcessingFallbackForced {
      try? input.setVoiceProcessingEnabled(false)
      voiceProcessingEnabled = false
    } else {
      do {
        try input.setVoiceProcessingEnabled(true)
        voiceProcessingEnabled = input.isVoiceProcessingEnabled
      } catch {
        voiceProcessingEnabled = false
      }
    }

    // iOS input taps must be installed with the hardware input format. After
    // VoiceProcessingIO is enabled, outputFormat can produce a running engine
    // whose input tap never receives a buffer.
    let inputFormat = input.inputFormat(forBus: 0)
    let opusEncoder: ChronicleOpusPacketEncoder
    do {
      opusEncoder = try ChronicleOpusPacketEncoder()
    } catch {
      throw Exception(name: "engine_unavailable", description: "Cannot create the raw Opus encoder: \(error)")
    }
    guard let converter = AVAudioConverter(from: inputFormat, to: opusEncoder.inputFormat) else {
      throw Exception(name: "engine_unavailable", description: "Cannot create the 16 kHz PCM converter")
    }
    self.converter = converter
    self.opusEncoder = opusEncoder
    self.pcmPacketizer = ChroniclePcm16Packetizer()
    resetCaptureMetrics()
    let inputFrameCount = AVAudioFrameCount(round(inputFormat.sampleRate * 0.02))
    let tapFormat: AVAudioFormat? = diagnosticProfile.usesSystemTapFormat ? nil : inputFormat
    input.installTap(onBus: 0, bufferSize: inputFrameCount, format: tapFormat) { [weak self] buffer, audioTime in
      self?.observeTapFrame()
      self?.emitCaptureDiagnostic(stage: "tap_received")
      self?.emitOpus(buffer, audioTime: audioTime)
    }
    tapInstalled = true
    engine.prepare()
    try engine.start()
    sessionRunning = true
    scheduleCaptureWatchdog()
  }

  private func emitOpus(_ input: AVAudioPCMBuffer, audioTime: AVAudioTime) {
    let capturedMonotonicMs = AVAudioTime.seconds(forHostTime: audioTime.hostTime) * 1_000
    let capturedWallMs = Date().timeIntervalSince1970 * 1_000
      - (ProcessInfo.processInfo.systemUptime * 1_000 - capturedMonotonicMs)
    guard !captureSuppressed,
          engine.isRunning,
          let converter,
          let opusEncoder,
          let pcmPacketizer else { return }
    let capacity = ChronicleDuplexResampler.outputCapacity(
      inputFrames: input.frameLength,
      inputRate: input.format.sampleRate
    )
    guard let output = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity) else {
      return
    }
    var supplied = false
    var conversionError: NSError?
    let status = converter.convert(to: output, error: &conversionError) { _, state in
      if supplied {
        state.pointee = .noDataNow
        return nil
      }
      supplied = true
      state.pointee = .haveData
      return input
    }
    guard status != .error, conversionError == nil else {
      emitCaptureDiagnostic(
        stage: "pcm_conversion_failed",
        detail: conversionError?.localizedDescription ?? "converter_status_error"
      )
      return
    }
    guard output.frameLength > 0 else {
      emitCaptureDiagnostic(stage: "pcm_empty")
      return
    }
    emitCaptureDiagnostic(stage: "pcm_converted", frameCount: Int(output.frameLength))
    observeConvertedFrames(Int(output.frameLength))
    guard let samples = output.int16ChannelData?[0] else {
      emitCaptureDiagnostic(stage: "pcm_conversion_failed", detail: "16 kHz PCM samples unavailable")
      return
    }
    let pendingDurationMs = Double(pcmPacketizer.pendingSampleCount) / 16_000 * 1_000
    let packets = pcmPacketizer.append(samples: samples, count: Int(output.frameLength))
    let durationMs = 20.0
    for (index, packet) in packets.enumerated() {
      let data: Data
      do {
        data = try opusEncoder.encode(samples: packet)
      } catch {
        emitCaptureDiagnostic(stage: "opus_encode_failed", detail: String(describing: error))
        return
      }
      emitCaptureDiagnostic(stage: "opus_encoded", frameCount: packet.count, byteCount: data.count)
      let packetOffsetMs = Double(index) * durationMs - pendingDurationMs
      let audioLevel = packet.withUnsafeBufferPointer {
        ChronicleAudioMeter.level(samples: $0.baseAddress!, count: $0.count)
      }
      observeEncodedPacket(byteCount: data.count, audioLevel: audioLevel)
      sendEvent("onOpusFrame", [
        "captureEpoch": captureEpoch,
        "capturedAtMs": capturedWallMs + packetOffsetMs,
        "monotonicTimestampMs": capturedMonotonicMs + packetOffsetMs,
        "sampleRate": 16_000,
        "channels": 1,
        "frameDurationMs": durationMs,
        "audioLevel": audioLevel,
        "opusBase64": data.base64EncodedString(),
      ])
    }
  }

  private func resetCaptureMetrics() {
    captureMetricsLock.lock()
    tapFrameCount = 0
    convertedFrameCount = 0
    opusPacketCount = 0
    opusByteCount = 0
    peakAudioLevel = 0
    systemChangeCount = 0
    lastSystemChangeReason = "none"
    watchdogEvaluationCount = 0
    captureMetricsLock.unlock()
  }

  private func observeTapFrame() {
    captureMetricsLock.lock()
    tapFrameCount += 1
    captureMetricsLock.unlock()
  }

  private func capturedTapFrameCount() -> Int {
    captureMetricsLock.lock()
    let count = tapFrameCount
    captureMetricsLock.unlock()
    return count
  }

  private func observeConvertedFrames(_ count: Int) {
    captureMetricsLock.lock()
    convertedFrameCount += count
    captureMetricsLock.unlock()
  }

  private func observeEncodedPacket(byteCount: Int, audioLevel: Double) {
    captureMetricsLock.lock()
    opusPacketCount += 1
    opusByteCount += byteCount
    peakAudioLevel = max(peakAudioLevel, audioLevel)
    captureMetricsLock.unlock()
  }

  private func observeSystemChange(_ reason: String) {
    captureMetricsLock.lock()
    systemChangeCount += 1
    lastSystemChangeReason = reason
    captureMetricsLock.unlock()
  }

  private func observeWatchdogEvaluation() {
    captureMetricsLock.lock()
    watchdogEvaluationCount += 1
    captureMetricsLock.unlock()
  }

  private func scheduleCaptureWatchdog() {
    captureWatchdogGeneration += 1
    let generation = captureWatchdogGeneration
    let epoch = captureEpoch
    controlQueue.asyncAfter(deadline: .now() + 1.5) { [weak self] in
      guard let self,
            self.sessionRunning,
            self.captureEpoch == epoch,
            self.captureWatchdogGeneration == generation else { return }
      let tapCount = self.capturedTapFrameCount()
      self.observeWatchdogEvaluation()
      let action = DuplexCaptureWatchdog.recoveryAction(
        tapFrameCount: tapCount,
        voiceProcessingEnabled: self.voiceProcessingEnabled
      )
      self.emitCaptureDiagnostic(
        stage: "watchdog_evaluated",
        detail: "profile=\(self.diagnosticProfile.rawValue) taps=\(tapCount) action=\(String(describing: action))"
      )
      if self.diagnosticProfile != .production {
        if tapCount == 0 {
          self.emitCaptureDiagnostic(
            stage: "capture_failed",
            detail: "diagnostic profile produced no input tap after 1500ms"
          )
        }
        return
      }
      switch action {
      case .none:
        return
      case .disableVoiceProcessing:
        self.voiceProcessingFallbackForced = true
        self.emitCaptureDiagnostic(
          stage: "voice_processing_fallback",
          detail: "no input tap after 1500ms taps=\(tapCount)"
        )
        self.tearDownEngine(deactivateSession: false)
        let payload: [String: Any] = [
          "captureEpoch": epoch,
          "reason": "effect_failed",
          "capabilities": self.capabilities(),
        ]
        DispatchQueue.main.async { [weak self] in
          self?.sendEvent("onRouteChange", payload)
        }
      case .reportFailure:
        self.emitCaptureDiagnostic(
          stage: "capture_failed",
          detail: "no input tap after voice-processing fallback taps=\(tapCount)"
        )
      }
    }
  }

  private func emitCaptureDiagnostic(
    stage: String,
    frameCount: Int? = nil,
    byteCount: Int? = nil,
    detail: String? = nil
  ) {
    captureDiagnosticLock.lock()
    let inserted = emittedCaptureDiagnosticStages.insert(stage).inserted
    captureDiagnosticLock.unlock()
    guard inserted else { return }
    var payload: [String: Any] = [
      "captureEpoch": captureEpoch,
      "stage": stage,
      "monotonicTimestampMs": ProcessInfo.processInfo.systemUptime * 1_000,
    ]
    if let frameCount { payload["frameCount"] = frameCount }
    if let byteCount { payload["byteCount"] = byteCount }
    if let detail { payload["detail"] = String(detail.prefix(240)) }
    DispatchQueue.main.async { [weak self] in
      self?.sendEvent("onCaptureDiagnostic", payload)
    }
  }

  private func schedule(response: [String: Any]) throws {
    guard let responseId = response["responseId"] as? String,
          let generation = response["generation"] as? Int,
          let epoch = response["captureEpoch"] as? Int,
          epoch == captureEpoch,
          let encodedPackets = response["opusPacketsBase64"] as? [String],
          !encodedPackets.isEmpty else {
      throw Exception(name: "decode_failed", description: "Response binding or Opus packets are invalid")
    }
    guard engine.isRunning else {
      throw Exception(name: "playback_unavailable", description: "Audio engine is not running")
    }
    cancelCurrent(errorCode: nil)
    let packets = try encodedPackets.map { value -> Data in
      guard let packet = Data(base64Encoded: value), !packet.isEmpty else {
        throw Exception(name: "decode_failed", description: "Response contains an invalid Opus packet")
      }
      return packet
    }
    guard let opusFormat = AVAudioFormat(settings: [
      AVFormatIDKey: kAudioFormatOpus,
      AVSampleRateKey: 24_000,
      AVNumberOfChannelsKey: 1,
    ]) else {
      throw Exception(name: "decode_failed", description: "Cannot create the Opus playback format")
    }
    let outputFormat = engine.mainMixerNode.outputFormat(forBus: 0)
    guard let decoder = AVAudioConverter(from: opusFormat, to: outputFormat) else {
      throw Exception(name: "decode_failed", description: "Cannot create the Opus playback decoder")
    }
    let decoded = try packets.map { packet in
      try decodePlaybackPacket(packet, decoder: decoder, opusFormat: opusFormat, outputFormat: outputFormat)
    }
    currentResponse = (responseId, generation)
    captureSuppressed = capabilities()["mode"] as? String == "duplex_half"
    for (index, buffer) in decoded.enumerated() {
      let isFirst = index == 0
      let isLast = index == decoded.count - 1
      let bufferDurationMs = Double(buffer.frameLength) / buffer.format.sampleRate * 1_000
      player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
        // Capture the observation before dispatching across the control queue.
        // Apple includes downstream/device latency in dataPlayedBack. Subtracting
        // the first buffer estimates its onset; callback jitter remains explicit
        // in the report's estimated quality.
        let completedMs = ProcessInfo.processInfo.systemUptime * 1_000
        guard isFirst || isLast else { return }
        self?.controlQueue.async {
          guard let self,
                let current = self.currentResponse,
                current.id == responseId,
                current.generation == generation else { return }
          if isFirst {
            self.emitPlayback(responseId, generation, state: "started", errorCode: nil,
                              timestampMs: completedMs - bufferDurationMs)
          }
          if isLast {
            self.currentResponse = nil
            self.captureSuppressed = false
            self.emitPlayback(responseId, generation, state: "done", errorCode: nil,
                              timestampMs: completedMs)
          }
        }
      }
    }
    player.play()
  }

  private func decodePlaybackPacket(
    _ packet: Data,
    decoder: AVAudioConverter,
    opusFormat: AVAudioFormat,
    outputFormat: AVAudioFormat
  ) throws -> AVAudioPCMBuffer {
    let compressed = AVAudioCompressedBuffer(
      format: opusFormat,
      packetCapacity: 1,
      maximumPacketSize: packet.count
    )
    packet.copyBytes(to: compressed.data.assumingMemoryBound(to: UInt8.self), count: packet.count)
    compressed.byteLength = UInt32(packet.count)
    compressed.packetCount = 1
    if let descriptions = compressed.packetDescriptions {
      descriptions[0] = AudioStreamPacketDescription(
        mStartOffset: 0,
        mVariableFramesInPacket: 480,
        mDataByteSize: UInt32(packet.count)
      )
    }
    let capacity = AVAudioFrameCount(ceil(outputFormat.sampleRate / 50.0))
    guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
      throw Exception(name: "decode_failed", description: "Cannot allocate playback PCM")
    }
    var supplied = false
    var conversionError: NSError?
    let status = decoder.convert(to: output, error: &conversionError) { _, state in
      if supplied {
        state.pointee = .noDataNow
        return nil
      }
      supplied = true
      state.pointee = .haveData
      return compressed
    }
    guard status != .error, conversionError == nil, output.frameLength > 0 else {
      throw Exception(name: "decode_failed", description: "Cannot decode an Opus playback packet")
    }
    return output
  }

  private func cancelCurrent(errorCode: String?) {
    guard let current = currentResponse else { return }
    player.stop()
    currentResponse = nil
    captureSuppressed = false
    emitPlayback(current.id, current.generation, state: "cancelled", errorCode: errorCode)
  }

  private func emitPlayback(
    _ responseId: String,
    _ generation: Int,
    state: String,
    errorCode: String?,
    timestampMs: Double? = nil
  ) {
    sendEvent("onPlaybackState", [
      "responseId": responseId,
      "generation": generation,
      "captureEpoch": captureEpoch,
      "state": state,
      "monotonicTimestampMs": timestampMs ?? ProcessInfo.processInfo.systemUptime * 1_000,
      "errorCode": errorCode as Any,
    ])
  }

  private func capabilities() -> [String: Any] {
    let route = AVAudioSession.sharedInstance().currentRoute
    let input = route.inputs.first?.portType
    let output = route.outputs.first?.portType
    let isolated = output == .headphones || output == .bluetoothHFP || output == .usbAudio
    let speaker = output == .builtInSpeaker
    let full = speaker && voiceProcessingEnabled
    let mode = isolated ? "duplex_isolated" : (full ? "duplex_full" : "duplex_half")
    let aecAvailable = voiceProcessingEnabled && speaker
    return [
      "mode": mode,
      "input_route": inputRoute(input),
      "output_route": outputRoute(output),
      "native_sample_rate": Int(AVAudioSession.sharedInstance().sampleRate),
      "aec": effect(requested: speaker, available: aecAvailable, enabled: full),
      "noise_suppression": effect(
        requested: !isolated,
        available: voiceProcessingEnabled,
        enabled: voiceProcessingEnabled && !isolated
      ),
      "fallback_reason": (!isolated && !full) ? "aec_unavailable" : NSNull(),
    ]
  }

  private func voiceSessionDiagnostics() -> [String: Any] {
    captureMetricsLock.lock()
    let metrics: [String: Any] = [
      "tapFrameCount": tapFrameCount,
      "convertedFrameCount": convertedFrameCount,
      "opusPacketCount": opusPacketCount,
      "opusByteCount": opusByteCount,
      "peakAudioLevel": peakAudioLevel,
      "systemChangeCount": systemChangeCount,
      "lastSystemChangeReason": lastSystemChangeReason,
      "watchdogEvaluationCount": watchdogEvaluationCount,
    ]
    captureMetricsLock.unlock()

    let session = AVAudioSession.sharedInstance()
    let inputFormat = engine.inputNode.inputFormat(forBus: 0)
    let outputFormat = engine.outputNode.outputFormat(forBus: 0)
    return metrics.merging([
      "diagnosticProfile": diagnosticProfile.rawValue,
      "captureEpoch": captureEpoch,
      "engineRunning": engine.isRunning,
      "sessionRunning": sessionRunning,
      "tapInstalled": tapInstalled,
      "voiceProcessingEnabled": voiceProcessingEnabled,
      "audioSessionCategory": session.category.rawValue,
      "audioSessionMode": session.mode.rawValue,
      "audioSessionSampleRate": session.sampleRate,
      "audioSessionIOBufferDurationMs": session.ioBufferDuration * 1_000,
      "inputFormat": formatSummary(inputFormat),
      "outputFormat": formatSummary(outputFormat),
    ]) { _, newest in newest }
  }

  private func formatSummary(_ format: AVAudioFormat) -> String {
    let commonFormat: String
    switch format.commonFormat {
    case .pcmFormatFloat32: commonFormat = "float32"
    case .pcmFormatFloat64: commonFormat = "float64"
    case .pcmFormatInt16: commonFormat = "int16"
    case .pcmFormatInt32: commonFormat = "int32"
    case .otherFormat: commonFormat = "other"
    @unknown default: commonFormat = "unknown"
    }
    return "\(Int(format.sampleRate))Hz/\(format.channelCount)ch/\(commonFormat)/\(format.isInterleaved ? "interleaved" : "noninterleaved")"
  }

  private func effect(requested: Bool, available: Bool, enabled: Bool) -> [String: Bool] {
    ["requested": requested, "available": available, "enabled": enabled]
  }

  private func inputRoute(_ port: AVAudioSession.Port?) -> String {
    switch port {
    case .builtInMic: return "built_in_mic"
    case .bluetoothHFP: return "bluetooth_hfp"
    case .headsetMic: return "wired_mic"
    case .usbAudio: return "usb"
    default: return "unknown"
    }
  }

  private func outputRoute(_ port: AVAudioSession.Port?) -> String {
    switch port {
    case .builtInSpeaker: return "speakerphone"
    case .builtInReceiver: return "earpiece"
    case .headphones: return "headphones"
    case .bluetoothHFP: return "bluetooth_hfp"
    case .usbAudio: return "usb"
    default: return "unknown"
    }
  }

  @discardableResult
  private func tearDownEngine(deactivateSession: Bool) -> Bool {
    var restored = true
    captureWatchdogGeneration += 1
    cancelCurrent(errorCode: nil)
    sessionRunning = false
    if tapInstalled {
      engine.inputNode.removeTap(onBus: 0)
      tapInstalled = false
    }
    player.stop()
    engine.stop()
    if player.engine != nil { engine.detach(player) }
    converter = nil
    opusEncoder = nil
    pcmPacketizer = nil
    voiceProcessingEnabled = false
    captureSuppressed = false
    if deactivateSession {
      let session = AVAudioSession.sharedInstance()
      if let previousCategory, let previousMode {
        do {
          try session.setCategory(
            previousCategory,
            mode: previousMode,
            options: previousOptions
          )
        } catch {
          restored = false
        }
      }
      do {
        try session.setActive(false, options: .notifyOthersOnDeactivation)
      } catch {
        restored = false
      }
      sessionConfigured = false
      previousCategory = nil
      previousMode = nil
      previousOptions = []
    }
    return restored
  }

  private func installObservers() {
    let center = NotificationCenter.default
    observers.append(center.addObserver(
      forName: AVAudioSession.routeChangeNotification,
      object: nil,
      queue: nil
    ) { [weak self] _ in self?.handleSystemChange(reason: "route_changed", errorCode: "route_changed") })
    observers.append(center.addObserver(
      forName: AVAudioSession.interruptionNotification,
      object: nil,
      queue: nil
    ) { [weak self] _ in self?.handleSystemChange(reason: "interruption", errorCode: "playback_unavailable") })
    observers.append(center.addObserver(
      forName: NSNotification.Name.AVAudioEngineConfigurationChange,
      object: engine,
      queue: nil
    ) { [weak self] _ in self?.handleSystemChange(reason: "engine_reset", errorCode: "engine_reset") })
  }

  private func handleSystemChange(reason: String, errorCode: String) {
    controlQueue.async { [weak self] in
      guard let self else { return }
      self.observeSystemChange(reason)
      let held = DuplexSystemChangePolicy.shouldHoldEngine(
        reason: reason,
        sessionRunning: self.sessionRunning,
        diagnosticProfile: self.diagnosticProfile
      )
      self.emitCaptureDiagnostic(
        stage: "system_change",
        detail: "reason=\(reason) session_running=\(self.sessionRunning) held=\(held) engine_running=\(self.engine.isRunning)"
      )
      guard self.sessionRunning else { return }
      let changedCapabilities = self.capabilities()
      if held {
        self.sendEvent("onRouteChange", [
          "captureEpoch": self.captureEpoch,
          "reason": reason,
          "capabilities": changedCapabilities,
        ])
        return
      }
      self.cancelCurrent(errorCode: errorCode)
      self.tearDownEngine(deactivateSession: false)
      self.sendEvent("onRouteChange", [
        "captureEpoch": self.captureEpoch,
        "reason": reason,
        "capabilities": changedCapabilities,
      ])
    }
  }

  private func removeObservers() {
    observers.forEach(NotificationCenter.default.removeObserver)
    observers.removeAll()
  }
}
