from typing import ClassVar as _ClassVar
from typing import Iterable as _Iterable
from typing import Mapping as _Mapping
from typing import Optional as _Optional
from typing import Union as _Union

from google.protobuf import descriptor as _descriptor
from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf import message as _message
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper

DESCRIPTOR: _descriptor.FileDescriptor

class AudioCodec(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AUDIO_CODEC_UNSPECIFIED: _ClassVar[AudioCodec]
    AUDIO_CODEC_OPUS: _ClassVar[AudioCodec]
    AUDIO_CODEC_PCM_S16LE: _ClassVar[AudioCodec]

class DeliveryClass(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DELIVERY_CLASS_UNSPECIFIED: _ClassVar[DeliveryClass]
    DELIVERY_CLASS_LIVE: _ClassVar[DeliveryClass]
    DELIVERY_CLASS_RECOVERED: _ClassVar[DeliveryClass]

class DeviceKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DEVICE_KIND_UNSPECIFIED: _ClassVar[DeviceKind]
    DEVICE_KIND_IOS_PHONE: _ClassVar[DeviceKind]
    DEVICE_KIND_ANDROID_PHONE: _ClassVar[DeviceKind]
    DEVICE_KIND_WEB_BROWSER: _ClassVar[DeviceKind]
    DEVICE_KIND_OMI: _ClassVar[DeviceKind]
    DEVICE_KIND_NEO: _ClassVar[DeviceKind]
    DEVICE_KIND_HAVPE: _ClassVar[DeviceKind]
    DEVICE_KIND_SCREENPIPE: _ClassVar[DeviceKind]
    DEVICE_KIND_PROBE: _ClassVar[DeviceKind]

class ProcessingProfile(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROCESSING_PROFILE_UNSPECIFIED: _ClassVar[ProcessingProfile]
    PROCESSING_PROFILE_AMBIENT: _ClassVar[ProcessingProfile]
    PROCESSING_PROFILE_SOURCE_NATIVE: _ClassVar[ProcessingProfile]
    PROCESSING_PROFILE_DUPLEX_AEC: _ClassVar[ProcessingProfile]
    PROCESSING_PROFILE_DUPLEX_ISOLATED: _ClassVar[ProcessingProfile]
    PROCESSING_PROFILE_HALF_DUPLEX: _ClassVar[ProcessingProfile]
    PROCESSING_PROFILE_IMPORTED: _ClassVar[ProcessingProfile]

class DataPurpose(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DATA_PURPOSE_UNSPECIFIED: _ClassVar[DataPurpose]
    DATA_PURPOSE_NORMAL_CAPTURE: _ClassVar[DataPurpose]
    DATA_PURPOSE_ANNOTATION: _ClassVar[DataPurpose]

class DuplexMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DUPLEX_MODE_UNSPECIFIED: _ClassVar[DuplexMode]
    DUPLEX_MODE_FULL: _ClassVar[DuplexMode]
    DUPLEX_MODE_ISOLATED: _ClassVar[DuplexMode]
    DUPLEX_MODE_HALF: _ClassVar[DuplexMode]

class InputRoute(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    INPUT_ROUTE_UNSPECIFIED: _ClassVar[InputRoute]
    INPUT_ROUTE_BUILT_IN_MIC: _ClassVar[InputRoute]
    INPUT_ROUTE_BLUETOOTH_HFP: _ClassVar[InputRoute]
    INPUT_ROUTE_WIRED_MIC: _ClassVar[InputRoute]
    INPUT_ROUTE_USB: _ClassVar[InputRoute]
    INPUT_ROUTE_REMOTE: _ClassVar[InputRoute]
    INPUT_ROUTE_UNKNOWN: _ClassVar[InputRoute]

class OutputRoute(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OUTPUT_ROUTE_UNSPECIFIED: _ClassVar[OutputRoute]
    OUTPUT_ROUTE_SPEAKERPHONE: _ClassVar[OutputRoute]
    OUTPUT_ROUTE_EARPIECE: _ClassVar[OutputRoute]
    OUTPUT_ROUTE_HEADPHONES: _ClassVar[OutputRoute]
    OUTPUT_ROUTE_BLUETOOTH_HFP: _ClassVar[OutputRoute]
    OUTPUT_ROUTE_USB: _ClassVar[OutputRoute]
    OUTPUT_ROUTE_REMOTE: _ClassVar[OutputRoute]

class StopReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STOP_REASON_UNSPECIFIED: _ClassVar[StopReason]
    STOP_REASON_USER_REQUESTED: _ClassVar[StopReason]
    STOP_REASON_AUDIO_DISCONNECT: _ClassVar[StopReason]
    STOP_REASON_INTERACTION_COMPLETE: _ClassVar[StopReason]
    STOP_REASON_TEMPORARILY_UNAVAILABLE: _ClassVar[StopReason]

class PlaybackState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLAYBACK_STATE_UNSPECIFIED: _ClassVar[PlaybackState]
    PLAYBACK_STATE_STARTED: _ClassVar[PlaybackState]
    PLAYBACK_STATE_DONE: _ClassVar[PlaybackState]
    PLAYBACK_STATE_CANCELLED: _ClassVar[PlaybackState]
    PLAYBACK_STATE_FAILED: _ClassVar[PlaybackState]
    PLAYBACK_STATE_PROGRESS: _ClassVar[PlaybackState]

class ButtonState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    BUTTON_STATE_UNSPECIFIED: _ClassVar[ButtonState]
    BUTTON_STATE_SINGLE_PRESS: _ClassVar[ButtonState]
    BUTTON_STATE_DOUBLE_PRESS: _ClassVar[ButtonState]
    BUTTON_STATE_LONG_PRESS: _ClassVar[ButtonState]

class ProtocolErrorCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROTOCOL_ERROR_CODE_UNSPECIFIED: _ClassVar[ProtocolErrorCode]
    PROTOCOL_ERROR_CODE_AUTHENTICATION_FAILED: _ClassVar[ProtocolErrorCode]
    PROTOCOL_ERROR_CODE_INVALID_TRANSITION: _ClassVar[ProtocolErrorCode]
    PROTOCOL_ERROR_CODE_INVALID_MEDIA: _ClassVar[ProtocolErrorCode]
    PROTOCOL_ERROR_CODE_STALE_BINDING: _ClassVar[ProtocolErrorCode]
    PROTOCOL_ERROR_CODE_UNSUPPORTED_AUDIO_FORMAT: _ClassVar[ProtocolErrorCode]
    PROTOCOL_ERROR_CODE_MESSAGE_TOO_LARGE: _ClassVar[ProtocolErrorCode]
    PROTOCOL_ERROR_CODE_INTERNAL: _ClassVar[ProtocolErrorCode]

class ConversationAction(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONVERSATION_ACTION_UNSPECIFIED: _ClassVar[ConversationAction]
    CONVERSATION_ACTION_START: _ClassVar[ConversationAction]
    CONVERSATION_ACTION_END: _ClassVar[ConversationAction]
    CONVERSATION_ACTION_CANCEL_TASK: _ClassVar[ConversationAction]
    CONVERSATION_ACTION_SNAPSHOT: _ClassVar[ConversationAction]
    CONVERSATION_ACTION_PAUSE_TASK: _ClassVar[ConversationAction]
    CONVERSATION_ACTION_RESUME_TASK: _ClassVar[ConversationAction]

class SpeechEngine(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPEECH_ENGINE_UNSPECIFIED: _ClassVar[SpeechEngine]
    SPEECH_ENGINE_MODULAR: _ClassVar[SpeechEngine]
    SPEECH_ENGINE_REALTIME: _ClassVar[SpeechEngine]

class ConversationPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONVERSATION_PHASE_UNSPECIFIED: _ClassVar[ConversationPhase]
    CONVERSATION_PHASE_LISTENING: _ClassVar[ConversationPhase]
    CONVERSATION_PHASE_THINKING: _ClassVar[ConversationPhase]
    CONVERSATION_PHASE_SPEAKING: _ClassVar[ConversationPhase]
    CONVERSATION_PHASE_ENDED: _ClassVar[ConversationPhase]

class VoiceTaskStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VOICE_TASK_STATUS_UNSPECIFIED: _ClassVar[VoiceTaskStatus]
    VOICE_TASK_STATUS_QUEUED: _ClassVar[VoiceTaskStatus]
    VOICE_TASK_STATUS_RUNNING: _ClassVar[VoiceTaskStatus]
    VOICE_TASK_STATUS_COMPLETED: _ClassVar[VoiceTaskStatus]
    VOICE_TASK_STATUS_FAILED: _ClassVar[VoiceTaskStatus]
    VOICE_TASK_STATUS_CANCEL_REQUESTED: _ClassVar[VoiceTaskStatus]
    VOICE_TASK_STATUS_CANCELLED: _ClassVar[VoiceTaskStatus]
    VOICE_TASK_STATUS_UNKNOWN: _ClassVar[VoiceTaskStatus]

class VoiceEffectKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VOICE_EFFECT_KIND_UNSPECIFIED: _ClassVar[VoiceEffectKind]
    VOICE_EFFECT_KIND_RESPONSE: _ClassVar[VoiceEffectKind]
    VOICE_EFFECT_KIND_TASK: _ClassVar[VoiceEffectKind]
    VOICE_EFFECT_KIND_PUBLISH_STATE: _ClassVar[VoiceEffectKind]
    VOICE_EFFECT_KIND_CANCEL_TASK: _ClassVar[VoiceEffectKind]

AUDIO_CODEC_UNSPECIFIED: AudioCodec
AUDIO_CODEC_OPUS: AudioCodec
AUDIO_CODEC_PCM_S16LE: AudioCodec
DELIVERY_CLASS_UNSPECIFIED: DeliveryClass
DELIVERY_CLASS_LIVE: DeliveryClass
DELIVERY_CLASS_RECOVERED: DeliveryClass
DEVICE_KIND_UNSPECIFIED: DeviceKind
DEVICE_KIND_IOS_PHONE: DeviceKind
DEVICE_KIND_ANDROID_PHONE: DeviceKind
DEVICE_KIND_WEB_BROWSER: DeviceKind
DEVICE_KIND_OMI: DeviceKind
DEVICE_KIND_NEO: DeviceKind
DEVICE_KIND_HAVPE: DeviceKind
DEVICE_KIND_SCREENPIPE: DeviceKind
DEVICE_KIND_PROBE: DeviceKind
PROCESSING_PROFILE_UNSPECIFIED: ProcessingProfile
PROCESSING_PROFILE_AMBIENT: ProcessingProfile
PROCESSING_PROFILE_SOURCE_NATIVE: ProcessingProfile
PROCESSING_PROFILE_DUPLEX_AEC: ProcessingProfile
PROCESSING_PROFILE_DUPLEX_ISOLATED: ProcessingProfile
PROCESSING_PROFILE_HALF_DUPLEX: ProcessingProfile
PROCESSING_PROFILE_IMPORTED: ProcessingProfile
DATA_PURPOSE_UNSPECIFIED: DataPurpose
DATA_PURPOSE_NORMAL_CAPTURE: DataPurpose
DATA_PURPOSE_ANNOTATION: DataPurpose
DUPLEX_MODE_UNSPECIFIED: DuplexMode
DUPLEX_MODE_FULL: DuplexMode
DUPLEX_MODE_ISOLATED: DuplexMode
DUPLEX_MODE_HALF: DuplexMode
INPUT_ROUTE_UNSPECIFIED: InputRoute
INPUT_ROUTE_BUILT_IN_MIC: InputRoute
INPUT_ROUTE_BLUETOOTH_HFP: InputRoute
INPUT_ROUTE_WIRED_MIC: InputRoute
INPUT_ROUTE_USB: InputRoute
INPUT_ROUTE_REMOTE: InputRoute
INPUT_ROUTE_UNKNOWN: InputRoute
OUTPUT_ROUTE_UNSPECIFIED: OutputRoute
OUTPUT_ROUTE_SPEAKERPHONE: OutputRoute
OUTPUT_ROUTE_EARPIECE: OutputRoute
OUTPUT_ROUTE_HEADPHONES: OutputRoute
OUTPUT_ROUTE_BLUETOOTH_HFP: OutputRoute
OUTPUT_ROUTE_USB: OutputRoute
OUTPUT_ROUTE_REMOTE: OutputRoute
STOP_REASON_UNSPECIFIED: StopReason
STOP_REASON_USER_REQUESTED: StopReason
STOP_REASON_AUDIO_DISCONNECT: StopReason
STOP_REASON_INTERACTION_COMPLETE: StopReason
STOP_REASON_TEMPORARILY_UNAVAILABLE: StopReason
PLAYBACK_STATE_UNSPECIFIED: PlaybackState
PLAYBACK_STATE_STARTED: PlaybackState
PLAYBACK_STATE_DONE: PlaybackState
PLAYBACK_STATE_CANCELLED: PlaybackState
PLAYBACK_STATE_FAILED: PlaybackState
PLAYBACK_STATE_PROGRESS: PlaybackState
BUTTON_STATE_UNSPECIFIED: ButtonState
BUTTON_STATE_SINGLE_PRESS: ButtonState
BUTTON_STATE_DOUBLE_PRESS: ButtonState
BUTTON_STATE_LONG_PRESS: ButtonState
PROTOCOL_ERROR_CODE_UNSPECIFIED: ProtocolErrorCode
PROTOCOL_ERROR_CODE_AUTHENTICATION_FAILED: ProtocolErrorCode
PROTOCOL_ERROR_CODE_INVALID_TRANSITION: ProtocolErrorCode
PROTOCOL_ERROR_CODE_INVALID_MEDIA: ProtocolErrorCode
PROTOCOL_ERROR_CODE_STALE_BINDING: ProtocolErrorCode
PROTOCOL_ERROR_CODE_UNSUPPORTED_AUDIO_FORMAT: ProtocolErrorCode
PROTOCOL_ERROR_CODE_MESSAGE_TOO_LARGE: ProtocolErrorCode
PROTOCOL_ERROR_CODE_INTERNAL: ProtocolErrorCode
CONVERSATION_ACTION_UNSPECIFIED: ConversationAction
CONVERSATION_ACTION_START: ConversationAction
CONVERSATION_ACTION_END: ConversationAction
CONVERSATION_ACTION_CANCEL_TASK: ConversationAction
CONVERSATION_ACTION_SNAPSHOT: ConversationAction
CONVERSATION_ACTION_PAUSE_TASK: ConversationAction
CONVERSATION_ACTION_RESUME_TASK: ConversationAction
SPEECH_ENGINE_UNSPECIFIED: SpeechEngine
SPEECH_ENGINE_MODULAR: SpeechEngine
SPEECH_ENGINE_REALTIME: SpeechEngine
CONVERSATION_PHASE_UNSPECIFIED: ConversationPhase
CONVERSATION_PHASE_LISTENING: ConversationPhase
CONVERSATION_PHASE_THINKING: ConversationPhase
CONVERSATION_PHASE_SPEAKING: ConversationPhase
CONVERSATION_PHASE_ENDED: ConversationPhase
VOICE_TASK_STATUS_UNSPECIFIED: VoiceTaskStatus
VOICE_TASK_STATUS_QUEUED: VoiceTaskStatus
VOICE_TASK_STATUS_RUNNING: VoiceTaskStatus
VOICE_TASK_STATUS_COMPLETED: VoiceTaskStatus
VOICE_TASK_STATUS_FAILED: VoiceTaskStatus
VOICE_TASK_STATUS_CANCEL_REQUESTED: VoiceTaskStatus
VOICE_TASK_STATUS_CANCELLED: VoiceTaskStatus
VOICE_TASK_STATUS_UNKNOWN: VoiceTaskStatus
VOICE_EFFECT_KIND_UNSPECIFIED: VoiceEffectKind
VOICE_EFFECT_KIND_RESPONSE: VoiceEffectKind
VOICE_EFFECT_KIND_TASK: VoiceEffectKind
VOICE_EFFECT_KIND_PUBLISH_STATE: VoiceEffectKind
VOICE_EFFECT_KIND_CANCEL_TASK: VoiceEffectKind

class EventId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class ClientId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class ConnectionId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class CaptureSourceId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class CaptureSessionId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class MemorySpaceId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class VoiceSessionId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class TurnId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class ResponseId(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class AudioSpec(_message.Message):
    __slots__ = (
        "codec",
        "sample_rate_hz",
        "channel_count",
        "frame_duration",
        "bitrate_bps",
    )
    CODEC_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_COUNT_FIELD_NUMBER: _ClassVar[int]
    FRAME_DURATION_FIELD_NUMBER: _ClassVar[int]
    BITRATE_BPS_FIELD_NUMBER: _ClassVar[int]
    codec: AudioCodec
    sample_rate_hz: int
    channel_count: int
    frame_duration: _duration_pb2.Duration
    bitrate_bps: int
    def __init__(
        self,
        codec: _Optional[_Union[AudioCodec, str]] = ...,
        sample_rate_hz: _Optional[int] = ...,
        channel_count: _Optional[int] = ...,
        frame_duration: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...,
        bitrate_bps: _Optional[int] = ...,
    ) -> None: ...

class CaptureBinding(_message.Message):
    __slots__ = ("capture_session_id", "voice_session_id", "capture_epoch")
    CAPTURE_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    VOICE_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_EPOCH_FIELD_NUMBER: _ClassVar[int]
    capture_session_id: CaptureSessionId
    voice_session_id: VoiceSessionId
    capture_epoch: int
    def __init__(
        self,
        capture_session_id: _Optional[_Union[CaptureSessionId, _Mapping]] = ...,
        voice_session_id: _Optional[_Union[VoiceSessionId, _Mapping]] = ...,
        capture_epoch: _Optional[int] = ...,
    ) -> None: ...

class EffectStatus(_message.Message):
    __slots__ = ("requested", "available", "enabled")
    REQUESTED_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    requested: bool
    available: bool
    enabled: bool
    def __init__(
        self, requested: bool = ..., available: bool = ..., enabled: bool = ...
    ) -> None: ...

class CaptureCapabilities(_message.Message):
    __slots__ = (
        "duplex_mode",
        "input_route",
        "output_route",
        "native_sample_rate_hz",
        "acoustic_echo_cancellation",
        "noise_suppression",
        "incremental_playback",
    )
    DUPLEX_MODE_FIELD_NUMBER: _ClassVar[int]
    INPUT_ROUTE_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_ROUTE_FIELD_NUMBER: _ClassVar[int]
    NATIVE_SAMPLE_RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    ACOUSTIC_ECHO_CANCELLATION_FIELD_NUMBER: _ClassVar[int]
    NOISE_SUPPRESSION_FIELD_NUMBER: _ClassVar[int]
    INCREMENTAL_PLAYBACK_FIELD_NUMBER: _ClassVar[int]
    duplex_mode: DuplexMode
    input_route: InputRoute
    output_route: OutputRoute
    native_sample_rate_hz: int
    acoustic_echo_cancellation: EffectStatus
    noise_suppression: EffectStatus
    incremental_playback: bool
    def __init__(
        self,
        duplex_mode: _Optional[_Union[DuplexMode, str]] = ...,
        input_route: _Optional[_Union[InputRoute, str]] = ...,
        output_route: _Optional[_Union[OutputRoute, str]] = ...,
        native_sample_rate_hz: _Optional[int] = ...,
        acoustic_echo_cancellation: _Optional[_Union[EffectStatus, _Mapping]] = ...,
        noise_suppression: _Optional[_Union[EffectStatus, _Mapping]] = ...,
        incremental_playback: bool = ...,
    ) -> None: ...

class ClientHello(_message.Message):
    __slots__ = (
        "bearer_token",
        "source_id",
        "device_kind",
        "display_name",
        "supported_uplink",
        "supported_downlink",
    )
    BEARER_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    DEVICE_KIND_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_UPLINK_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_DOWNLINK_FIELD_NUMBER: _ClassVar[int]
    bearer_token: str
    source_id: CaptureSourceId
    device_kind: DeviceKind
    display_name: str
    supported_uplink: _containers.RepeatedCompositeFieldContainer[AudioSpec]
    supported_downlink: _containers.RepeatedCompositeFieldContainer[AudioSpec]
    def __init__(
        self,
        bearer_token: _Optional[str] = ...,
        source_id: _Optional[_Union[CaptureSourceId, _Mapping]] = ...,
        device_kind: _Optional[_Union[DeviceKind, str]] = ...,
        display_name: _Optional[str] = ...,
        supported_uplink: _Optional[_Iterable[_Union[AudioSpec, _Mapping]]] = ...,
        supported_downlink: _Optional[_Iterable[_Union[AudioSpec, _Mapping]]] = ...,
    ) -> None: ...

class StartCapture(_message.Message):
    __slots__ = (
        "capture_epoch",
        "processing_profile",
        "data_purpose",
        "delivery_class",
        "audio_spec",
        "capabilities",
        "recovery_batch_id",
        "memory_space_id",
    )
    CAPTURE_EPOCH_FIELD_NUMBER: _ClassVar[int]
    PROCESSING_PROFILE_FIELD_NUMBER: _ClassVar[int]
    DATA_PURPOSE_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_CLASS_FIELD_NUMBER: _ClassVar[int]
    AUDIO_SPEC_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    RECOVERY_BATCH_ID_FIELD_NUMBER: _ClassVar[int]
    MEMORY_SPACE_ID_FIELD_NUMBER: _ClassVar[int]
    capture_epoch: int
    processing_profile: ProcessingProfile
    data_purpose: DataPurpose
    delivery_class: DeliveryClass
    audio_spec: AudioSpec
    capabilities: CaptureCapabilities
    recovery_batch_id: str
    memory_space_id: MemorySpaceId
    def __init__(
        self,
        capture_epoch: _Optional[int] = ...,
        processing_profile: _Optional[_Union[ProcessingProfile, str]] = ...,
        data_purpose: _Optional[_Union[DataPurpose, str]] = ...,
        delivery_class: _Optional[_Union[DeliveryClass, str]] = ...,
        audio_spec: _Optional[_Union[AudioSpec, _Mapping]] = ...,
        capabilities: _Optional[_Union[CaptureCapabilities, _Mapping]] = ...,
        recovery_batch_id: _Optional[str] = ...,
        memory_space_id: _Optional[_Union[MemorySpaceId, _Mapping]] = ...,
    ) -> None: ...

class StopCapture(_message.Message):
    __slots__ = ("binding", "reason")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    reason: StopReason
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        reason: _Optional[_Union[StopReason, str]] = ...,
    ) -> None: ...

class VoiceReady(_message.Message):
    __slots__ = ("binding", "capabilities")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    capabilities: CaptureCapabilities
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        capabilities: _Optional[_Union[CaptureCapabilities, _Mapping]] = ...,
    ) -> None: ...

class PlaybackAcknowledgement(_message.Message):
    __slots__ = (
        "binding",
        "response_id",
        "generation",
        "state",
        "monotonic_timestamp_us",
        "error_code",
        "rendered_samples",
        "buffered_samples",
    )
    BINDING_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    MONOTONIC_TIMESTAMP_US_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    RENDERED_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    BUFFERED_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    response_id: ResponseId
    generation: int
    state: PlaybackState
    monotonic_timestamp_us: int
    error_code: ProtocolErrorCode
    rendered_samples: int
    buffered_samples: int
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        response_id: _Optional[_Union[ResponseId, _Mapping]] = ...,
        generation: _Optional[int] = ...,
        state: _Optional[_Union[PlaybackState, str]] = ...,
        monotonic_timestamp_us: _Optional[int] = ...,
        error_code: _Optional[_Union[ProtocolErrorCode, str]] = ...,
        rendered_samples: _Optional[int] = ...,
        buffered_samples: _Optional[int] = ...,
    ) -> None: ...

class Heartbeat(_message.Message):
    __slots__ = ("monotonic_timestamp_us",)
    MONOTONIC_TIMESTAMP_US_FIELD_NUMBER: _ClassVar[int]
    monotonic_timestamp_us: int
    def __init__(self, monotonic_timestamp_us: _Optional[int] = ...) -> None: ...

class ButtonEvent(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: ButtonState
    def __init__(self, state: _Optional[_Union[ButtonState, str]] = ...) -> None: ...

class ClientControl(_message.Message):
    __slots__ = (
        "event_id",
        "sent_at",
        "hello",
        "start_capture",
        "stop_capture",
        "voice_ready",
        "playback_acknowledgement",
        "heartbeat",
        "button_event",
        "conversation_command",
    )
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SENT_AT_FIELD_NUMBER: _ClassVar[int]
    HELLO_FIELD_NUMBER: _ClassVar[int]
    START_CAPTURE_FIELD_NUMBER: _ClassVar[int]
    STOP_CAPTURE_FIELD_NUMBER: _ClassVar[int]
    VOICE_READY_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_ACKNOWLEDGEMENT_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    BUTTON_EVENT_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_COMMAND_FIELD_NUMBER: _ClassVar[int]
    event_id: EventId
    sent_at: _timestamp_pb2.Timestamp
    hello: ClientHello
    start_capture: StartCapture
    stop_capture: StopCapture
    voice_ready: VoiceReady
    playback_acknowledgement: PlaybackAcknowledgement
    heartbeat: Heartbeat
    button_event: ButtonEvent
    conversation_command: ConversationCommand
    def __init__(
        self,
        event_id: _Optional[_Union[EventId, _Mapping]] = ...,
        sent_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        hello: _Optional[_Union[ClientHello, _Mapping]] = ...,
        start_capture: _Optional[_Union[StartCapture, _Mapping]] = ...,
        stop_capture: _Optional[_Union[StopCapture, _Mapping]] = ...,
        voice_ready: _Optional[_Union[VoiceReady, _Mapping]] = ...,
        playback_acknowledgement: _Optional[
            _Union[PlaybackAcknowledgement, _Mapping]
        ] = ...,
        heartbeat: _Optional[_Union[Heartbeat, _Mapping]] = ...,
        button_event: _Optional[_Union[ButtonEvent, _Mapping]] = ...,
        conversation_command: _Optional[_Union[ConversationCommand, _Mapping]] = ...,
    ) -> None: ...

class ServerHello(_message.Message):
    __slots__ = ("client_id", "connection_id")
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    client_id: ClientId
    connection_id: ConnectionId
    def __init__(
        self,
        client_id: _Optional[_Union[ClientId, _Mapping]] = ...,
        connection_id: _Optional[_Union[ConnectionId, _Mapping]] = ...,
    ) -> None: ...

class CaptureStarted(_message.Message):
    __slots__ = ("binding", "audio_spec")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    AUDIO_SPEC_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    audio_spec: AudioSpec
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        audio_spec: _Optional[_Union[AudioSpec, _Mapping]] = ...,
    ) -> None: ...

class CaptureStopped(_message.Message):
    __slots__ = ("binding",)
    BINDING_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    def __init__(
        self, binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...
    ) -> None: ...

class CapturePacketAccepted(_message.Message):
    __slots__ = ("binding", "sequence")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    sequence: int
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        sequence: _Optional[int] = ...,
    ) -> None: ...

class TranscriptUpdate(_message.Message):
    __slots__ = ("binding", "text", "is_final", "confidence", "speaker_name")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    IS_FINAL_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    SPEAKER_NAME_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    text: str
    is_final: bool
    confidence: float
    speaker_name: str
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        text: _Optional[str] = ...,
        is_final: bool = ...,
        confidence: _Optional[float] = ...,
        speaker_name: _Optional[str] = ...,
    ) -> None: ...

class PlaybackOffer(_message.Message):
    __slots__ = (
        "binding",
        "turn_id",
        "response_id",
        "generation",
        "audio_spec",
        "duration",
        "barge_in_allowed",
        "incremental",
        "pre_skip_samples",
    )
    BINDING_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    AUDIO_SPEC_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    BARGE_IN_ALLOWED_FIELD_NUMBER: _ClassVar[int]
    INCREMENTAL_FIELD_NUMBER: _ClassVar[int]
    PRE_SKIP_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    turn_id: TurnId
    response_id: ResponseId
    generation: int
    audio_spec: AudioSpec
    duration: _duration_pb2.Duration
    barge_in_allowed: bool
    incremental: bool
    pre_skip_samples: int
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        turn_id: _Optional[_Union[TurnId, _Mapping]] = ...,
        response_id: _Optional[_Union[ResponseId, _Mapping]] = ...,
        generation: _Optional[int] = ...,
        audio_spec: _Optional[_Union[AudioSpec, _Mapping]] = ...,
        duration: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...,
        barge_in_allowed: bool = ...,
        incremental: bool = ...,
        pre_skip_samples: _Optional[int] = ...,
    ) -> None: ...

class CancelPlayback(_message.Message):
    __slots__ = ("binding", "response_id", "generation", "reason")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    response_id: ResponseId
    generation: int
    reason: StopReason
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        response_id: _Optional[_Union[ResponseId, _Mapping]] = ...,
        generation: _Optional[int] = ...,
        reason: _Optional[_Union[StopReason, str]] = ...,
    ) -> None: ...

class ProtocolError(_message.Message):
    __slots__ = ("code", "detail", "rejected_event_id")
    CODE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    REJECTED_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    code: ProtocolErrorCode
    detail: str
    rejected_event_id: EventId
    def __init__(
        self,
        code: _Optional[_Union[ProtocolErrorCode, str]] = ...,
        detail: _Optional[str] = ...,
        rejected_event_id: _Optional[_Union[EventId, _Mapping]] = ...,
    ) -> None: ...

class ServerControl(_message.Message):
    __slots__ = (
        "event_id",
        "sent_at",
        "hello",
        "capture_started",
        "capture_stopped",
        "playback_offer",
        "cancel_playback",
        "heartbeat",
        "error",
        "capture_packet_accepted",
        "transcript_update",
        "conversation_state",
        "playback_finished",
        "voice_processing_update",
    )
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SENT_AT_FIELD_NUMBER: _ClassVar[int]
    HELLO_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_STARTED_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_STOPPED_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_OFFER_FIELD_NUMBER: _ClassVar[int]
    CANCEL_PLAYBACK_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_PACKET_ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_UPDATE_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_STATE_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_FINISHED_FIELD_NUMBER: _ClassVar[int]
    VOICE_PROCESSING_UPDATE_FIELD_NUMBER: _ClassVar[int]
    event_id: EventId
    sent_at: _timestamp_pb2.Timestamp
    hello: ServerHello
    capture_started: CaptureStarted
    capture_stopped: CaptureStopped
    playback_offer: PlaybackOffer
    cancel_playback: CancelPlayback
    heartbeat: Heartbeat
    error: ProtocolError
    capture_packet_accepted: CapturePacketAccepted
    transcript_update: TranscriptUpdate
    conversation_state: ConversationState
    playback_finished: PlaybackFinished
    voice_processing_update: VoiceProcessingUpdate
    def __init__(
        self,
        event_id: _Optional[_Union[EventId, _Mapping]] = ...,
        sent_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        hello: _Optional[_Union[ServerHello, _Mapping]] = ...,
        capture_started: _Optional[_Union[CaptureStarted, _Mapping]] = ...,
        capture_stopped: _Optional[_Union[CaptureStopped, _Mapping]] = ...,
        playback_offer: _Optional[_Union[PlaybackOffer, _Mapping]] = ...,
        cancel_playback: _Optional[_Union[CancelPlayback, _Mapping]] = ...,
        heartbeat: _Optional[_Union[Heartbeat, _Mapping]] = ...,
        error: _Optional[_Union[ProtocolError, _Mapping]] = ...,
        capture_packet_accepted: _Optional[
            _Union[CapturePacketAccepted, _Mapping]
        ] = ...,
        transcript_update: _Optional[_Union[TranscriptUpdate, _Mapping]] = ...,
        conversation_state: _Optional[_Union[ConversationState, _Mapping]] = ...,
        playback_finished: _Optional[_Union[PlaybackFinished, _Mapping]] = ...,
        voice_processing_update: _Optional[
            _Union[VoiceProcessingUpdate, _Mapping]
        ] = ...,
    ) -> None: ...

class CaptureMediaPacket(_message.Message):
    __slots__ = (
        "device_monotonic_timestamp_us",
        "binding",
        "sequence",
        "captured_at",
        "monotonic_offset_us",
        "delivery_class",
        "opus_payload",
    )
    DEVICE_MONOTONIC_TIMESTAMP_US_FIELD_NUMBER: _ClassVar[int]
    BINDING_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    CAPTURED_AT_FIELD_NUMBER: _ClassVar[int]
    MONOTONIC_OFFSET_US_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_CLASS_FIELD_NUMBER: _ClassVar[int]
    OPUS_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    device_monotonic_timestamp_us: int
    binding: CaptureBinding
    sequence: int
    captured_at: _timestamp_pb2.Timestamp
    monotonic_offset_us: int
    delivery_class: DeliveryClass
    opus_payload: bytes
    def __init__(
        self,
        device_monotonic_timestamp_us: _Optional[int] = ...,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        sequence: _Optional[int] = ...,
        captured_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        monotonic_offset_us: _Optional[int] = ...,
        delivery_class: _Optional[_Union[DeliveryClass, str]] = ...,
        opus_payload: _Optional[bytes] = ...,
    ) -> None: ...

class PlaybackMediaPacket(_message.Message):
    __slots__ = (
        "response_id",
        "generation",
        "sequence",
        "final_packet",
        "opus_payload",
    )
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    FINAL_PACKET_FIELD_NUMBER: _ClassVar[int]
    OPUS_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    response_id: ResponseId
    generation: int
    sequence: int
    final_packet: bool
    opus_payload: bytes
    def __init__(
        self,
        response_id: _Optional[_Union[ResponseId, _Mapping]] = ...,
        generation: _Optional[int] = ...,
        sequence: _Optional[int] = ...,
        final_packet: bool = ...,
        opus_payload: _Optional[bytes] = ...,
    ) -> None: ...

class MediaEnvelope(_message.Message):
    __slots__ = ("capture", "playback")
    CAPTURE_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_FIELD_NUMBER: _ClassVar[int]
    capture: CaptureMediaPacket
    playback: PlaybackMediaPacket
    def __init__(
        self,
        capture: _Optional[_Union[CaptureMediaPacket, _Mapping]] = ...,
        playback: _Optional[_Union[PlaybackMediaPacket, _Mapping]] = ...,
    ) -> None: ...

class DeviceDownlinkEvent(_message.Message):
    __slots__ = (
        "playback_offer",
        "playback",
        "cancel_playback",
        "playback_finished",
        "conversation_state",
        "voice_processing_update",
    )
    PLAYBACK_OFFER_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_FIELD_NUMBER: _ClassVar[int]
    CANCEL_PLAYBACK_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_FINISHED_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_STATE_FIELD_NUMBER: _ClassVar[int]
    VOICE_PROCESSING_UPDATE_FIELD_NUMBER: _ClassVar[int]
    playback_offer: PlaybackOffer
    playback: PlaybackMediaPacket
    cancel_playback: CancelPlayback
    playback_finished: PlaybackFinished
    conversation_state: ConversationState
    voice_processing_update: VoiceProcessingUpdate
    def __init__(
        self,
        playback_offer: _Optional[_Union[PlaybackOffer, _Mapping]] = ...,
        playback: _Optional[_Union[PlaybackMediaPacket, _Mapping]] = ...,
        cancel_playback: _Optional[_Union[CancelPlayback, _Mapping]] = ...,
        playback_finished: _Optional[_Union[PlaybackFinished, _Mapping]] = ...,
        conversation_state: _Optional[_Union[ConversationState, _Mapping]] = ...,
        voice_processing_update: _Optional[
            _Union[VoiceProcessingUpdate, _Mapping]
        ] = ...,
    ) -> None: ...

class CaptureStreamOpened(_message.Message):
    __slots__ = (
        "binding",
        "client_id",
        "source_id",
        "source_spec",
        "processing_profile",
        "data_purpose",
        "memory_space_id",
    )
    BINDING_FIELD_NUMBER: _ClassVar[int]
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_SPEC_FIELD_NUMBER: _ClassVar[int]
    PROCESSING_PROFILE_FIELD_NUMBER: _ClassVar[int]
    DATA_PURPOSE_FIELD_NUMBER: _ClassVar[int]
    MEMORY_SPACE_ID_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    client_id: ClientId
    source_id: CaptureSourceId
    source_spec: AudioSpec
    processing_profile: ProcessingProfile
    data_purpose: DataPurpose
    memory_space_id: MemorySpaceId
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        client_id: _Optional[_Union[ClientId, _Mapping]] = ...,
        source_id: _Optional[_Union[CaptureSourceId, _Mapping]] = ...,
        source_spec: _Optional[_Union[AudioSpec, _Mapping]] = ...,
        processing_profile: _Optional[_Union[ProcessingProfile, str]] = ...,
        data_purpose: _Optional[_Union[DataPurpose, str]] = ...,
        memory_space_id: _Optional[_Union[MemorySpaceId, _Mapping]] = ...,
    ) -> None: ...

class CanonicalPcmFrame(_message.Message):
    __slots__ = (
        "device_monotonic_timestamp_us",
        "binding",
        "sequence",
        "captured_at",
        "monotonic_offset_us",
        "delivery_class",
        "pcm_s16le",
        "data_purpose",
    )
    DEVICE_MONOTONIC_TIMESTAMP_US_FIELD_NUMBER: _ClassVar[int]
    BINDING_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    CAPTURED_AT_FIELD_NUMBER: _ClassVar[int]
    MONOTONIC_OFFSET_US_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_CLASS_FIELD_NUMBER: _ClassVar[int]
    PCM_S16LE_FIELD_NUMBER: _ClassVar[int]
    DATA_PURPOSE_FIELD_NUMBER: _ClassVar[int]
    device_monotonic_timestamp_us: int
    binding: CaptureBinding
    sequence: int
    captured_at: _timestamp_pb2.Timestamp
    monotonic_offset_us: int
    delivery_class: DeliveryClass
    pcm_s16le: bytes
    data_purpose: DataPurpose
    def __init__(
        self,
        device_monotonic_timestamp_us: _Optional[int] = ...,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        sequence: _Optional[int] = ...,
        captured_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        monotonic_offset_us: _Optional[int] = ...,
        delivery_class: _Optional[_Union[DeliveryClass, str]] = ...,
        pcm_s16le: _Optional[bytes] = ...,
        data_purpose: _Optional[_Union[DataPurpose, str]] = ...,
    ) -> None: ...

class CaptureStreamEnded(_message.Message):
    __slots__ = ("binding", "reason")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    reason: StopReason
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        reason: _Optional[_Union[StopReason, str]] = ...,
    ) -> None: ...

class CaptureStreamEvent(_message.Message):
    __slots__ = ("opened", "frame", "ended", "failed")
    OPENED_FIELD_NUMBER: _ClassVar[int]
    FRAME_FIELD_NUMBER: _ClassVar[int]
    ENDED_FIELD_NUMBER: _ClassVar[int]
    FAILED_FIELD_NUMBER: _ClassVar[int]
    opened: CaptureStreamOpened
    frame: CanonicalPcmFrame
    ended: CaptureStreamEnded
    failed: ProtocolError
    def __init__(
        self,
        opened: _Optional[_Union[CaptureStreamOpened, _Mapping]] = ...,
        frame: _Optional[_Union[CanonicalPcmFrame, _Mapping]] = ...,
        ended: _Optional[_Union[CaptureStreamEnded, _Mapping]] = ...,
        failed: _Optional[_Union[ProtocolError, _Mapping]] = ...,
    ) -> None: ...

class InteractionTimingEvent(_message.Message):
    __slots__ = (
        "event_id",
        "user_id",
        "client_id",
        "audio_session_id",
        "capture_epoch",
        "turn_id",
        "turn_revision",
        "response_id",
        "generation",
        "stage",
        "clock_domain",
        "timestamp_ms",
        "observed_at_ms",
        "duration_ms",
        "outcome",
        "detail",
    )
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    AUDIO_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_EPOCH_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_REVISION_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    CLOCK_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    user_id: str
    client_id: str
    audio_session_id: str
    capture_epoch: int
    turn_id: str
    turn_revision: int
    response_id: str
    generation: int
    stage: str
    clock_domain: str
    timestamp_ms: float
    observed_at_ms: float
    duration_ms: float
    outcome: str
    detail: str
    def __init__(
        self,
        event_id: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        client_id: _Optional[str] = ...,
        audio_session_id: _Optional[str] = ...,
        capture_epoch: _Optional[int] = ...,
        turn_id: _Optional[str] = ...,
        turn_revision: _Optional[int] = ...,
        response_id: _Optional[str] = ...,
        generation: _Optional[int] = ...,
        stage: _Optional[str] = ...,
        clock_domain: _Optional[str] = ...,
        timestamp_ms: _Optional[float] = ...,
        observed_at_ms: _Optional[float] = ...,
        duration_ms: _Optional[float] = ...,
        outcome: _Optional[str] = ...,
        detail: _Optional[str] = ...,
    ) -> None: ...

class ConversationCommand(_message.Message):
    __slots__ = (
        "binding",
        "action",
        "engine",
        "task_id",
        "interaction_id",
        "thread_id",
        "task_revision",
    )
    BINDING_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    INTERACTION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_REVISION_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    action: ConversationAction
    engine: SpeechEngine
    task_id: str
    interaction_id: str
    thread_id: str
    task_revision: int
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        action: _Optional[_Union[ConversationAction, str]] = ...,
        engine: _Optional[_Union[SpeechEngine, str]] = ...,
        task_id: _Optional[str] = ...,
        interaction_id: _Optional[str] = ...,
        thread_id: _Optional[str] = ...,
        task_revision: _Optional[int] = ...,
    ) -> None: ...

class VoiceTaskSnapshot(_message.Message):
    __slots__ = ("task_id", "tool_name", "status", "detail")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    tool_name: str
    status: VoiceTaskStatus
    detail: str
    def __init__(
        self,
        task_id: _Optional[str] = ...,
        tool_name: _Optional[str] = ...,
        status: _Optional[_Union[VoiceTaskStatus, str]] = ...,
        detail: _Optional[str] = ...,
    ) -> None: ...

class ConversationState(_message.Message):
    __slots__ = (
        "binding",
        "interaction_id",
        "revision",
        "phase",
        "engine",
        "tasks",
        "detail",
        "transcript",
        "response_text",
        "vault_retrieval_enabled",
        "response_generation",
        "response_effect_id",
        "thread_id",
    )
    BINDING_FIELD_NUMBER: _ClassVar[int]
    INTERACTION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    TASKS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_TEXT_FIELD_NUMBER: _ClassVar[int]
    VAULT_RETRIEVAL_ENABLED_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_EFFECT_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    interaction_id: str
    revision: int
    phase: ConversationPhase
    engine: SpeechEngine
    tasks: _containers.RepeatedCompositeFieldContainer[VoiceTaskSnapshot]
    detail: str
    transcript: str
    response_text: str
    vault_retrieval_enabled: bool
    response_generation: int
    response_effect_id: str
    thread_id: str
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        interaction_id: _Optional[str] = ...,
        revision: _Optional[int] = ...,
        phase: _Optional[_Union[ConversationPhase, str]] = ...,
        engine: _Optional[_Union[SpeechEngine, str]] = ...,
        tasks: _Optional[_Iterable[_Union[VoiceTaskSnapshot, _Mapping]]] = ...,
        detail: _Optional[str] = ...,
        transcript: _Optional[str] = ...,
        response_text: _Optional[str] = ...,
        vault_retrieval_enabled: bool = ...,
        response_generation: _Optional[int] = ...,
        response_effect_id: _Optional[str] = ...,
        thread_id: _Optional[str] = ...,
    ) -> None: ...

class VoiceProcessingUpdate(_message.Message):
    __slots__ = (
        "binding",
        "interaction_id",
        "generation",
        "sequence",
        "response_id",
        "transcribing",
        "generating_text",
        "synthesizing_speech",
        "generating_response",
        "finished",
        "effect_id",
        "state_revision",
    )
    BINDING_FIELD_NUMBER: _ClassVar[int]
    INTERACTION_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIBING_FIELD_NUMBER: _ClassVar[int]
    GENERATING_TEXT_FIELD_NUMBER: _ClassVar[int]
    SYNTHESIZING_SPEECH_FIELD_NUMBER: _ClassVar[int]
    GENERATING_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    FINISHED_FIELD_NUMBER: _ClassVar[int]
    EFFECT_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_REVISION_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    interaction_id: str
    generation: int
    sequence: int
    response_id: ResponseId
    transcribing: bool
    generating_text: bool
    synthesizing_speech: bool
    generating_response: bool
    finished: bool
    effect_id: str
    state_revision: int
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        interaction_id: _Optional[str] = ...,
        generation: _Optional[int] = ...,
        sequence: _Optional[int] = ...,
        response_id: _Optional[_Union[ResponseId, _Mapping]] = ...,
        transcribing: bool = ...,
        generating_text: bool = ...,
        synthesizing_speech: bool = ...,
        generating_response: bool = ...,
        finished: bool = ...,
        effect_id: _Optional[str] = ...,
        state_revision: _Optional[int] = ...,
    ) -> None: ...

class PlaybackFinished(_message.Message):
    __slots__ = ("binding", "response_id", "generation", "total_samples")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    TOTAL_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    binding: CaptureBinding
    response_id: ResponseId
    generation: int
    total_samples: int
    def __init__(
        self,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        response_id: _Optional[_Union[ResponseId, _Mapping]] = ...,
        generation: _Optional[int] = ...,
        total_samples: _Optional[int] = ...,
    ) -> None: ...

class VoiceEffect(_message.Message):
    __slots__ = ("effect_id", "interaction_id", "kind", "generation", "task_id")
    EFFECT_ID_FIELD_NUMBER: _ClassVar[int]
    INTERACTION_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    effect_id: str
    interaction_id: str
    kind: VoiceEffectKind
    generation: int
    task_id: str
    def __init__(
        self,
        effect_id: _Optional[str] = ...,
        interaction_id: _Optional[str] = ...,
        kind: _Optional[_Union[VoiceEffectKind, str]] = ...,
        generation: _Optional[int] = ...,
        task_id: _Optional[str] = ...,
    ) -> None: ...

class VoiceJournalMessage(_message.Message):
    __slots__ = ("role", "content", "interrupted")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    INTERRUPTED_FIELD_NUMBER: _ClassVar[int]
    role: str
    content: str
    interrupted: bool
    def __init__(
        self,
        role: _Optional[str] = ...,
        content: _Optional[str] = ...,
        interrupted: bool = ...,
    ) -> None: ...

class VoiceJournalExcerpt(_message.Message):
    __slots__ = ("path", "revision", "text", "coverage")
    PATH_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    COVERAGE_FIELD_NUMBER: _ClassVar[int]
    path: str
    revision: str
    text: str
    coverage: str
    def __init__(
        self,
        path: _Optional[str] = ...,
        revision: _Optional[str] = ...,
        text: _Optional[str] = ...,
        coverage: _Optional[str] = ...,
    ) -> None: ...

class VoiceJournalTask(_message.Message):
    __slots__ = (
        "task_id",
        "name",
        "status",
        "request",
        "answer",
        "remote_run_id",
        "evidence",
    )
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    ANSWER_FIELD_NUMBER: _ClassVar[int]
    REMOTE_RUN_ID_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    name: str
    status: str
    request: str
    answer: str
    remote_run_id: str
    evidence: _containers.RepeatedCompositeFieldContainer[VoiceJournalExcerpt]
    def __init__(
        self,
        task_id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        status: _Optional[str] = ...,
        request: _Optional[str] = ...,
        answer: _Optional[str] = ...,
        remote_run_id: _Optional[str] = ...,
        evidence: _Optional[_Iterable[_Union[VoiceJournalExcerpt, _Mapping]]] = ...,
    ) -> None: ...

class VoiceJournalTurn(_message.Message):
    __slots__ = ("work_id", "turn_id", "turn_revision", "start_ms", "end_ms", "status")
    WORK_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_REVISION_FIELD_NUMBER: _ClassVar[int]
    START_MS_FIELD_NUMBER: _ClassVar[int]
    END_MS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    work_id: str
    turn_id: TurnId
    turn_revision: int
    start_ms: float
    end_ms: float
    status: str
    def __init__(
        self,
        work_id: _Optional[str] = ...,
        turn_id: _Optional[_Union[TurnId, _Mapping]] = ...,
        turn_revision: _Optional[int] = ...,
        start_ms: _Optional[float] = ...,
        end_ms: _Optional[float] = ...,
        status: _Optional[str] = ...,
    ) -> None: ...

class VoiceJournalEntry(_message.Message):
    __slots__ = (
        "event_id",
        "interaction_id",
        "revision",
        "user_id",
        "client_id",
        "binding",
        "memory_space_id",
        "phase",
        "status",
        "messages",
        "tasks",
        "response_id",
        "rendered_samples",
        "recorded_at",
        "turns",
        "thread_id",
        "utterance_ids",
    )
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    INTERACTION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    BINDING_FIELD_NUMBER: _ClassVar[int]
    MEMORY_SPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    TASKS_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    RENDERED_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    RECORDED_AT_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    UTTERANCE_IDS_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    interaction_id: str
    revision: int
    user_id: str
    client_id: str
    binding: CaptureBinding
    memory_space_id: str
    phase: str
    status: str
    messages: _containers.RepeatedCompositeFieldContainer[VoiceJournalMessage]
    tasks: _containers.RepeatedCompositeFieldContainer[VoiceJournalTask]
    response_id: ResponseId
    rendered_samples: int
    recorded_at: _timestamp_pb2.Timestamp
    turns: _containers.RepeatedCompositeFieldContainer[VoiceJournalTurn]
    thread_id: str
    utterance_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        event_id: _Optional[str] = ...,
        interaction_id: _Optional[str] = ...,
        revision: _Optional[int] = ...,
        user_id: _Optional[str] = ...,
        client_id: _Optional[str] = ...,
        binding: _Optional[_Union[CaptureBinding, _Mapping]] = ...,
        memory_space_id: _Optional[str] = ...,
        phase: _Optional[str] = ...,
        status: _Optional[str] = ...,
        messages: _Optional[_Iterable[_Union[VoiceJournalMessage, _Mapping]]] = ...,
        tasks: _Optional[_Iterable[_Union[VoiceJournalTask, _Mapping]]] = ...,
        response_id: _Optional[_Union[ResponseId, _Mapping]] = ...,
        rendered_samples: _Optional[int] = ...,
        recorded_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        turns: _Optional[_Iterable[_Union[VoiceJournalTurn, _Mapping]]] = ...,
        thread_id: _Optional[str] = ...,
        utterance_ids: _Optional[_Iterable[str]] = ...,
    ) -> None: ...
