import pytest

from backend.constants import is_non_enrollable_speaker, is_unknown_speaker_label


@pytest.mark.parametrize(
    "label",
    [
        "Unknown",
        "Unknown Speaker 2",
        "unknown_speaker_3",
        "Speaker 0",
        "speaker 12",
        "SPEAKER_00",
        " Speaker 1 ",
    ],
)
def test_placeholder_and_raw_diarization_labels_are_unknown(label):
    assert is_unknown_speaker_label(label)
    assert is_non_enrollable_speaker(label)


@pytest.mark.parametrize("label", ["Stu", "Speaker", "Speakerphone 2", "Guest Speaker 1"])
def test_real_names_are_not_unknown(label):
    assert not is_unknown_speaker_label(label)
