import AVFoundation
import XCTest
@testable import ChronicleDuplexAudio

final class DuplexAudioStateTests: XCTestCase {
  func testEngineStateRejectsResponseFromPreviousEpoch() throws {
    let state = DuplexAudioState()
    state.start(captureEpoch: 4)
    XCTAssertThrowsError(try state.schedule(.init(id: "old", generation: 1, captureEpoch: 3)))
  }

  func testOnlyOneResponseCanBeScheduled() throws {
    let state = DuplexAudioState()
    state.start(captureEpoch: 4)
    try state.schedule(.init(id: "one", generation: 1, captureEpoch: 4))
    XCTAssertThrowsError(try state.schedule(.init(id: "two", generation: 1, captureEpoch: 4)))
  }

  func testNativeCancellationReturnsExactAcknowledgedBinding() throws {
    let state = DuplexAudioState()
    let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
    state.start(captureEpoch: 4)
    try state.schedule(response)
    XCTAssertEqual(state.cancel(), response)
    XCTAssertNil(state.cancel())
  }

  func testNewGenerationCancellationStopsOlderPlayingResponse() {
    let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
    XCTAssertTrue(
      DuplexCancellationPolicy.shouldCancel(
        current: response,
        responseId: "one",
        cancellationGeneration: 8
      )
    )
  }

  func testStaleOrMismatchedCancellationCannotStopCurrentResponse() {
    let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
    XCTAssertFalse(
      DuplexCancellationPolicy.shouldCancel(
        current: response,
        responseId: "one",
        cancellationGeneration: 6
      )
    )
    XCTAssertFalse(
      DuplexCancellationPolicy.shouldCancel(
        current: response,
        responseId: "other",
        cancellationGeneration: 8
      )
    )
  }

  func testRouteInterruptionAndResetFlushScheduledResponse() throws {
    for _ in 0..<3 {
      let state = DuplexAudioState()
      let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
      state.start(captureEpoch: 4)
      try state.schedule(response)
      XCTAssertEqual(state.systemChanged(), response)
      XCTAssertNil(state.response)
    }
  }

  func testResamplingCapacityRetainsTwentyMilliseconds() {
    XCTAssertEqual(
      ChronicleDuplexResampler.outputCapacity(inputFrames: 960, inputRate: 48_000),
      320
    )
  }

  func testAudioMeterReportsSilenceAndNormalizedPeak() {
    let silence = [Int16](repeating: 0, count: 320)
    let halfScale = [Int16](repeating: 16_384, count: 320)
    silence.withUnsafeBufferPointer { samples in
      XCTAssertEqual(ChronicleAudioMeter.level(samples: samples.baseAddress!, count: samples.count), 0)
    }
    halfScale.withUnsafeBufferPointer { samples in
      XCTAssertEqual(
        ChronicleAudioMeter.level(samples: samples.baseAddress!, count: samples.count),
        0.5,
        accuracy: 0.001
      )
    }
  }

  func testCaptureWatchdogFallsBackWhenVoiceProcessingTapIsSilent() {
    XCTAssertEqual(
      DuplexCaptureWatchdog.recoveryAction(
        tapFrameCount: 0,
        voiceProcessingEnabled: true
      ),
      .disableVoiceProcessing
    )
  }

  func testCaptureWatchdogLeavesDeliveringTapAlone() {
    XCTAssertEqual(
      DuplexCaptureWatchdog.recoveryAction(
        tapFrameCount: 1,
        voiceProcessingEnabled: true
      ),
      .none
    )
  }

  func testCaptureWatchdogReportsFailureAfterFallbackTapIsSilent() {
    XCTAssertEqual(
      DuplexCaptureWatchdog.recoveryAction(
        tapFrameCount: 0,
        voiceProcessingEnabled: false
      ),
      .reportFailure
    )
  }

  func testDiagnosticMatrixChangesOneCaptureVariableAtATime() {
    XCTAssertNil(DuplexDiagnosticProfile.production.forcedVoiceProcessing)
    XCTAssertFalse(DuplexDiagnosticProfile.production.holdsEngineOnSystemChange)

    XCTAssertEqual(DuplexDiagnosticProfile.voiceProcessingHold.forcedVoiceProcessing, true)
    XCTAssertTrue(DuplexDiagnosticProfile.voiceProcessingHold.holdsEngineOnSystemChange)
    XCTAssertFalse(DuplexDiagnosticProfile.voiceProcessingHold.usesSystemTapFormat)

    XCTAssertEqual(DuplexDiagnosticProfile.plainCaptureHold.forcedVoiceProcessing, false)
    XCTAssertTrue(DuplexDiagnosticProfile.plainCaptureHold.holdsEngineOnSystemChange)
    XCTAssertFalse(DuplexDiagnosticProfile.plainCaptureHold.usesSystemTapFormat)

    XCTAssertEqual(DuplexDiagnosticProfile.systemTapFormatHold.forcedVoiceProcessing, true)
    XCTAssertTrue(DuplexDiagnosticProfile.systemTapFormatHold.holdsEngineOnSystemChange)
    XCTAssertTrue(DuplexDiagnosticProfile.systemTapFormatHold.usesSystemTapFormat)
  }

  func testProductionKeepsCapturingThroughInitialRouteSettlement() {
    XCTAssertTrue(
      DuplexSystemChangePolicy.shouldHoldEngine(
        reason: "route_changed",
        sessionRunning: true,
        diagnosticProfile: .production
      )
    )
  }

  func testProductionStillRestartsForEngineResetAndInterruption() {
    for reason in ["engine_reset", "interruption"] {
      XCTAssertFalse(
        DuplexSystemChangePolicy.shouldHoldEngine(
          reason: reason,
          sessionRunning: true,
          diagnosticProfile: .production
        )
      )
    }
  }

  func testDiagnosticHoldProfilesCanObserveEverySystemChange() {
    XCTAssertTrue(
      DuplexSystemChangePolicy.shouldHoldEngine(
        reason: "engine_reset",
        sessionRunning: true,
        diagnosticProfile: .voiceProcessingHold
      )
    )
    XCTAssertFalse(
      DuplexSystemChangePolicy.shouldHoldEngine(
        reason: "route_changed",
        sessionRunning: false,
        diagnosticProfile: .voiceProcessingHold
      )
    )
  }

  func testPacketizerSplitsLargePcmBuffersIntoTwentyMillisecondPackets() {
    let packetizer = ChroniclePcm16Packetizer()
    let samples = Array(0..<1_600).map(Int16.init)
    let packets = samples.withUnsafeBufferPointer {
      packetizer.append(samples: $0.baseAddress!, count: $0.count)
    }

    XCTAssertEqual(packets.count, 5)
    XCTAssertTrue(packets.allSatisfy { $0.count == 320 })
    XCTAssertEqual(packets[0].first, 0)
    XCTAssertEqual(packets[4].last, 1_599)
    XCTAssertEqual(packetizer.pendingSampleCount, 0)
  }

  func testPacketizerCarriesPartialPcmAcrossTapCallbacks() {
    let packetizer = ChroniclePcm16Packetizer()
    let first = [Int16](repeating: 1, count: 100)
    let second = [Int16](repeating: 2, count: 220)

    let initialPackets = first.withUnsafeBufferPointer {
      packetizer.append(samples: $0.baseAddress!, count: $0.count)
    }
    let completedPackets = second.withUnsafeBufferPointer {
      packetizer.append(samples: $0.baseAddress!, count: $0.count)
    }

    XCTAssertTrue(initialPackets.isEmpty)
    XCTAssertEqual(completedPackets.count, 1)
    XCTAssertEqual(Array(completedPackets[0].prefix(100)), [Int16](repeating: 1, count: 100))
    XCTAssertEqual(Array(completedPackets[0].suffix(220)), [Int16](repeating: 2, count: 220))
    XCTAssertEqual(packetizer.pendingSampleCount, 0)
  }

  func testOpusEncoderProducesOnePacketForTwentyMillisecondsOfPcm() throws {
    let encoder = try ChronicleOpusPacketEncoder()
    let buffer = try XCTUnwrap(
      AVAudioPCMBuffer(
        pcmFormat: encoder.inputFormat,
        frameCapacity: ChronicleOpusPacketEncoder.framesPerPacket
      )
    )
    buffer.frameLength = ChronicleOpusPacketEncoder.framesPerPacket
    let samples = try XCTUnwrap(buffer.int16ChannelData)[0]
    for index in 0..<Int(buffer.frameLength) {
      samples[index] = Int16(Double(Int16.max) * 0.2 * sin(Double(index) * 0.15))
    }

    let packet = try encoder.encode(buffer)

    XCTAssertFalse(packet.isEmpty)
    XCTAssertLessThanOrEqual(packet.count, 1_275)
  }

  func testOpusEncoderAcceptsOnePacketOfRawSamples() throws {
    let encoder = try ChronicleOpusPacketEncoder()
    let samples = (0..<Int(ChronicleOpusPacketEncoder.framesPerPacket)).map {
      Int16(Double(Int16.max) * 0.2 * sin(Double($0) * 0.15))
    }

    let packet = try encoder.encode(samples: samples)

    XCTAssertFalse(packet.isEmpty)
    XCTAssertLessThanOrEqual(packet.count, 1_275)
  }

  func testStopRestoresInactiveEngineState() {
    let state = DuplexAudioState()
    state.start(captureEpoch: 4)
    state.stop()
    XCTAssertFalse(state.running)
    XCTAssertNil(state.response)
  }
}
