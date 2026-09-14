import AVFoundation

enum DuplexAudioStateError: Error {
  case staleEpoch
  case responseAlreadyScheduled
}

enum ChronicleOpusEncoderError: Error {
  case formatUnavailable
  case converterUnavailable
  case conversionFailed(String)
  case packetUnavailable
}

struct DuplexResponseBinding: Equatable {
  let id: String
  let generation: Int
  let captureEpoch: Int
}

enum DuplexCancellationPolicy {
  static func shouldCancel(
    current: DuplexResponseBinding?,
    responseId: String,
    cancellationGeneration: Int
  ) -> Bool {
    guard let current,
          responseId == "*" || responseId == current.id else { return false }
    return cancellationGeneration >= current.generation
  }
}

final class DuplexAudioState {
  private(set) var captureEpoch = -1
  private(set) var response: DuplexResponseBinding?
  private(set) var running = false

  func start(captureEpoch: Int) {
    self.captureEpoch = captureEpoch
    response = nil
    running = true
  }

  func schedule(_ next: DuplexResponseBinding) throws {
    guard running, next.captureEpoch == captureEpoch else {
      throw DuplexAudioStateError.staleEpoch
    }
    guard response == nil else {
      throw DuplexAudioStateError.responseAlreadyScheduled
    }
    response = next
  }

  @discardableResult
  func cancel() -> DuplexResponseBinding? {
    defer { response = nil }
    return response
  }

  @discardableResult
  func systemChanged() -> DuplexResponseBinding? {
    cancel()
  }

  func stop() {
    response = nil
    running = false
  }
}

enum ChronicleDuplexResampler {
  static func outputCapacity(
    inputFrames: AVAudioFrameCount,
    inputRate: Double,
    outputRate: Double = 16_000
  ) -> AVAudioFrameCount {
    AVAudioFrameCount(max(1, ceil(Double(inputFrames) * outputRate / inputRate)))
  }
}

enum ChronicleAudioMeter {
  static func level(samples: UnsafePointer<Int16>, count: Int) -> Double {
    guard count > 0 else { return 0 }
    var sumOfSquares = 0.0
    for index in 0..<count {
      let normalized = Double(samples[index]) / 32_768.0
      sumOfSquares += normalized * normalized
    }
    return min(1, sqrt(sumOfSquares / Double(count)))
  }
}

enum DuplexCaptureRecoveryAction: Equatable {
  case none
  case disableVoiceProcessing
  case reportFailure
}

enum DuplexCaptureWatchdog {
  static func recoveryAction(
    tapFrameCount: Int,
    voiceProcessingEnabled: Bool
  ) -> DuplexCaptureRecoveryAction {
    guard tapFrameCount == 0 else { return .none }
    return voiceProcessingEnabled ? .disableVoiceProcessing : .reportFailure
  }
}

enum DuplexSystemChangePolicy {
  static func shouldHoldEngine(
    reason: String,
    sessionRunning: Bool,
    diagnosticProfile: DuplexDiagnosticProfile
  ) -> Bool {
    guard sessionRunning else { return false }
    if diagnosticProfile.holdsEngineOnSystemChange { return true }

    // AVAudioSession emits route-change notifications while the initial
    // play-and-record route is settling. Tearing down here leaves the engine
    // looking successfully started but prevents its input tap from ever
    // delivering a frame. A later, real route change is still forwarded to JS,
    // whose bound capture lifecycle performs the restart.
    return reason == "route_changed"
  }
}

enum DuplexDiagnosticProfile: String, CaseIterable {
  case production
  case voiceProcessingHold = "voice_processing_hold"
  case plainCaptureHold = "plain_capture_hold"
  case systemTapFormatHold = "system_tap_format_hold"

  var forcedVoiceProcessing: Bool? {
    switch self {
    case .production: return nil
    case .voiceProcessingHold, .systemTapFormatHold: return true
    case .plainCaptureHold: return false
    }
  }

  var usesSystemTapFormat: Bool {
    self == .systemTapFormatHold
  }

  var holdsEngineOnSystemChange: Bool {
    self != .production
  }
}

final class ChroniclePcm16Packetizer {
  private(set) var pendingSampleCount = 0
  private var pending: [Int16] = []

  func append(samples: UnsafePointer<Int16>, count: Int) -> [[Int16]] {
    guard count > 0 else { return [] }
    pending.append(contentsOf: UnsafeBufferPointer(start: samples, count: count))
    var packets: [[Int16]] = []
    let packetSize = Int(ChronicleOpusPacketEncoder.framesPerPacket)
    let packetCount = pending.count / packetSize
    for packetIndex in 0..<packetCount {
      let start = packetIndex * packetSize
      packets.append(Array(pending[start..<(start + packetSize)]))
    }
    pending.removeFirst(packetCount * packetSize)
    pendingSampleCount = pending.count
    return packets
  }
}

final class ChronicleOpusPacketEncoder {
  static let sampleRate = 16_000.0
  static let framesPerPacket: AVAudioFrameCount = 320

  let inputFormat: AVAudioFormat
  let outputFormat: AVAudioFormat
  private let converter: AVAudioConverter

  init(bitRate: Int = 24_000) throws {
    guard let inputFormat = AVAudioFormat(
      commonFormat: .pcmFormatInt16,
      sampleRate: Self.sampleRate,
      channels: 1,
      interleaved: true
    ) else {
      throw ChronicleOpusEncoderError.formatUnavailable
    }
    var description = AudioStreamBasicDescription(
      mSampleRate: Self.sampleRate,
      mFormatID: kAudioFormatOpus,
      mFormatFlags: 0,
      mBytesPerPacket: 0,
      mFramesPerPacket: UInt32(Self.framesPerPacket),
      mBytesPerFrame: 0,
      mChannelsPerFrame: 1,
      mBitsPerChannel: 0,
      mReserved: 0
    )
    guard let outputFormat = AVAudioFormat(streamDescription: &description),
          let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
      throw ChronicleOpusEncoderError.converterUnavailable
    }
    converter.bitRate = bitRate
    converter.primeMethod = .none
    self.inputFormat = inputFormat
    self.outputFormat = outputFormat
    self.converter = converter
  }

  func encode(_ input: AVAudioPCMBuffer) throws -> Data {
    guard input.format == inputFormat,
          input.frameLength == Self.framesPerPacket else {
      throw ChronicleOpusEncoderError.formatUnavailable
    }
    let maximumPacketSize = max(1, converter.maximumOutputPacketSize)
    let compressed = AVAudioCompressedBuffer(
      format: outputFormat,
      packetCapacity: 1,
      maximumPacketSize: maximumPacketSize
    )
    var supplied = false
    var conversionError: NSError?
    let status = converter.convert(to: compressed, error: &conversionError) { _, state in
      if supplied {
        state.pointee = .noDataNow
        return nil
      }
      supplied = true
      state.pointee = .haveData
      return input
    }
    if status == .error || conversionError != nil {
      throw ChronicleOpusEncoderError.conversionFailed(
        conversionError?.localizedDescription ?? "converter_status_error"
      )
    }
    guard compressed.packetCount == 1, compressed.byteLength > 0 else {
      throw ChronicleOpusEncoderError.packetUnavailable
    }
    return Data(bytes: compressed.data, count: Int(compressed.byteLength))
  }

  func encode(samples: [Int16]) throws -> Data {
    guard samples.count == Int(Self.framesPerPacket),
          let input = AVAudioPCMBuffer(
            pcmFormat: inputFormat,
            frameCapacity: Self.framesPerPacket
          ),
          let target = input.int16ChannelData?[0] else {
      throw ChronicleOpusEncoderError.formatUnavailable
    }
    input.frameLength = Self.framesPerPacket
    samples.withUnsafeBufferPointer { source in
      target.update(from: source.baseAddress!, count: source.count)
    }
    return try encode(input)
  }
}
