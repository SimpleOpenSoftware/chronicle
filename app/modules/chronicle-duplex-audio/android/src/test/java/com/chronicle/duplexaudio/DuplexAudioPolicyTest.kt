package com.chronicle.duplexaudio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class DuplexAudioPolicyTest {
  @Test fun speakerphoneRequiresVerifiedAecForFullDuplex() {
    assertEquals(DuplexMode.FULL, DuplexAudioPolicy.mode(false, true, true))
    assertEquals(DuplexMode.HALF, DuplexAudioPolicy.mode(false, true, false))
  }

  @Test fun wiredAndBluetoothCommunicationAudioAreIsolated() {
    assertEquals(DuplexMode.ISOLATED, DuplexAudioPolicy.mode(true, false, false))
  }

  @Test fun staleEpochIsRejectedBeforeAudioTrackWrite() {
    val gate = ResponseEpochGate()
    gate.start(4)
    assertThrows(IllegalArgumentException::class.java) {
      gate.schedule(EpochResponse("old", 1, 3))
    }
  }

  @Test fun recorderRouteAndFocusFailuresFlushOneCancellationAck() {
    val gate = ResponseEpochGate()
    val response = EpochResponse("response-1", 7, 4)
    gate.start(4)
    gate.schedule(response)
    assertEquals(response, gate.systemChanged())
    assertNull(gate.systemChanged())
  }

  @Test fun newerCancellationGenerationStopsOlderPlayingResponse() {
    val response = EpochResponse("response-1", 7, 4)
    assertEquals(true, DuplexAudioPolicy.shouldCancel(response, "response-1", 8))
  }

  @Test fun staleOrMismatchedCancellationCannotStopCurrentResponse() {
    val response = EpochResponse("response-1", 7, 4)
    assertEquals(false, DuplexAudioPolicy.shouldCancel(response, "response-1", 6))
    assertEquals(false, DuplexAudioPolicy.shouldCancel(response, "other", 8))
  }

  @Test fun onlyOneAudioTrackResponseCanBeScheduled() {
    val gate = ResponseEpochGate()
    gate.start(4)
    gate.schedule(EpochResponse("one", 1, 4))
    assertThrows(IllegalStateException::class.java) {
      gate.schedule(EpochResponse("two", 1, 4))
    }
  }

  @Test fun audioMeterReportsSilenceAndNormalizedPeak() {
    assertEquals(0.0, DuplexAudioPolicy.audioLevel(ByteArray(640), 640), 0.001)
    val halfScale = ByteArray(640)
    for (index in halfScale.indices step 2) {
      halfScale[index] = 0
      halfScale[index + 1] = 64
    }
    assertEquals(0.5, DuplexAudioPolicy.audioLevel(halfScale, 640), 0.001)
  }
}
