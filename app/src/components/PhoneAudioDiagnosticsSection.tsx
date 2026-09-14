import { useRouter } from 'expo-router';
import React, { useState } from 'react';
import { Alert, Platform, StyleSheet, View } from 'react-native';

import { Body, Button, ButtonRow, Caption, Card, CardWell, Mono } from '@/components/ui';
import {
  runPhoneAudioDiagnosticSuite,
  type PhoneAudioDiagnosticProgress,
  type PhoneAudioDiagnosticRunResult,
} from '@/services/phoneAudioSelfTest';
import { useTheme, type Theme } from '@/theme';

interface PhoneAudioDiagnosticsSectionProps {
  backendUrl: string;
  jwtToken: string | null;
}

function progressLabel(progress: PhoneAudioDiagnosticProgress | null): string {
  if (!progress) return 'Ready';
  if (progress.phase === 'native') {
    return `${progress.label} (${progress.current}/${progress.total})`;
  }
  return progress.label;
}

export default function PhoneAudioDiagnosticsSection({
  backendUrl,
  jwtToken,
}: PhoneAudioDiagnosticsSectionProps) {
  const router = useRouter();
  const t = useTheme();
  const s = createStyles(t);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<PhoneAudioDiagnosticProgress | null>(null);
  const [result, setResult] = useState<PhoneAudioDiagnosticRunResult | null>(null);

  const run = async () => {
    setRunning(true);
    setResult(null);
    try {
      const next = await runPhoneAudioDiagnosticSuite({
        backendUrl,
        jwtToken,
        onProgress: setProgress,
      });
      setResult(next);
      const nativePassed = next.nativeProbes.filter(probe => probe.status === 'pass').length;
      Alert.alert(
        next.status === 'pass' ? 'Audio checks complete' : 'Audio issue captured',
        `Native profiles: ${nativePassed}/${next.nativeProbes.length}. Backend: ${next.networkProbe.status}. The full trace was appended to Device Log.`,
      );
    } catch (cause) {
      Alert.alert('Audio check failed', String(cause));
    } finally {
      setRunning(false);
    }
  };

  if (Platform.OS !== 'ios') {
    return (
      <Card title="Phone audio diagnostics">
        <Body>The exhaustive native audio matrix is currently available in the iOS TestFlight app.</Body>
      </Card>
    );
  }

  const nativePassed = result?.nativeProbes.filter(probe => probe.status === 'pass').length ?? 0;
  return (
    <Card title="Phone audio diagnostics">
      <Body>
        Runs four bounded microphone configurations, records engine/tap/PCM/Opus counters,
        then performs an authenticated Audio V2 capture with 25 packet acknowledgements.
      </Body>
      <Caption style={s.note}>
        Takes about 12 seconds. Stop any active phone stream first. If a microphone probe
        succeeds, up to 0.5 seconds of its audio is sent as a diagnostic annotation; otherwise
        the backend probe uses synthetic silence. Every result is appended to Device Log.
      </Caption>

      <CardWell style={s.statusWell}>
        <Mono>{running ? progressLabel(progress) : result ? `Run ${result.runId}` : 'Ready to run'}</Mono>
        {result && (
          <View style={s.resultRows}>
            <Mono>Overall: {result.status}</Mono>
            <Mono>Native: {nativePassed}/{result.nativeProbes.length} profiles</Mono>
            <Mono>Backend: {result.networkProbe.status}</Mono>
            <Mono>Packets: {result.networkProbe.packetsAcked}/{result.networkProbe.packetsSent} acked</Mono>
            {result.networkProbe.captureSessionId && (
              <Mono numberOfLines={1}>Capture: {result.networkProbe.captureSessionId}</Mono>
            )}
          </View>
        )}
      </CardWell>

      <Button
        variant="primary"
        size="lg"
        fullWidth
        loading={running}
        disabled={running}
        onPress={run}
      >
        {running ? 'Running audio checks…' : 'Run full audio check'}
      </Button>
      <ButtonRow style={s.logActions}>
        <Button
          variant="outline"
          size="md"
          fullWidth
          disabled={running}
          onPress={() => router.push('/diagnostics')}
        >
          Open Device Log
        </Button>
      </ButtonRow>
    </Card>
  );
}

const createStyles = (t: Theme) => StyleSheet.create({
  note: {
    marginTop: t.space[2],
    marginBottom: t.space[3],
  },
  statusWell: {
    marginBottom: t.space[3],
  },
  resultRows: {
    gap: t.space[1],
    marginTop: t.space[2],
  },
  logActions: {
    marginTop: t.space[2],
  },
});
