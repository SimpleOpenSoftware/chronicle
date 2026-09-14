import AVFoundation
import ExpoModulesCore

public final class ChronicleDuplexAudioModule: Module {
  private let engine = AVAudioEngine()
  private let player = AVAudioPlayerNode()
  private let controlQueue = DispatchQueue(label: "chronicle.duplex.audio")
  private var converter: AVAudioConverter?
  private var opusConverter: AVAudioConverter?
  private var captureEpoch = 0
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
    Events("onOpusFrame", "onPlaybackState", "onRouteChange")

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
      guard await self.requestRecordPermission() else {
        throw Exception(name: "permission_denied", description: "Microphone permission denied")
      }
      return try await self.onControlQueue {
        try self.startEngine(captureEpoch: epoch)
        return self.capabilities()
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
        self.tearDownEngine(deactivateSession: true)
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

  private func startEngine(captureEpoch: Int) throws {
    tearDownEngine(deactivateSession: false)
    self.captureEpoch = captureEpoch

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
    do {
      try input.setVoiceProcessingEnabled(true)
      voiceProcessingEnabled = input.isVoiceProcessingEnabled
    } catch {
      voiceProcessingEnabled = false
    }

    let inputFormat = input.outputFormat(forBus: 0)
    guard let targetFormat = AVAudioFormat(
      commonFormat: .pcmFormatInt16,
      sampleRate: 16_000,
      channels: 1,
      interleaved: true
    ), let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
      throw Exception(name: "engine_unavailable", description: "Cannot create the 16 kHz PCM converter")
    }
    guard let opusFormat = AVAudioFormat(settings: [
      AVFormatIDKey: kAudioFormatOpus,
      AVSampleRateKey: 16_000,
      AVNumberOfChannelsKey: 1,
      AVEncoderBitRateKey: 24_000,
    ]), let opusConverter = AVAudioConverter(from: targetFormat, to: opusFormat) else {
      throw Exception(name: "engine_unavailable", description: "Cannot create the raw Opus encoder")
    }
    opusConverter.bitRate = 24_000
    self.converter = converter
    self.opusConverter = opusConverter
    let inputFrameCount = AVAudioFrameCount(round(inputFormat.sampleRate * 0.02))
    input.installTap(onBus: 0, bufferSize: inputFrameCount, format: inputFormat) { [weak self] buffer, audioTime in
      self?.emitOpus(buffer, audioTime: audioTime)
    }
    tapInstalled = true
    engine.prepare()
    try engine.start()
    sessionRunning = true
  }

  private func emitOpus(_ input: AVAudioPCMBuffer, audioTime: AVAudioTime) {
    let capturedMonotonicMs = AVAudioTime.seconds(forHostTime: audioTime.hostTime) * 1_000
    let capturedWallMs = Date().timeIntervalSince1970 * 1_000
      - (ProcessInfo.processInfo.systemUptime * 1_000 - capturedMonotonicMs)
    guard !captureSuppressed,
          engine.isRunning,
          let converter,
          let opusConverter else { return }
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
    guard status != .error,
          conversionError == nil,
          output.frameLength > 0 else { return }
    let compressed = AVAudioCompressedBuffer(
      format: opusConverter.outputFormat,
      packetCapacity: 1,
      maximumPacketSize: 1_275
    )
    var opusSupplied = false
    var opusError: NSError?
    let opusStatus = opusConverter.convert(to: compressed, error: &opusError) { _, state in
      if opusSupplied {
        state.pointee = .noDataNow
        return nil
      }
      opusSupplied = true
      state.pointee = .haveData
      return output
    }
    guard opusStatus != .error,
          opusError == nil,
          compressed.packetCount == 1,
          compressed.byteLength > 0 else { return }
    let data = Data(bytes: compressed.data, count: Int(compressed.byteLength))
    let durationMs = Double(output.frameLength) / 16_000 * 1_000
    sendEvent("onOpusFrame", [
      "captureEpoch": captureEpoch,
      "capturedAtMs": capturedWallMs,
      "monotonicTimestampMs": capturedMonotonicMs,
      "sampleRate": 16_000,
      "channels": 1,
      "frameDurationMs": durationMs,
      "opusBase64": data.base64EncodedString(),
    ])
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
    opusConverter = nil
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
      guard let self, self.sessionRunning else { return }
      let changedCapabilities = self.capabilities()
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
