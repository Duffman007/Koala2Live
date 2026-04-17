"""
koalaALS.py  -  BUSSES VARIANT
-------------------------------
Koala Sampler .koala backup -> Ableton Live 12 project

BUS ROUTING
    If any pad in the project has a bus value of 0-3, ALL drum rack groups
    are built with 4 bus return chains (A-Bus / B-Bus / C-Bus / D-Bus).
    Four ReturnTracks are added to the project and each drum rack gets a
    ReturnBranches block. Each pad's output is routed to its assigned bus
    return chain; pads with bus=-1 route to No Output (master).

    Koala bus -> ALS ReturnBranch / ReturnTrack
        bus -1  ->  AudioOut/None  (no bus)
        bus  0  ->  a Return Chain -> A-Bus ReturnTrack
        bus  1  ->  b Return Chain -> B-Bus ReturnTrack
        bus  2  ->  c Return Chain -> C-Bus ReturnTrack
        bus  3  ->  d Return Chain -> D-Bus ReturnTrack

    If no pad uses a bus, output is identical to the standard KoalaALS.py.

Koala Sampler .koala backup -> Ableton Live 12 project

USAGE
    python3 koalaALS.py MyProject.koala
    (or drag the .koala file onto the script when prompted)

OUTPUT
    MyProject Project/                  <- Valid Ableton project folder
        MyProject.als
        Ableton Project Info/
            AbletonProject.cfg          <- Required by Ableton Live
        Samples/
            Imported/                   <- Extracted WAV samples
            Processed/
                Reverse/                <- Reversed copies where needed

    The ALS contains:
        - One MIDI track per active Koala group (A/B/C/D) with drum rack embedded
        - One MIDI track per note-mode pad with Simpler device embedded
        - All devices inline -- no external .adg or .adv files needed
        - BPM and project name read from the .koala backup

PAD PARAMETERS TRANSLATED (v0.5)
    Koala field   ALS target                     Formula / notes
    -----------   ----------                     ---------------
    vol           Volume (AudioBranchMixer)       max(0.0003162277571, vol^4)
    pan           Panorama Manual                 pan * 2.0 - 1.0
    pitch         TransposeKey Manual             int(round(pitch))  [semitones]
    tune          TransposeFine Manual            tune * 100.0       [cents, ±50]
    speed         TransposeKey + TransposeFine    semitones = 12*log2(speed); integer
                                                  part -> TransposeKey offset, remainder
                                                  cents -> TransposeFine (combined w/ tune)
    attack        AttackTime Manual               log-interp 0.00011->3.0 -> 0.1->20000 ms
    release       ReleaseTime Manual              log-interp 0.0->3.0     -> 1->60000 ms
    fadeIn        OneShotEnvelope FadeInTime      linear 0.0->1.0 -> 0->2000 ms
    fadeOut       OneShotEnvelope FadeOutTime     linear 0.0->1.0 -> 0->2000 ms
                                                  (0.0 keeps ALS default 0.1 ms)
    tone          SimplerFilter IsOn/Type/Freq    <0=LP, >0=HP, 0=off; 1001.69 Hz fixed
    start         SampleStart                     direct (sample frames)
    end           SampleEnd                       direct (sample frames)
    looping       LoopOn + ReleaseLoop Mode       true/false + 0/3
    oneshot       PlaybackMode                    0=note, 1=oneshot
    chokeGroup    ChokeGroup                      direct (drum rack only)
    reverse       separate reversed WAV file      Samples/Processed/Reverse/
    stretching    IsWarped                        true/false

    trim          SampleStart offset                  trim * total_WAV_frames added to start
                                                  (reads WAV header on disk; safe to 0 if
                                                  file unavailable)

    NOTE: Koala's `nudge` field has no direct per-pad ALS equivalent and is not
    translated.

    All parameters are applied to both drum rack branches and Simpler tracks.

DEPENDENCIES
    No external files or pip packages needed beyond Python standard library.

NOTES
    - This is the base working version. Do not make creative changes without
      explicit instruction. All behaviour is intentional and tested.
    - All ADG/ADV logic is unchanged from koalaexportADV.py
    - Drum rack and Simpler templates are extracted from a reference Ableton project
      and embedded as base64 constants (_DRUM_RACK_TPL_B64 etc)
"""


import sys
import io
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import gzip
import json
import math
import os
import shlex
import shutil
import struct
import sys
import wave
import zipfile
from pathlib import Path


# ══════════════════════════════════════════════════════════════════════════════
# SHARED CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

GROUPS        = ["Group A", "Group B", "Group C", "Group D"]
SAMPLE_SUBFOLDER = os.path.join("Samples", "Imported")

# MIDI constants
TICKS_PER_BEAT   = 4096
MIDI_TICKS_PER_BAR = 4 * TICKS_PER_BEAT
KOALA_BASE_NOTE  = 36
GROUP_DEFS = [
    ("Group A",  0, 15),
    ("Group B", 16, 31),
    ("Group C", 32, 47),
    ("Group D", 48, 63),
]
GROUP_LETTER = {"Group A": "A", "Group B": "B", "Group C": "C", "Group D": "D"}
GROUP_SHIFT  = {"Group A": 0, "Group B": -16, "Group C": -32, "Group D": -48}


# ══════════════════════════════════════════════════════════════════════════════
# -- ADG SECTION (from koalaadg30) --------------------------------------------
# ══════════════════════════════════════════════════════════════════════════════

class IdCounter:
    def __init__(self):
        self._n = -1
    def next(self):
        self._n += 1
        return self._n


def get_group_index(pad_num: int) -> int:
    bank = pad_num // 16
    return bank if 0 <= bank <= 3 else -1


def get_bank_position(pad_num: int) -> tuple:
    pad_in_bank = pad_num % 16
    row = pad_in_bank // 4
    col = pad_in_bank % 4
    return row, col


def reverse_wav(src_path: str, dst_path: str):
    with wave.open(src_path, 'rb') as w:
        params     = w.getparams()
        frame_size = params.nchannels * params.sampwidth
        frames     = w.readframes(w.getnframes())
    n   = len(frames) // frame_size
    rev = b''.join(frames[i*frame_size:(i+1)*frame_size] for i in range(n-1, -1, -1))
    with wave.open(dst_path, 'wb') as w:
        w.setparams(params)
        w.writeframes(rev)


def _drum_branch_preset(ids, receiving_note, relative_path, pad_data,
                        relative_path_type=1, display_name=""):
    if not display_name:
        display_name = os.path.splitext(os.path.basename(relative_path))[0]

    start_pt  = int(pad_data.get("start", 0) or 0)
    end_pt    = int(pad_data.get("end",   0) or 0)
    one_shot  = str(pad_data.get("oneshot",  "false")).lower() == "true"
    loop_on   = str(pad_data.get("looping",  "false")).lower() == "true"
    choke_grp = int(pad_data.get("chokeGroup", 0) or 0)

    playback_mode = 1 if one_shot else 0
    loop_on_val   = "true" if loop_on else "false"
    is_warped     = "true" if pad_data.get("stretching") is True else "false"

    return f"""\t\t\t<DrumBranchPreset Id="{ids.next()}">
\t\t\t\t<Name Value="" />
\t\t\t\t<IsSoloed Value="false" />
\t\t\t\t<DevicePresets>
\t\t\t\t\t<AbletonDevicePreset Id="{ids.next()}">
\t\t\t\t\t\t<OverwriteProtectionNumber Value="2816" />
\t\t\t\t\t\t<Device>
\t\t\t\t\t\t\t<OriginalSimpler Id="{ids.next()}">
\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t<LomIdView Value="0" />
\t\t\t\t\t\t\t\t<IsExpanded Value="true" />
\t\t\t\t\t\t\t\t<On>
\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t<Manual Value="true" />
\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t</On>
\t\t\t\t\t\t\t\t<ModulationSourceCount Value="0" />
\t\t\t\t\t\t\t\t<ParametersListWrapper LomId="0" />
\t\t\t\t\t\t\t\t<Pointee Id="{ids.next()}" />
\t\t\t\t\t\t\t\t<LastSelectedTimeableIndex Value="0" />
\t\t\t\t\t\t\t\t<LastSelectedClipEnvelopeIndex Value="0" />
\t\t\t\t\t\t\t\t<LockedScripts />
\t\t\t\t\t\t\t\t<IsFolded Value="false" />
\t\t\t\t\t\t\t\t<ShouldShowPresetName Value="true" />
\t\t\t\t\t\t\t\t<UserName Value="" />
\t\t\t\t\t\t\t\t<Annotation Value="" />
\t\t\t\t\t\t\t\t<SourceContext>
\t\t\t\t\t\t\t\t\t<Value />
\t\t\t\t\t\t\t\t</SourceContext>
\t\t\t\t\t\t\t\t<OverwriteProtectionNumber Value="2816" />
\t\t\t\t\t\t\t\t<Player>
\t\t\t\t\t\t\t\t\t<MultiSampleMap>
\t\t\t\t\t\t\t\t\t\t<SampleParts>
\t\t\t\t\t\t\t\t\t\t\t<MultiSamplePart Id="{ids.next()}" HasImportedSlicePoints="true" NeedsAnalysisData="true">
\t\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<Name Value="{display_name}" />
\t\t\t\t\t\t\t\t\t\t\t\t<Selection Value="true" />
\t\t\t\t\t\t\t\t\t\t\t\t<IsActive Value="true" />
\t\t\t\t\t\t\t\t\t\t\t\t<Solo Value="false" />
\t\t\t\t\t\t\t\t\t\t\t\t<KeyRange>
\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMin Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMax Value="127" />
\t\t\t\t\t\t\t\t\t\t\t\t</KeyRange>
\t\t\t\t\t\t\t\t\t\t\t\t<VelocityRange>
\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="1" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMin Value="1" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMax Value="127" />
\t\t\t\t\t\t\t\t\t\t\t\t</VelocityRange>
\t\t\t\t\t\t\t\t\t\t\t\t<SelectorRange>
\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMin Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMax Value="127" />
\t\t\t\t\t\t\t\t\t\t\t\t</SelectorRange>
\t\t\t\t\t\t\t\t\t\t\t\t<RootKey Value="60" />
\t\t\t\t\t\t\t\t\t\t\t\t<Detune Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<TuneScale Value="100" />
\t\t\t\t\t\t\t\t\t\t\t\t<Panorama Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<Volume Value="1" />
\t\t\t\t\t\t\t\t\t\t\t\t<Link Value="false" />
\t\t\t\t\t\t\t\t\t\t\t\t<SampleStart Value="{start_pt}" />
\t\t\t\t\t\t\t\t\t\t\t\t<SampleEnd Value="{end_pt}" />
\t\t\t\t\t\t\t\t\t\t\t\t<SustainLoop>
\t\t\t\t\t\t\t\t\t\t\t\t\t<Start Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<End Value="{end_pt}" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Mode Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Crossfade Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Detune Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t</SustainLoop>
\t\t\t\t\t\t\t\t\t\t\t\t<ReleaseLoop>
\t\t\t\t\t\t\t\t\t\t\t\t\t<Start Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<End Value="{end_pt}" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Mode Value="3" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Crossfade Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<Detune Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t</ReleaseLoop>
\t\t\t\t\t\t\t\t\t\t\t\t<SampleRef>
\t\t\t\t\t\t\t\t\t\t\t\t\t<FileRef>
\t\t\t\t\t\t\t\t\t\t\t\t\t\t<RelativePathType Value="{relative_path_type}" />
\t\t\t\t\t\t\t\t\t\t\t\t\t\t<RelativePath Value="{relative_path}" />
\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Type Value="2" />
\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LivePackName Value="" />
\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LivePackId Value="" />
\t\t\t\t\t\t\t\t\t\t\t\t\t\t<OriginalFileSize Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t\t<OriginalCrc Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t</FileRef>
\t\t\t\t\t\t\t\t\t\t\t\t\t<LastModDate Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<SampleUsageHint Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<DefaultDuration Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t\t<DefaultSampleRate Value="44100" />
\t\t\t\t\t\t\t\t\t\t\t\t</SampleRef>
\t\t\t\t\t\t\t\t\t\t\t\t<SampleWarpProperties>
\t\t\t\t\t\t\t\t\t\t\t\t\t<IsWarped Value="{is_warped}" />
\t\t\t\t\t\t\t\t\t\t\t\t</SampleWarpProperties>
\t\t\t\t\t\t\t\t\t\t\t\t<SlicingThreshold Value="100" />
\t\t\t\t\t\t\t\t\t\t\t\t<SlicingBeatGrid Value="4" />
\t\t\t\t\t\t\t\t\t\t\t\t<SlicingIsAuto Value="true" />
\t\t\t\t\t\t\t\t\t\t\t\t<SlicingStyle Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<LaunchMode Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<LaunchQuantization Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</MultiSamplePart>
\t\t\t\t\t\t\t\t\t\t</SampleParts>
\t\t\t\t\t\t\t\t\t\t<LoadInRam Value="false" />
\t\t\t\t\t\t\t\t\t\t<LayerCrossfade Value="0" />
\t\t\t\t\t\t\t\t\t\t<SourceContext />
\t\t\t\t\t\t\t\t\t</MultiSampleMap>
\t\t\t\t\t\t\t\t\t<LoopModulators>
\t\t\t\t\t\t\t\t\t\t<IsModulated Value="true" />
\t\t\t\t\t\t\t\t\t\t<SampleStart>
\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t\t\t<Manual Value="0" />
\t\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="1" />
\t\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</ModulationTarget>
\t\t\t\t\t\t\t\t\t\t</SampleStart>
\t\t\t\t\t\t\t\t\t\t<SampleLength>
\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t\t\t<Manual Value="1" />
\t\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="1" />
\t\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</ModulationTarget>
\t\t\t\t\t\t\t\t\t\t</SampleLength>
\t\t\t\t\t\t\t\t\t\t<LoopOn>
\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t\t\t<Manual Value="{loop_on_val}" />
\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t\t\t<MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="64" />
\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t\t\t\t\t</MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t\t\t</LoopOn>
\t\t\t\t\t\t\t\t\t\t<LoopLength>
\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t\t\t<Manual Value="1" />
\t\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="1" />
\t\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</ModulationTarget>
\t\t\t\t\t\t\t\t\t\t</LoopLength>
\t\t\t\t\t\t\t\t\t</LoopModulators>
\t\t\t\t\t\t\t\t\t<Reverse>
\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t\t<Manual Value="false" />
\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t\t<MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t\t\t\t<Min Value="64" />
\t\t\t\t\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t\t\t\t</MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t\t</Reverse>
\t\t\t\t\t\t\t\t\t<InterpolationMode Value="2" />
\t\t\t\t\t\t\t\t\t<UseConstPowCrossfade Value="true" />
\t\t\t\t\t\t\t\t</Player>
								<Pitch>
									<TransposeKey>
										<LomId Value="0" />
										<Manual Value="0" />
										<MidiControllerRange>
											<Min Value="-48" />
											<Max Value="48" />
										</MidiControllerRange>
										<AutomationTarget Id="0">
											<LockEnvelope Value="0" />
										</AutomationTarget>
										<ModulationTarget Id="0">
											<LockEnvelope Value="0" />
										</ModulationTarget>
									</TransposeKey>
									<TransposeFine>
										<LomId Value="0" />
										<Manual Value="0" />
										<MidiControllerRange>
											<Min Value="-50" />
											<Max Value="50" />
										</MidiControllerRange>
										<AutomationTarget Id="0">
											<LockEnvelope Value="0" />
										</AutomationTarget>
										<ModulationTarget Id="0">
											<LockEnvelope Value="0" />
										</ModulationTarget>
									</TransposeFine>
									<Envelope>
										<IsOn>
											<LomId Value="0" />
											<Manual Value="false" />
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<MidiCCOnOffThresholds>
												<Min Value="64" />
												<Max Value="127" />
											</MidiCCOnOffThresholds>
										</IsOn>
										<Slot>
											<Value />
										</Slot>
									</Envelope>
									<ScrollPosition Value="-1073741824" />
								</Pitch>
								
								<VolumeAndPan>
									<Volume>
										<LomId Value="0" />
										<Manual Value="-12" />
										<MidiControllerRange>
											<Min Value="-36" />
											<Max Value="36" />
										</MidiControllerRange>
										<AutomationTarget Id="0">
											<LockEnvelope Value="0" />
										</AutomationTarget>
										<ModulationTarget Id="0">
											<LockEnvelope Value="0" />
										</ModulationTarget>
									</Volume>
									<VolumeVelScale>
										<LomId Value="0" />
										<Manual Value="0.4499999881" />
										<MidiControllerRange>
											<Min Value="0" />
											<Max Value="1" />
										</MidiControllerRange>
										<AutomationTarget Id="0">
											<LockEnvelope Value="0" />
										</AutomationTarget>
										<ModulationTarget Id="0">
											<LockEnvelope Value="0" />
										</ModulationTarget>
									</VolumeVelScale>
									<VolumeKeyScale>
										<LomId Value="0" />
										<Manual Value="0" />
										<MidiControllerRange>
											<Min Value="-1" />
											<Max Value="1" />
										</MidiControllerRange>
										<AutomationTarget Id="0">
											<LockEnvelope Value="0" />
										</AutomationTarget>
										<ModulationTarget Id="0">
											<LockEnvelope Value="0" />
										</ModulationTarget>
									</VolumeKeyScale>
									<Panorama>
										<LomId Value="0" />
										<Manual Value="0" />
										<MidiControllerRange>
											<Min Value="-1" />
											<Max Value="1" />
										</MidiControllerRange>
										<AutomationTarget Id="0">
											<LockEnvelope Value="0" />
										</AutomationTarget>
										<ModulationTarget Id="0">
											<LockEnvelope Value="0" />
										</ModulationTarget>
									</Panorama>
									<PanoramaKeyScale>
										<LomId Value="0" />
										<Manual Value="0" />
										<MidiControllerRange>
											<Min Value="-1" />
											<Max Value="1" />
										</MidiControllerRange>
										<AutomationTarget Id="0">
											<LockEnvelope Value="0" />
										</AutomationTarget>
										<ModulationTarget Id="0">
											<LockEnvelope Value="0" />
										</ModulationTarget>
									</PanoramaKeyScale>
									
									
									<Envelope>
										<AttackTime>
											<LomId Value="0" />
											<Manual Value="0.110000" />
											<MidiControllerRange>
												<Min Value="0.1000000015" />
												<Max Value="20000" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</AttackTime>
										<AttackLevel>
											<LomId Value="0" />
											<Manual Value="0.0003162277571" />
											<MidiControllerRange>
												<Min Value="0.0003162277571" />
												<Max Value="1" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</AttackLevel>
										<AttackSlope>
											<LomId Value="0" />
											<Manual Value="0" />
											<MidiControllerRange>
												<Min Value="-1" />
												<Max Value="1" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</AttackSlope>
										<DecayTime>
											<LomId Value="0" />
											<Manual Value="1" />
											<MidiControllerRange>
												<Min Value="1" />
												<Max Value="60000" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</DecayTime>
										<DecayLevel>
											<LomId Value="0" />
											<Manual Value="1" />
											<MidiControllerRange>
												<Min Value="0.0003162277571" />
												<Max Value="1" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</DecayLevel>
										<DecaySlope>
											<LomId Value="0" />
											<Manual Value="1" />
											<MidiControllerRange>
												<Min Value="-1" />
												<Max Value="1" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</DecaySlope>
										<SustainLevel>
											<LomId Value="0" />
											<Manual Value="1" />
											<MidiControllerRange>
												<Min Value="0.0003162277571" />
												<Max Value="1" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</SustainLevel>
										<ReleaseTime>
											<LomId Value="0" />
											<Manual Value="0.000000" />
											<MidiControllerRange>
												<Min Value="1" />
												<Max Value="60000" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</ReleaseTime>
										<ReleaseLevel>
											<LomId Value="0" />
											<Manual Value="0.0003162277571" />
											<MidiControllerRange>
												<Min Value="0.0003162277571" />
												<Max Value="1" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</ReleaseLevel>
										<ReleaseSlope>
											<LomId Value="0" />
											<Manual Value="1" />
											<MidiControllerRange>
												<Min Value="-1" />
												<Max Value="1" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</ReleaseSlope>
										<LoopMode>
											<LomId Value="0" />
											<Manual Value="0" />
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
										</LoopMode>
										<LoopTime>
											<LomId Value="0" />
											<Manual Value="100" />
											<MidiControllerRange>
												<Min Value="0.200000003" />
												<Max Value="20000" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</LoopTime>
										<RepeatTime>
											<LomId Value="0" />
											<Manual Value="3" />
											<MidiControllerRange>
												<Min Value="0" />
												<Max Value="14" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</RepeatTime>
										<TimeVelScale>
											<LomId Value="0" />
											<Manual Value="0" />
											<MidiControllerRange>
												<Min Value="-100" />
												<Max Value="100" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</TimeVelScale>
										<CurrentOverlay Value="0" />
									</Envelope>
									<OneShotEnvelope>
										<FadeInTime>
											<LomId Value="0" />
											<Manual Value="0" />
											<MidiControllerRange>
												<Min Value="0" />
												<Max Value="2000" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</FadeInTime>
										<SustainMode>
											<LomId Value="0" />
											<Manual Value="0" />
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
										</SustainMode>
										<FadeOutTime>
											<LomId Value="0" />
											<Manual Value="0.1000000015" />
											<MidiControllerRange>
												<Min Value="0" />
												<Max Value="2000" />
											</MidiControllerRange>
											<AutomationTarget Id="0">
												<LockEnvelope Value="0" />
											</AutomationTarget>
											<ModulationTarget Id="0">
												<LockEnvelope Value="0" />
											</ModulationTarget>
										</FadeOutTime>
									</OneShotEnvelope>
								</VolumeAndPan>
								<Groove>
\t\t\t\t\t\t\t\t\t<Value />
\t\t\t\t\t\t\t\t</Groove>
\t\t\t\t\t\t\t\t<Globals>
\t\t\t\t\t\t\t\t\t<NumVoices Value="2" />
\t\t\t\t\t\t\t\t\t<NumVoicesEnvTimeControl Value="false" />
\t\t\t\t\t\t\t\t\t<RetriggerMode Value="true" />
\t\t\t\t\t\t\t\t\t<ModulationResolution Value="2" />
\t\t\t\t\t\t\t\t\t<KeyZoneShift>
\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t\t<Manual Value="0" />
\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t\t<Min Value="-48" />
\t\t\t\t\t\t\t\t\t\t\t<Max Value="48" />
\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t\t</ModulationTarget>
\t\t\t\t\t\t\t\t\t</KeyZoneShift>
\t\t\t\t\t\t\t\t\t<PitchBendRange Value="5" />
\t\t\t\t\t\t\t\t\t<ScrollPosition Value="0" />
\t\t\t\t\t\t\t\t\t<IsSimpler Value="true" />
\t\t\t\t\t\t\t\t\t<PlaybackMode Value="{playback_mode}" />
\t\t\t\t\t\t\t\t\t<LegacyMode Value="false" />
\t\t\t\t\t\t\t\t</Globals>
\t\t\t\t\t\t\t</OriginalSimpler>
\t\t\t\t\t\t</Device>
\t\t\t\t\t</AbletonDevicePreset>
\t\t\t\t</DevicePresets>
\t\t\t\t<MixerPreset>
\t\t\t\t\t<AbletonDevicePreset Id="{ids.next()}">
\t\t\t\t\t\t<OverwriteProtectionNumber Value="2817" />
\t\t\t\t\t\t<Device>
\t\t\t\t\t\t\t<AudioBranchMixerDevice Id="{ids.next()}">
\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t<LomIdView Value="0" />
\t\t\t\t\t\t\t\t<IsExpanded Value="true" />
\t\t\t\t\t\t\t\t<On>
\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t<Manual Value="true" />
\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t<MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t\t\t<Min Value="64" />
\t\t\t\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t\t\t</MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t</On>
\t\t\t\t\t\t\t\t<ModulationSourceCount Value="0" />
\t\t\t\t\t\t\t\t<ParametersListWrapper LomId="0" />
\t\t\t\t\t\t\t\t<Pointee Id="{ids.next()}" />
\t\t\t\t\t\t\t\t<LastSelectedTimeableIndex Value="0" />
\t\t\t\t\t\t\t\t<LastSelectedClipEnvelopeIndex Value="0" />
\t\t\t\t\t\t\t\t<LastPresetRef>
\t\t\t\t\t\t\t\t\t<Value>
\t\t\t\t\t\t\t\t\t\t<AbletonDefaultPresetRef Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t\t<FileRef>
\t\t\t\t\t\t\t\t\t\t\t\t<RelativePathType Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<RelativePath Value="" />
\t\t\t\t\t\t\t\t\t\t\t\t<Path Value="" />
\t\t\t\t\t\t\t\t\t\t\t\t<Type Value="2" />
\t\t\t\t\t\t\t\t\t\t\t\t<LivePackName Value="" />
\t\t\t\t\t\t\t\t\t\t\t\t<LivePackId Value="" />
\t\t\t\t\t\t\t\t\t\t\t\t<OriginalFileSize Value="0" />
\t\t\t\t\t\t\t\t\t\t\t\t<OriginalCrc Value="0" />
\t\t\t\t\t\t\t\t\t\t\t</FileRef>
\t\t\t\t\t\t\t\t\t\t\t<DeviceId Name="AudioBranchMixerDevice" />
\t\t\t\t\t\t\t\t\t\t</AbletonDefaultPresetRef>
\t\t\t\t\t\t\t\t\t</Value>
\t\t\t\t\t\t\t\t</LastPresetRef>
\t\t\t\t\t\t\t\t<LockedScripts />
\t\t\t\t\t\t\t\t<IsFolded Value="false" />
\t\t\t\t\t\t\t\t<ShouldShowPresetName Value="true" />
\t\t\t\t\t\t\t\t<UserName Value="" />
\t\t\t\t\t\t\t\t<Annotation Value="" />
\t\t\t\t\t\t\t\t<SourceContext>
\t\t\t\t\t\t\t\t\t<Value />
\t\t\t\t\t\t\t\t</SourceContext>
\t\t\t\t\t\t\t\t<OverwriteProtectionNumber Value="2817" />
\t\t\t\t\t\t\t\t<Speaker>
\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t<Manual Value="true" />
\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t<MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t\t\t<Min Value="64" />
\t\t\t\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t\t\t</MidiCCOnOffThresholds>
\t\t\t\t\t\t\t\t</Speaker>
\t\t\t\t\t\t\t\t<Volume>
\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t<Manual Value="1.000000" />
\t\t\t\t\t\t\t\t\t<MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t<Min Value="0.0003162277571" />
\t\t\t\t\t\t\t\t\t\t<Max Value="1.99526238" />
\t\t\t\t\t\t\t\t\t</MidiControllerRange>
\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t</ModulationTarget>
\t\t\t\t\t\t\t\t</Volume>
\t\t\t\t\t\t\t\t<Panorama>
\t\t\t\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t\t\t\t<Manual Value="0.000000" />
\t\t\t\t\t\t\t\t\t<MidiControllerRange>
\t\t\t\t\t\t\t\t\t\t<Min Value="-1" />
\t\t\t\t\t\t\t\t\t\t<Max Value="1" />
\t\t\t\t\t\t\t\t\t</MidiControllerRange>
\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{ids.next()}">
\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t\t\t\t\t</ModulationTarget>
\t\t\t\t\t\t\t\t</Panorama>
\t\t\t\t\t\t\t\t<SendInfos />
\t\t\t\t\t\t\t\t<RoutingHelper>
\t\t\t\t\t\t\t\t\t<Routable>
\t\t\t\t\t\t\t\t\t\t<Target Value="AudioOut/None" />
\t\t\t\t\t\t\t\t\t\t<UpperDisplayString Value="No Output" />
\t\t\t\t\t\t\t\t\t\t<LowerDisplayString Value="" />
\t\t\t\t\t\t\t\t\t\t<MpeSettings>
\t\t\t\t\t\t\t\t\t\t\t<ZoneType Value="0" />
\t\t\t\t\t\t\t\t\t\t\t<FirstNoteChannel Value="1" />
\t\t\t\t\t\t\t\t\t\t\t<LastNoteChannel Value="15" />
\t\t\t\t\t\t\t\t\t\t</MpeSettings>
\t\t\t\t\t\t\t\t\t</Routable>
\t\t\t\t\t\t\t\t\t<TargetEnum Value="0" />
\t\t\t\t\t\t\t\t</RoutingHelper>
\t\t\t\t\t\t\t\t<SendsListWrapper LomId="0" />
\t\t\t\t\t\t\t</AudioBranchMixerDevice>
\t\t\t\t\t\t</Device>
\t\t\t\t\t\t<PresetRef>
\t\t\t\t\t\t\t<AbletonDefaultPresetRef Id="{ids.next()}">
\t\t\t\t\t\t\t\t<FileRef>
\t\t\t\t\t\t\t\t\t<RelativePathType Value="0" />
\t\t\t\t\t\t\t\t\t<RelativePath Value="" />
\t\t\t\t\t\t\t\t\t<Path Value="" />
\t\t\t\t\t\t\t\t\t<Type Value="2" />
\t\t\t\t\t\t\t\t\t<LivePackName Value="" />
\t\t\t\t\t\t\t\t\t<LivePackId Value="" />
\t\t\t\t\t\t\t\t\t<OriginalFileSize Value="0" />
\t\t\t\t\t\t\t\t\t<OriginalCrc Value="0" />
\t\t\t\t\t\t\t\t</FileRef>
\t\t\t\t\t\t\t\t<DeviceId Name="AudioBranchMixerDevice" />
\t\t\t\t\t\t\t</AbletonDefaultPresetRef>
\t\t\t\t\t\t</PresetRef>
\t\t\t\t\t</AbletonDevicePreset>
\t\t\t\t</MixerPreset>
\t\t\t\t<SessionViewBranchWidth Value="55" />
\t\t\t\t<DocumentColorIndex Value="4" />
\t\t\t\t<AutoColored Value="true" />
\t\t\t\t<AutoColorScheme Value="0" />
\t\t\t\t<ZoneSettings>
\t\t\t\t\t<ReceivingNote Value="{receiving_note}" />
\t\t\t\t\t<SendingNote Value="60" />
\t\t\t\t\t<ChokeGroup Value="{choke_grp}" />
\t\t\t\t</ZoneSettings>
\t\t\t</DrumBranchPreset>
"""


def make_adg_xml(adg_pads, group_index, group_letter):
    """Build the full ADG XML string for one group."""
    ids = IdCounter()

    GROUP_BASE_NOTES = [80, 80, 80, 80]
    base_note = GROUP_BASE_NOTES[group_index]

    branch_presets_xml = ""
    for pad_num, pad_data, new_name, relative_path, relative_path_type in adg_pads:
        pad_in_bank = pad_num % 16
        row = pad_in_bank // 4
        col = pad_in_bank % 4
        receiving_note = base_note - col + row * 4
        display_name = os.path.splitext(os.path.basename(relative_path))[0]
        branch_presets_xml += _drum_branch_preset(
            ids, receiving_note, relative_path, pad_data,
            relative_path_type, display_name
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Ableton MajorVersion="5" MinorVersion="12.0_12300" SchemaChangeCount="1" Creator="Ableton Live 12.3.2" Revision="bba1e05a8769233839bfbad067d72440d966db31">
\t<GroupDevicePreset>
\t\t<OverwriteProtectionNumber Value="2816" />
\t\t<Device>
\t\t\t<DrumGroupDevice Id="{ids.next()}">
\t\t\t\t<LomId Value="0" />
\t\t\t\t<LomIdView Value="0" />
\t\t\t\t<IsExpanded Value="true" />
\t\t\t\t<On>
\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t<Manual Value="true" />
\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t<MidiCCOnOffThresholds>
\t\t\t\t\t\t<Min Value="64" />
\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t</MidiCCOnOffThresholds>
\t\t\t\t</On>
\t\t\t\t<ModulationSourceCount Value="0" />
\t\t\t\t<ParametersListWrapper LomId="0" />
\t\t\t\t<Pointee Id="{ids.next()}" />
\t\t\t\t<LastSelectedTimeableIndex Value="0" />
\t\t\t\t<LastSelectedClipEnvelopeIndex Value="0" />
\t\t\t\t<LastPresetRef>
\t\t\t\t\t<Value>
\t\t\t\t\t\t<AbletonDefaultPresetRef Id="{ids.next()}">
\t\t\t\t\t\t\t<FileRef>
\t\t\t\t\t\t\t\t<RelativePathType Value="5" />
\t\t\t\t\t\t\t\t<RelativePath Value="Racks/Drum Racks/Drum Rack" />
\t\t\t\t\t\t\t\t<Path Value="/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Racks/Drum Racks/Drum Rack" />
\t\t\t\t\t\t\t\t<Type Value="2" />
\t\t\t\t\t\t\t\t<LivePackName Value="Core Library" />
\t\t\t\t\t\t\t\t<LivePackId Value="www.ableton.com/0" />
\t\t\t\t\t\t\t\t<OriginalFileSize Value="0" />
\t\t\t\t\t\t\t\t<OriginalCrc Value="0" />
\t\t\t\t\t\t\t</FileRef>
\t\t\t\t\t\t\t<DeviceId Name="DrumGroupDevice" />
\t\t\t\t\t\t</AbletonDefaultPresetRef>
\t\t\t\t\t</Value>
\t\t\t\t</LastPresetRef>
\t\t\t\t<LockedScripts />
\t\t\t\t<IsFolded Value="false" />
\t\t\t\t<ShouldShowPresetName Value="true" />
\t\t\t\t<UserName Value="" />
\t\t\t\t<Annotation Value="" />
\t\t\t\t<SourceContext>
\t\t\t\t\t<Value />
\t\t\t\t</SourceContext>
\t\t\t\t<OverwriteProtectionNumber Value="2816" />
\t\t\t\t<Branches />
\t\t\t\t<IsBranchesListVisible Value="false" />
\t\t\t\t<IsReturnBranchesListVisible Value="false" />
\t\t\t\t<IsRangesEditorVisible Value="false" />
\t\t\t\t<AreDevicesVisible Value="true" />
\t\t\t\t<NumVisibleMacroControls Value="8" />
\t\t\t\t<AreMacroControlsVisible Value="false" />
\t\t\t\t<IsAutoSelectEnabled Value="true" />
\t\t\t\t<ChainSelector>
\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t<Manual Value="0" />
\t\t\t\t\t<MidiControllerRange>
\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t</MidiControllerRange>
\t\t\t\t\t<AutomationTarget Id="{ids.next()}">
\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t</AutomationTarget>
\t\t\t\t\t<ModulationTarget Id="{ids.next()}">
\t\t\t\t\t\t<LockEnvelope Value="0" />
\t\t\t\t\t</ModulationTarget>
\t\t\t\t</ChainSelector>
\t\t\t\t<ChainSelectorRelativePosition Value="-1073741824" />
\t\t\t\t<ViewsToRestoreWhenUnfolding Value="0" />
\t\t\t\t<ReturnBranches />
\t\t\t\t<BranchesSplitterProportion Value="0.5" />
\t\t\t\t<ShowBranchesInSessionMixer Value="false" />
\t\t\t\t<LockId Value="0" />
\t\t\t\t<LockSeal Value="0" />
\t\t\t\t<ChainsListWrapper LomId="0" />
\t\t\t\t<ReturnChainsListWrapper LomId="0" />
\t\t\t\t<AreMacroVariationsControlsVisible Value="false" />
\t\t\t\t<ChainSelectorFilterMidiCtrl Value="false" />
\t\t\t\t<RangeTypeIndex Value="1" />
\t\t\t\t<ShowsZonesInsteadOfNoteNames Value="true" />
\t\t\t\t<IsMidiSectionVisible Value="false" />
\t\t\t\t<AreSendsVisible Value="false" />
\t\t\t\t<ArePadsVisible Value="true" />
\t\t\t\t<PadScrollPosition Value="19" />
\t\t\t\t<DrumPadsListWrapper LomId="0" />
\t\t\t\t<VisibleDrumPadsListWrapper LomId="0" />
\t\t\t</DrumGroupDevice>
\t\t</Device>
\t\t<BranchPresets>
{branch_presets_xml}\t\t</BranchPresets>
\t\t<ReturnBranchPresets />
\t</GroupDevicePreset>
</Ableton>
"""


# ══════════════════════════════════════════════════════════════════════════════
# -- MIDI SECTION (from MIDISPLIT31) ------------------------------------------
# ══════════════════════════════════════════════════════════════════════════════

# Simpler_blank.adv embedded as base64 - no external file needed
_BLANK_ADV_B64 = (
    "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"
    "AA=="
)


def make_adv_xml(relative_path: str, pad_data: dict, display_name: str,
                 relative_path_type: int = 1) -> str:
    """
    Build a Simpler .adv preset by patching the blank template with the sample data.
    relative_path: e.g. "Samples/Imported/Cello.wav"
    pad_data: the pad dict from sampler.json (start, end, looping, stretching etc.)
    display_name: shown as the sample name in Simpler
    """
    import base64, io
    xml = gzip.open(io.BytesIO(base64.b64decode(_BLANK_ADV_B64))).read().decode('utf-8')

    start_pt   = int(pad_data.get("start",   0) or 0)
    end_pt     = int(pad_data.get("end",     0) or 0)
    loop_on    = str(pad_data.get("looping", "false")).lower() == "true"
    is_warped  = "true" if pad_data.get("stretching") is True else "false"
    loop_mode  = 0 if loop_on else 3   # 0=loop, 3=no loop (same as drum rack)

    multi_sample_part = f"""<MultiSamplePart Id="0" InitUpdateAreSlicesFromOnsetsEditableAfterRead="false" HasImportedSlicePoints="true" NeedsAnalysisData="true">
\t\t\t\t\t\t<LomId Value="0" />
\t\t\t\t\t\t<Name Value="{display_name}" />
\t\t\t\t\t\t<Selection Value="true" />
\t\t\t\t\t\t<IsActive Value="true" />
\t\t\t\t\t\t<Solo Value="false" />
\t\t\t\t\t\t<KeyRange>
\t\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t<CrossfadeMin Value="0" />
\t\t\t\t\t\t\t<CrossfadeMax Value="127" />
\t\t\t\t\t\t</KeyRange>
\t\t\t\t\t\t<VelocityRange>
\t\t\t\t\t\t\t<Min Value="1" />
\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t<CrossfadeMin Value="1" />
\t\t\t\t\t\t\t<CrossfadeMax Value="127" />
\t\t\t\t\t\t</VelocityRange>
\t\t\t\t\t\t<SelectorRange>
\t\t\t\t\t\t\t<Min Value="0" />
\t\t\t\t\t\t\t<Max Value="127" />
\t\t\t\t\t\t\t<CrossfadeMin Value="0" />
\t\t\t\t\t\t\t<CrossfadeMax Value="127" />
\t\t\t\t\t\t</SelectorRange>
\t\t\t\t\t\t<RootKey Value="60" />
\t\t\t\t\t\t<Detune Value="0" />
\t\t\t\t\t\t<TuneScale Value="100" />
\t\t\t\t\t\t<Panorama Value="0" />
\t\t\t\t\t\t<Volume Value="1" />
\t\t\t\t\t\t<Link Value="false" />
\t\t\t\t\t\t<SampleStart Value="{start_pt}" />
\t\t\t\t\t\t<SampleEnd Value="{end_pt}" />
\t\t\t\t\t\t<SustainLoop>
\t\t\t\t\t\t\t<Start Value="0" />
\t\t\t\t\t\t\t<End Value="{end_pt}" />
\t\t\t\t\t\t\t<Mode Value="0" />
\t\t\t\t\t\t\t<Crossfade Value="0" />
\t\t\t\t\t\t\t<Detune Value="0" />
\t\t\t\t\t\t</SustainLoop>
\t\t\t\t\t\t<ReleaseLoop>
\t\t\t\t\t\t\t<Start Value="0" />
\t\t\t\t\t\t\t<End Value="{end_pt}" />
\t\t\t\t\t\t\t<Mode Value="{loop_mode}" />
\t\t\t\t\t\t\t<Crossfade Value="0" />
\t\t\t\t\t\t\t<Detune Value="0" />
\t\t\t\t\t\t</ReleaseLoop>
\t\t\t\t\t\t<SampleRef>
\t\t\t\t\t\t\t<FileRef>
\t\t\t\t\t\t\t\t<RelativePathType Value="{relative_path_type}" />
\t\t\t\t\t\t\t\t<RelativePath Value="{relative_path}" />
\t\t\t\t\t\t\t\t<Path Value="" />
\t\t\t\t\t\t\t\t<Type Value="2" />
\t\t\t\t\t\t\t\t<LivePackName Value="" />
\t\t\t\t\t\t\t\t<LivePackId Value="" />
\t\t\t\t\t\t\t\t<OriginalFileSize Value="0" />
\t\t\t\t\t\t\t\t<OriginalCrc Value="0" />
\t\t\t\t\t\t\t</FileRef>
\t\t\t\t\t\t\t<LastModDate Value="0" />
\t\t\t\t\t\t\t<SourceContext />
\t\t\t\t\t\t\t<SampleUsageHint Value="0" />
\t\t\t\t\t\t\t<DefaultDuration Value="0" />
\t\t\t\t\t\t\t<DefaultSampleRate Value="44100" />
\t\t\t\t\t\t\t<SamplesToAutoWarp Value="1" />
\t\t\t\t\t\t</SampleRef>
\t\t\t\t\t\t<SlicingThreshold Value="100" />
\t\t\t\t\t\t<SlicingBeatGrid Value="4" />
\t\t\t\t\t\t<SlicingRegions Value="8" />
\t\t\t\t\t\t<SlicingStyle Value="0" />
\t\t\t\t\t\t<SampleWarpProperties>
\t\t\t\t\t\t\t<WarpMarkers />
\t\t\t\t\t\t\t<WarpMode Value="0" />
\t\t\t\t\t\t\t<GranularityTones Value="30" />
\t\t\t\t\t\t\t<GranularityTexture Value="65" />
\t\t\t\t\t\t\t<FluctuationTexture Value="25" />
\t\t\t\t\t\t\t<ComplexProFormants Value="100" />
\t\t\t\t\t\t\t<ComplexProEnvelope Value="128" />
\t\t\t\t\t\t\t<TransientResolution Value="6" />
\t\t\t\t\t\t\t<TransientLoopMode Value="2" />
\t\t\t\t\t\t\t<TransientEnvelope Value="100" />
\t\t\t\t\t\t\t<IsWarped Value="{is_warped}" />
\t\t\t\t\t\t\t<Onsets>
\t\t\t\t\t\t\t\t<UserOnsets />
\t\t\t\t\t\t\t\t<HasUserOnsets Value="false" />
\t\t\t\t\t\t\t</Onsets>
\t\t\t\t\t\t\t<TimeSignature>
\t\t\t\t\t\t\t\t<TimeSignatures>
\t\t\t\t\t\t\t\t\t<RemoteableTimeSignature Id="0">
\t\t\t\t\t\t\t\t\t\t<Numerator Value="4" />
\t\t\t\t\t\t\t\t\t\t<Denominator Value="4" />
\t\t\t\t\t\t\t\t\t\t<Time Value="0" />
\t\t\t\t\t\t\t\t\t</RemoteableTimeSignature>
\t\t\t\t\t\t\t\t</TimeSignatures>
\t\t\t\t\t\t\t</TimeSignature>
\t\t\t\t\t\t\t<BeatGrid>
\t\t\t\t\t\t\t\t<FixedNumerator Value="1" />
\t\t\t\t\t\t\t\t<FixedDenominator Value="16" />
\t\t\t\t\t\t\t\t<GridIntervalPixel Value="20" />
\t\t\t\t\t\t\t\t<Ntoles Value="2" />
\t\t\t\t\t\t\t\t<SnapToGrid Value="true" />
\t\t\t\t\t\t\t\t<Fixed Value="false" />
\t\t\t\t\t\t\t</BeatGrid>
\t\t\t\t\t\t</SampleWarpProperties>
\t\t\t\t\t\t<InitialSlicePointsFromOnsets />
\t\t\t\t\t\t<SlicePoints />
\t\t\t\t\t\t<ManualSlicePoints />
\t\t\t\t\t\t<BeatSlicePoints />
\t\t\t\t\t\t<RegionSlicePoints />
\t\t\t\t\t\t<UseDynamicBeatSlices Value="true" />
\t\t\t\t\t\t<UseDynamicRegionSlices Value="true" />
\t\t\t\t\t\t<AreSlicesFromOnsetsEditable Value="false" />
\t\t\t\t\t</MultiSamplePart>"""

    # Splice into the blank: replace empty <SampleParts /> with populated block
    xml = xml.replace(
        '<SampleParts />',
        f'<SampleParts>\n\t\t\t\t\t{multi_sample_part}\n\t\t\t\t</SampleParts>'
    )
    return xml



def koala_note_to_midi(pad_num: int) -> int:
    bank        = pad_num // 16
    pad_in_bank = pad_num % 16
    row         = pad_in_bank // 4
    col         = pad_in_bank % 4
    return KOALA_BASE_NOTE + bank * 16 + (3 - row) * 4 + col


def group_for_pad(pad_num: int):
    for name, lo, hi in GROUP_DEFS:
        if lo <= pad_num <= hi:
            return name, GROUP_SHIFT[name]
    return None, 0


def pad_label(pad_num: int) -> str:
    bank  = pad_num // 16
    local = pad_num % 16
    return f"{'ABCD'[bank]}{local + 1:02d}"


def pad_num_from_label(label: str) -> int:
    """Inverse of pad_label: 'C05' -> 36, 'A01' -> 0, etc."""
    bank  = 'ABCD'.index(label[0])
    local = int(label[1:]) - 1
    return bank * 16 + local


def koala_pad_to_drum_note(pad_num: int) -> int:
    """Return the drum rack ReceivingNote for a Koala pad number.
    Must match the formula in _make_drum_rack_device_chain exactly.
    """
    _BASE = [80, 80, 80, 80]
    bank        = pad_num // 16
    pad_in_bank = pad_num % 16
    row         = pad_in_bank // 4
    col         = pad_in_bank % 4
    return _BASE[min(bank, 3)] - col + row * 4


# ==============================================================================
# -- MIDI CLIP SECTION --------------------------------------------------------
# ==============================================================================
# Tick conversion: Koala uses 4096 ticks/beat, ALS uses 4 ticks/beat
# Conversion: als_tick = koala_tick / 1024

_ALS_TICKS_PER_BAR    = 4       # 1 tick/beat * 4 beats/bar in 4/4
_KOALA_TO_ALS         = 4096    # koala ticks per beat -> ALS ticks per beat (1:1 beat)
_KOALA_TICKS_PER_BAR  = 16384   # 4096 ticks/beat * 4 beats/bar


def _midi_clip_xml(clip_name, num_bars, note_events, clip_colour=15):
    """
    Build a MidiClip XML string for one ClipSlot.
    note_events: list of (midi_note, velocity, start_koala_tick, end_koala_tick)
    All Koala ticks are divided by _KOALA_TO_ALS to get ALS ticks.
    """
    total_koala_ticks = num_bars * _KOALA_TICKS_PER_BAR
    clip_len          = num_bars * _ALS_TICKS_PER_BAR

    # Group notes by MIDI key into KeyTracks.
    # Clip notes to boundary (same logic as MIDISPLIT5's build_midi).
    key_map = {}   # midi_note -> list of (time, duration, velocity, note_id)
    note_id = 1
    for midi_note, velocity, start_k, end_k in note_events:
        if start_k >= total_koala_ticks:
            continue                                   # note starts after clip end
        end_k      = min(end_k, total_koala_ticks)    # clip note to boundary
        start_a    = start_k / _KOALA_TO_ALS
        duration_a = (end_k - start_k) / _KOALA_TO_ALS
        duration_a = max(0.0625, duration_a)           # minimum duration
        key_map.setdefault(midi_note, []).append(
            (start_a, duration_a, velocity, note_id))
        note_id += 1

    # Build KeyTrack XML for each MIDI key
    key_tracks_xml = ""
    kt_id = 0
    for midi_note in sorted(key_map.keys()):
        notes_xml = ""
        for time, dur, vel, nid in key_map[midi_note]:
            vel = max(1, min(127, int(round(vel))))
            notes_xml += (
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t'
                f'<MidiNoteEvent Time="{time}" Duration="{dur}" '
                f'Velocity="{vel}" OffVelocity="0" NoteId="{nid}" />\n'
            )
        key_tracks_xml += (
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<KeyTrack Id="{kt_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Notes>\n'
            f'{notes_xml}'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t</Notes>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<MidiKey Value="{midi_note}" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t</KeyTrack>\n'
        )
        kt_id += 1

    return (
        f'\t\t\t\t\t\t\t\t\t\t<MidiClip Id="0" Time="0">\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<LomIdView Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<CurrentStart Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<CurrentEnd Value="{clip_len}" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Loop>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<LoopStart Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<LoopEnd Value="{clip_len}" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<StartRelative Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<LoopOn Value="true" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<OutMarker Value="{clip_len}" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<HiddenLoopStart Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<HiddenLoopEnd Value="{clip_len}" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</Loop>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Name Value="{clip_name}" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Annotation Value="" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Color Value="{clip_colour}" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<LaunchMode Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<LaunchQuantisation Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<TimeSignature>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<TimeSignatures>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t<RemoteableTimeSignature Id="0">\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Numerator Value="4" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Denominator Value="4" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Time Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t</RemoteableTimeSignature>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t</TimeSignatures>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</TimeSignature>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Envelopes>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<Envelopes />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</Envelopes>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<ScrollerTimePreserver>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<LeftTime Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<RightTime Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</ScrollerTimePreserver>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<TimeSelection>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<AnchorTime Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<OtherTime Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</TimeSelection>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Legato Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Ram Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<GrooveSettings>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<GrooveId Value="-1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</GrooveSettings>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Disabled Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<VelocityAmount Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<FollowAction>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FollowTime Value="4" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<IsLinked Value="true" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<LoopIterations Value="1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FollowActionA Value="4" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FollowActionB Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FollowChanceA Value="100" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FollowChanceB Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<JumpIndexA Value="1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<JumpIndexB Value="1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FollowActionEnabled Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</FollowAction>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Grid>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FixedNumerator Value="1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FixedDenominator Value="16" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<GridIntervalPixel Value="20" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<Ntoles Value="2" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<SnapToGrid Value="true" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<Fixed Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</Grid>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<FreezeStart Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<FreezeEnd Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<IsWarped Value="true" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<TakeId Value="-1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<IsInKey Value="true" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<ScaleInformation>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<Root Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<Name Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</ScaleInformation>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<Notes>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<KeyTracks>\n'
        f'{key_tracks_xml}'
        f'\t\t\t\t\t\t\t\t\t\t\t\t</KeyTracks>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<PerNoteEventStore>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t<EventLists />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t</PerNoteEventStore>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<NoteProbabilityGroups />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<ProbabilityGroupIdGenerator>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t<NextId Value="1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t</ProbabilityGroupIdGenerator>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<NoteIdGenerator>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t\t<NextId Value="{note_id}" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t</NoteIdGenerator>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</Notes>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<BankSelectCoarse Value="-1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<BankSelectFine Value="-1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<ProgramChange Value="-1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<NoteEditorFoldInZoom Value="-1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<NoteEditorFoldInScroll Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<NoteEditorFoldOutZoom Value="144" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<NoteEditorFoldOutScroll Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<NoteEditorFoldScaleZoom Value="-1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<NoteEditorFoldScaleScroll Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<NoteSpellingPreference Value="0" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<AccidentalSpellingPreference Value="3" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<PreferFlatRootNote Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t<ExpressionGrid>\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FixedNumerator Value="1" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<FixedDenominator Value="16" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<GridIntervalPixel Value="20" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<Ntoles Value="2" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<SnapToGrid Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t\t<Fixed Value="false" />\n'
        f'\t\t\t\t\t\t\t\t\t\t\t</ExpressionGrid>\n'
        f'\t\t\t\t\t\t\t\t\t\t</MidiClip>\n'
    )


def _inject_clips(track_xml, clips_by_slot):
    """
    Inject MidiClip XML into the ClipSlots of a track.
    clips_by_slot: dict of {slot_index: clip_xml_string}
    Only injects into the MainSequencer ClipSlotList (Session view clips),
    not into the FreezeSequencer ClipSlotList.
    """
    import re as _re

    # Scope injection to only the MainSequencer region
    ms_end = track_xml.find('</MainSequencer>')
    if ms_end < 0:
        return track_xml   # no MainSequencer found, leave unchanged

    before_ms = track_xml[:ms_end]
    after_ms  = track_xml[ms_end:]

    def replace_slot(m):
        slot_id = int(m.group(1))
        inner   = m.group(2)
        if slot_id in clips_by_slot:
            inner = inner.replace(
                '<Value />',
                f'<Value>\n{clips_by_slot[slot_id]}\t\t\t\t\t\t\t\t\t</Value>',
                1
            )
        return f'<ClipSlot Id="{slot_id}">{inner}</ClipSlot>'

    before_ms = _re.sub(
        r'<ClipSlot Id="(\d+)">(.*?)</ClipSlot>',
        replace_slot,
        before_ms,
        flags=_re.DOTALL
    )
    return before_ms + after_ms


def build_sequence_clips(seq_data, keyboard_mode, selected_pad, note_mode_pad_nums,
                          group_index_map, chopper_pad_info=None):
    """
    Parse sequences from seq_data and return clip data for each track.

    chopper_pad_info: dict pad_num -> chopper_params dict (from _get_chopper_params).
      Chopper pads are always routed to simpler_clips, never drum_clips.
      Note mode  (trigger=0): midi_note = 35 + int(round(pitch))
      Velocity   (trigger=1): midi_note = 35 + floor((127-vel)*N/128), fixed vel out
      Random     (trigger=2): midi_note = 35 (MidiRandom handles slice selection)

    Returns:
        drum_clips:    dict group_name -> {slot_idx: (clip_name, num_bars, note_events)}
        simpler_clips: dict pad_num    -> {slot_idx: (clip_name, num_bars, note_events)}
    """
    if chopper_pad_info is None:
        chopper_pad_info = {}
    drum_clips    = {gname: {} for gname in group_index_map}
    simpler_clips = {}

    sequences = seq_data.get("sequences", [])
    for seq_idx, seq in enumerate(sequences):
        pattern  = (seq.get("noteSequence", {}) or {}).get("pattern", {}) or                     seq.get("pattern", {}) or {}
        notes    = pattern.get("notes") or []
        num_bars = pattern.get("numBars", 1)
        if not notes:
            continue

        seq_num = seq_idx + 1

        # Partition notes: chopper pads handled separately
        chopper_pads_here = {n["num"] for n in notes if n["num"] in chopper_pad_info}
        # A pad is note-mode in THIS sequence only if it has at least one
        # non-zero pitch note here. selected_pad is NOT unconditionally added —
        # if it only has pitch=0 notes in this sequence it's a drum trigger.
        note_mode_pads = {n["num"] for n in notes
                          if n.get("pitch", 0.0) != 0.0 and n["num"] not in chopper_pads_here}

        normal_notes   = [n for n in notes if n["num"] not in note_mode_pads
                          and n["num"] not in chopper_pads_here]
        keyboard_notes = [n for n in notes if n["num"] in note_mode_pads]
        chopper_notes  = [n for n in notes if n["num"] in chopper_pads_here]

        # -- Drum group clips --------------------------------------------------
        group_events = {gname: [] for gname in group_index_map}
        for note in normal_notes:
            pad_num = note["num"]
            gname, shift = group_for_pad(pad_num)
            if gname is None or gname not in group_index_map:
                continue
            midi_note = koala_note_to_midi(pad_num) + shift
            vel    = note.get("vel", 100)
            start  = int(note["timeOffset"])
            length = int(note["length"])
            if length <= 0:
                continue
            end = start + length
            group_events[gname].append((midi_note, vel, start, end))

        for gname, gevents in group_events.items():
            if gevents:
                letter    = GROUP_LETTER[gname]
                clip_name = f"Seq {seq_num}{letter}"
                slot_idx  = seq_idx
                drum_clips[gname][slot_idx] = (clip_name, num_bars, gevents)

        # -- Simpler (note-mode) clips -----------------------------------------
        pad_events = {}
        for note in keyboard_notes:
            pad_num   = note["num"]
            pitch     = note.get("pitch", 0.0)
            midi_note = 60 + int(round(pitch))
            vel    = note.get("vel", 100)
            start  = int(note["timeOffset"])
            length = int(note["length"])
            if length <= 0:
                continue
            end = start + length
            pad_events.setdefault(pad_num, []).append((midi_note, vel, start, end))

        for pad_num, events in pad_events.items():
            label_str = pad_label(pad_num)
            clip_name = f"Seq {seq_num} {label_str}"
            slot_idx  = seq_idx
            simpler_clips.setdefault(pad_num, {})[slot_idx] = (clip_name, num_bars, events)

        # -- Chopper clips -----------------------------------------------------
        chopper_events = {}
        for note in chopper_notes:
            pad_num = note["num"]
            cp      = chopper_pad_info[pad_num]
            N       = cp['slice_count']
            trigger = cp['trigger_mode']
            vel     = note.get("vel", 100)
            start   = int(note["timeOffset"])
            length  = int(note["length"])
            if length <= 0:
                continue
            end   = start + length
            pitch = note.get("pitch", 0.0)

            if trigger == 0.0:   # Note Mode: pitch selects slice
                slice_idx = int(round(pitch))
                slice_idx = max(0, min(N - 1, slice_idx))
                midi_note = 36 + slice_idx   # C1=slice1, C#1=slice2, ...
                out_vel   = vel
            elif trigger == 1.0: # Velocity Mode: high vel = early slice
                slice_idx = int((127 - vel) * N / 128)
                slice_idx = max(0, min(N - 1, slice_idx))
                midi_note = 36 + slice_idx   # C1=slice1 at max velocity
                out_vel   = 100  # fixed velocity in output
            else:                # Random Mode: route to drum_clips
                midi_note = -1   # handled separately below
                out_vel   = vel

            if midi_note == -1:
                # Random mode: fire the drum rack pad directly so MidiRandom
                # inside the branch selects the slice. Append into the existing
                # drum_clips entry for this group (or create one).
                gname, shift = group_for_pad(pad_num)
                if gname is not None and gname in group_index_map:
                    drum_midi_note = koala_note_to_midi(pad_num) + shift  # same formula as normal pads
                    letter = GROUP_LETTER[gname]
                    if seq_idx not in drum_clips[gname]:
                        drum_clips[gname][seq_idx] = (f"Seq {seq_num}{letter}", num_bars, [])
                    drum_clips[gname][seq_idx][2].append(
                        (drum_midi_note, out_vel, start, end))
            else:
                chopper_events.setdefault(pad_num, []).append((midi_note, out_vel, start, end))

        for pad_num, events in chopper_events.items():
            label_str = pad_label(pad_num)
            clip_name = f"Seq {seq_num} {label_str}"
            slot_idx  = seq_idx
            simpler_clips.setdefault(pad_num, {})[slot_idx] = (clip_name, num_bars, events)

    return drum_clips, simpler_clips


# ==============================================================================
# -- ALS SECTION -------------------------------------------------------------
# ==============================================================================


import base64
import io
import re


_BLANK_ALS_B64 = (
    "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"
)


_DRUM_RACK_TPL_B64 = (
    "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"
    "AA=="
)


_DRUM_BRANCH_TPL_B64 = (
    "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"
)


_SIMPLER_DC_TPL_B64 = (
    "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"
    "PwThIybbGIp0/YKKFrO4SpNt/vT/FIsWeRXuAAA="
)


_ALS_TRACK_COLOURS = [15, 25, 13, 28, 9]  # last for Simpler tracks


def _tpl(b64):
    return gzip.decompress(base64.b64decode(b64)).decode("utf-8")

def _als_load_blank():
    return _tpl(_BLANK_ALS_B64)

def _als_save(path, xml):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(xml.encode("utf-8"))

def _als_remap_ids(xml, id_start):
    """Remap every Id= attribute occurrence to a unique sequential value.
    Each occurrence gets its own unique ID, even if the original value was shared
    across multiple elements (e.g. templates use Id="0" everywhere).
    Skips LomId= attributes via negative lookbehind.
    """
    pat = r'(?<![a-zA-Z])Id="(\d+)"'
    counter = id_start
    replacements = []
    for m in re.finditer(pat, xml):
        replacements.append((m.start(), m.end(), counter))
        counter += 1
    # Build result by replacing from end to start to preserve positions
    result = list(xml)
    for start, end, new_id in reversed(replacements):
        result[start:end] = list(f'Id="{new_id}"')
    return ''.join(result), counter

def _als_remap_track(track_xml, track_id, new_name, new_colour, id_start, rewire_index):
    high_ids_vals = [m.group(1) for m in re.finditer(r'Id="(\d+)"', track_xml)
                     if int(m.group(1)) > 1000]
    id_map  = {}
    counter = id_start
    for old in high_ids_vals:
        if old not in id_map:
            id_map[old] = counter
            counter += 1
    result = re.sub(r'(<MidiTrack Id=")(\d+)(")',
                    lambda m: f'{m.group(1)}{track_id}{m.group(3)}',
                    track_xml, count=1)
    def replacer(m):
        old = m.group(1)
        return f'Id="{id_map[old]}"' if (int(old) > 1000 and old in id_map) else m.group(0)
    result = re.sub(r'Id="(\d+)"', replacer, result)
    display_num = rewire_index + 1
    result = re.sub(r'(<EffectiveName Value=")[^"]*(")',
                    lambda m: f'{m.group(1)}{display_num}-{new_name}{m.group(2)}', result, count=1)
    result = re.sub(r'(<EffectiveName Value="[^"]*" />\s*<UserName Value=")[^"]*(")',
                    lambda m: f'{m.group(1)}{new_name}{m.group(2)}',
                    result, count=1)
    result = re.sub(r'(<Color Value=")(\d+)(")',
                    lambda m: f'{m.group(1)}{new_colour}{m.group(3)}', result, count=1)
    result = re.sub(r'(<ReWireDeviceMidiTargetId Value=")(\d+)(")',
                    lambda m: f'{m.group(1)}{rewire_index}{m.group(3)}', result, count=1)
    return result, counter

def _als_extract_blocks(content, tag):
    blocks    = []
    start_pat = re.compile(rf'\t\t\t<{tag} ')
    end_pat   = re.compile(rf'</{tag}>')
    starts    = [m.start() for m in start_pat.finditer(content)]
    ends      = [m.end()   for m in end_pat.finditer(content)]
    for s, e in zip(starts, ends):
        blocks.append(content[s:e])
    return blocks

def _als_set_bpm(xml, bpm):
    xml = re.sub(r'(<Tempo>.*?<Manual Value=")[\d.]+(")',
                 lambda m: f'{m.group(1)}{bpm}{m.group(2)}',
                 xml, count=1, flags=re.DOTALL)
    xml = re.sub(r'(<FloatEvent Id="0" Time="-63072000" Value=")[\d.]+(")',
                 lambda m: f'{m.group(1)}{bpm}{m.group(2)}',
                 xml, count=1)
    return xml

def _als_expand_scenes(xml, n_scenes):
    """
    Expand the LiveSet Scenes block from 8 to n_scenes.
    """
    if n_scenes <= 8:
        return xml
    scene_tpl = (
        '\t\t\t<Scene Id="{i}">\n'
        '\t\t\t\t<FollowAction>\n'
        '\t\t\t\t\t<FollowTime Value="4" />\n'
        '\t\t\t\t\t<IsLinked Value="true" />\n'
        '\t\t\t\t\t<LoopIterations Value="1" />\n'
        '\t\t\t\t\t<FollowActionA Value="4" />\n'
        '\t\t\t\t\t<FollowActionB Value="0" />\n'
        '\t\t\t\t\t<FollowChanceA Value="100" />\n'
        '\t\t\t\t\t<FollowChanceB Value="0" />\n'
        '\t\t\t\t\t<JumpIndexA Value="0" />\n'
        '\t\t\t\t\t<JumpIndexB Value="0" />\n'
        '\t\t\t\t\t<FollowActionEnabled Value="false" />\n'
        '\t\t\t\t</FollowAction>\n'
        '\t\t\t\t<Name Value="" />\n'
        '\t\t\t\t<Annotation Value="" />\n'
        '\t\t\t\t<Color Value="-1" />\n'
        '\t\t\t\t<Tempo Value="120" />\n'
        '\t\t\t\t<IsTempoEnabled Value="false" />\n'
        '\t\t\t\t<TimeSignatureId Value="201" />\n'
        '\t\t\t\t<IsTimeSignatureEnabled Value="false" />\n'
        '\t\t\t\t<LomId Value="0" />\n'
        '\t\t\t\t<ClipSlotsListWrapper LomId="0" />\n'
        '\t\t\t</Scene>\n'
    )
    extra = ''.join(scene_tpl.format(i=i) for i in range(8, n_scenes))
    return xml.replace('\t\t</Scenes>', extra + '\t\t</Scenes>', 1)


def _als_expand_all_clipslots(xml, n_scenes):
    """
    Expand EVERY ClipSlotList in the entire ALS XML from 8 to n_scenes slots.
    This covers MidiTracks, ReturnTracks, MasterTrack, PreHearTrack - all of them.
    Ableton requires all tracks to have the same slot count as Scenes.
    """
    if n_scenes <= 8:
        return xml

    slot_tpl = (
        '\t\t\t\t\t\t\t<ClipSlot Id="{i}">\n'
        '\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
        '\t\t\t\t\t\t\t\t<ClipSlot>\n'
        '\t\t\t\t\t\t\t\t\t<Value />\n'
        '\t\t\t\t\t\t\t\t</ClipSlot>\n'
        '\t\t\t\t\t\t\t\t<HasStop Value="true" />\n'
        '\t\t\t\t\t\t\t\t<NeedRefreeze Value="true" />\n'
        '\t\t\t\t\t\t\t</ClipSlot>\n'
    )
    extra = ''.join(slot_tpl.format(i=i) for i in range(8, n_scenes))

    # Replace every occurrence of </ClipSlotList> with extra slots + </ClipSlotList>
    return xml.replace(
        '\t\t\t\t\t\t</ClipSlotList>',
        extra + '\t\t\t\t\t\t</ClipSlotList>'
    )


def _als_expand_clipslots(track_xml, n_scenes):
    """
    Expand ALL ClipSlotLists in a track (MainSequencer + FreezeSequencer) from 8 to n_scenes.
    Must run BEFORE _inject_clips so slots 8+ exist when clips are injected.
    Ableton requires every ClipSlotList to have the same count as Scenes.
    """
    if n_scenes <= 8:
        return track_xml

    slot_tpl = (
        '\t\t\t\t\t\t\t<ClipSlot Id="{i}">\n'
        '\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
        '\t\t\t\t\t\t\t\t<ClipSlot>\n'
        '\t\t\t\t\t\t\t\t\t<Value />\n'
        '\t\t\t\t\t\t\t\t</ClipSlot>\n'
        '\t\t\t\t\t\t\t\t<HasStop Value="true" />\n'
        '\t\t\t\t\t\t\t\t<NeedRefreeze Value="true" />\n'
        '\t\t\t\t\t\t\t</ClipSlot>\n'
    )
    extra = ''.join(slot_tpl.format(i=i) for i in range(8, n_scenes))
    needle = '\t\t\t\t\t\t</ClipSlotList>'
    # Replace ALL occurrences in the track (MainSequencer + FreezeSequencer)
    return track_xml.replace(needle, extra + needle)


def _make_drum_branch(rel_path, pad_data, display_name, receiving_note, id_start,
                      wav_abs_path=None, bus_mode=False, midi_track_id=None,
                      rt_ids=None, koala_bus=-1, chopper_params=None):
    """Build one DrumBranch XML block for a single pad.

    wav_abs_path:   absolute path to the extracted WAV on disk (used for trim).
    bus_mode:       if True, inject AudioBranchSendInfo and update pad routing.
    midi_track_id:  ALS MidiTrack ID (needed for routing target string).
    rt_ids:         list of 4 ReturnTrack IDs [A,B,C,D].
    koala_bus:      Koala bus value for this pad (-1=master, 0-3=bus).
    chopper_params: dict from _get_chopper_params() - switches Simpler to
                    Slice mode, sets SendingNote=35, injects MidiRandom for
                    Random trigger mode.
    """
    tpl = _tpl(_DRUM_BRANCH_TPL_B64)

    one_shot  = str(pad_data.get("oneshot",  "false")).lower() == "true"
    loop_on   = str(pad_data.get("looping",  "false")).lower() == "true"
    choke_grp = int(pad_data.get("chokeGroup", 0) or 0)
    is_warped = "true" if pad_data.get("stretching") is True else "false"

    playback_mode = "1" if one_shot else "0"
    loop_on_val   = "true" if loop_on else "false"

    # Trim: Koala 0.0->1.0 proportion of total frames to skip at the start.
    # We add trim_frames to start_pt (clamped so start < end).
    # Requires wav_abs_path to read the actual frame count.
    koala_trim = float(pad_data.get("trim", 0.0) or 0.0)
    trim_frames = 0
    if koala_trim > 0.0 and wav_abs_path and os.path.isfile(wav_abs_path):
        try:
            with wave.open(wav_abs_path, 'rb') as _wf:
                total_frames = _wf.getnframes()
            trim_frames = int(round(koala_trim * total_frames))
        except Exception:
            trim_frames = 0   # malformed WAV - silently ignore

    start_pt = int(pad_data.get("start", 0) or 0) + trim_frames
    end_pt   = int(pad_data.get("end",   0) or 0)
    # Ensure start never meets or exceeds end (when end is non-zero)
    if end_pt > 0:
        start_pt = min(start_pt, end_pt - 1)

    # Volume: Koala vol is perceptual (unity=1.0). ALS uses vol^4 as linear amplitude.
    # koala 0->silence, 1.0->0dB, ~2.0->+24dB
    _ALS_VOL_MIN = 0.0003162277571  # -70 dB ≈ silence
    koala_vol = float(pad_data.get("vol", 1.0) or 1.0)
    als_vol   = max(_ALS_VOL_MIN, koala_vol ** 4)

    # Pan: Koala 0.0=full left, 0.5=centre, 1.0=full right -> ALS -1, 0, +1
    koala_pan = float(pad_data.get("pan", 0.5) or 0.5)
    als_pan   = round(koala_pan * 2.0 - 1.0, 10)

    # Pitch: Koala pitch in semitones -> ALS TransposeKey in semitones (direct)
    koala_pitch = float(pad_data.get("pitch", 0.0) or 0.0)

    # Speed: Koala playback speed (0.5–2.0, default 1.0) -> additional semitone offset.
    # speed = 2^(semitones/12)  ->  semitones = 12 * log2(speed)
    # We split into integer semitones (added to TransposeKey) and fractional cents
    # (added to TransposeFine, clamped to ±50 so the two combine cleanly).
    koala_speed = float(pad_data.get("speed", 1.0) or 1.0)
    if abs(koala_speed - 1.0) < 1e-6:
        speed_semitones_total = 0.0
    else:
        speed_semitones_total = 12.0 * math.log2(max(koala_speed, 1e-6))
    speed_semi_int  = int(round(speed_semitones_total))  # whole semitones
    speed_cents     = (speed_semitones_total - speed_semi_int) * 100.0  # leftover cents

    als_pitch = int(round(koala_pitch)) + speed_semi_int

    # Tune: Koala fine-tune −1.0->+1.0 maps to ±100 cents -> ALS TransposeFine (−50->+50 range)
    # We combine speed remainder cents with explicit tune cents and clamp to ±50.
    koala_tune = float(pad_data.get("tune", 0.0) or 0.0)
    tune_cents = koala_tune * 100.0          # Koala ±1.0 -> ±100 cents
    als_fine   = tune_cents + speed_cents    # combine
    als_fine   = max(-50.0, min(50.0, als_fine))  # ALS TransposeFine range

    # Attack: log-interpolate koala 0.00011->3.0 to ALS 0.1->20000ms
    _ALS_ATK_MIN  = 0.1000000015
    _ALS_ATK_MAX  = 20000.0
    _KOA_ATK_MIN  = 0.00011
    _KOA_ATK_MAX  = 3.0
    koala_attack  = float(pad_data.get("attack", _KOA_ATK_MIN) or _KOA_ATK_MIN)
    if koala_attack <= _KOA_ATK_MIN:
        als_attack = _ALS_ATK_MIN
    elif koala_attack >= _KOA_ATK_MAX:
        als_attack = _ALS_ATK_MAX
    else:
        _t = (math.log(koala_attack) - math.log(_KOA_ATK_MIN)) /              (math.log(_KOA_ATK_MAX) - math.log(_KOA_ATK_MIN))
        als_attack = _ALS_ATK_MIN * (_ALS_ATK_MAX / _ALS_ATK_MIN) ** _t

    # Release: log-interpolate koala 0.0->3.0 to ALS 1->60000ms
    _ALS_REL_MIN  = 1.0
    _ALS_REL_MAX  = 60000.0
    _KOA_REL_MAX  = 3.0
    koala_release = float(pad_data.get("release", 0.0) or 0.0)
    if koala_release <= 0.0:
        als_release = _ALS_REL_MIN
    elif koala_release >= _KOA_REL_MAX:
        als_release = _ALS_REL_MAX
    else:
        _t = koala_release / _KOA_REL_MAX
        als_release = _ALS_REL_MIN * (_ALS_REL_MAX / _ALS_REL_MIN) ** _t

    # FadeIn / FadeOut: Koala 0.0->1.0 -> ALS OneShotEnvelope FadeInTime/FadeOutTime (0->2000 ms).
    # Koala uses a linear-feeling slider; we map it linearly to ALS ms range.
    # FadeOut default in template is 0.1 ms (≈0 in UI); when Koala fadeOut==0 we keep
    # the ALS default of 0.1 ms so the existing release envelope governs decay naturally.
    _ALS_FADE_MAX = 2000.0   # ms
    koala_fade_in  = float(pad_data.get("fadeIn",  0.0) or 0.0)
    koala_fade_out = float(pad_data.get("fadeOut", 0.0) or 0.0)
    als_fade_in    = koala_fade_in  * _ALS_FADE_MAX   # 0->2000 ms
    # Keep Ableton's near-zero default (0.1 ms) when fadeOut is unset in Koala.
    als_fade_out   = koala_fade_out * _ALS_FADE_MAX if koala_fade_out > 0.0 else 0.1000000015

    # Patch all per-pad fields
    tpl = re.sub(r'<EffectiveName Value="[^"]*"',
                 f'<EffectiveName Value="{display_name}"', tpl, count=1)
    tpl = re.sub(r'<Name Value="[^"]*"',
                 f'<Name Value="{display_name}"', tpl, count=1)
    # WAV RelativePath (the one with .wav)
    tpl = re.sub(r'<RelativePath Value="[^"]*\.wav[^"]*"',
                 f'<RelativePath Value="{rel_path}"', tpl, count=1)
    tpl = re.sub(r'<SampleStart Value="[^"]*"',
                 f'<SampleStart Value="{start_pt}"', tpl, count=1)
    tpl = re.sub(r'<SampleEnd Value="[^"]*"',
                 f'<SampleEnd Value="{end_pt}"', tpl, count=1)
    # LoopOn Manual value
    tpl = re.sub(r'(<LoopOn>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{loop_on_val}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)
    tpl = re.sub(r'<PlaybackMode Value="[^"]*"',
                 f'<PlaybackMode Value="{playback_mode}"', tpl, count=1)
    tpl = re.sub(r'<IsWarped Value="[^"]*"',
                 f'<IsWarped Value="{is_warped}"', tpl, count=1)
    tpl = re.sub(r'<ReceivingNote Value="[^"]*"',
                 f'<ReceivingNote Value="{receiving_note}"', tpl, count=1)
    tpl = re.sub(r'<ChokeGroup Value="[^"]*"',
                 f'<ChokeGroup Value="{choke_grp}"', tpl, count=1)

    # Volume (direct <Volume Value=...> attribute in SampleData)
    tpl = re.sub(r'(<Volume Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_vol:.10g}{m.group(2)}', tpl, count=1)

    # Pan: Panorama Manual in VolumeAndPan section
    tpl = re.sub(r'(<VolumeAndPan>.*?<Panorama>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_pan:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Pitch: TransposeKey Manual in Pitch section
    tpl = re.sub(r'(<Pitch>.*?<TransposeKey>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_pitch}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Tune / Speed fine: TransposeFine Manual in Pitch section
    tpl = re.sub(r'(<Pitch>.*?<TransposeFine>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_fine:.6g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Attack: AttackTime Manual
    tpl = re.sub(r'(<AttackTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_attack:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Release: ReleaseTime Manual
    tpl = re.sub(r'(<ReleaseTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_release:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # FadeIn -> OneShotEnvelope FadeInTime Manual
    tpl = re.sub(r'(<FadeInTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_fade_in:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # FadeOut -> OneShotEnvelope FadeOutTime Manual
    tpl = re.sub(r'(<FadeOutTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_fade_out:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Tone -> Filter (LP if tone<0, HP if tone>0, off if tone==0)
    # Frequency always 1001.69135 Hz (≈1kHz) when active.
    koala_tone = float(pad_data.get("tone", 0.0) or 0.0)
    if abs(koala_tone) < 1e-6:
        als_filter_type = None        # filter off
    elif koala_tone < 0:
        als_filter_type = "0"         # Low Pass
    else:
        als_filter_type = "1"         # High Pass

    # Set Filter IsOn in template (simple string swap, safe before remap)
    als_filter_on = "false" if als_filter_type is None else "true"
    tpl = re.sub(
        r'(<Filter>.*?<IsOn>.*?<Manual Value=")[^"]*(")',
        lambda m: f'{m.group(1)}{als_filter_on}{m.group(2)}',
        tpl, count=1, flags=re.DOTALL)

    # Mute: Koala muted=True -> ALS Speaker Manual=false
    # Chopper pads store muted in padParams; normal pads store it top-level.
    if chopper_params is not None:
        pad_muted = bool(pad_data.get('synthParams', {}).get('padParams', {}).get('muted', False))
    else:
        pad_muted = bool(pad_data.get('muted', False))
    if pad_muted:
        tpl = re.sub(
            r'(<Speaker>.*?<Manual Value=")[^"]*(")',
            lambda m: f'{m.group(1)}false{m.group(2)}',
            tpl, count=1, flags=re.DOTALL)

    # Remap all IDs to fresh unique values

    # Clear WarpMarkers to a clean single-origin marker.
    # The template has hardcoded markers from the reference sample that would
    # incorrectly warp every pad to those timecodes.
    tpl = re.sub(
        r'<WarpMarkers>.*?</WarpMarkers>',
        '<WarpMarkers>\n'
        '\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t'
        '<WarpMarker Id="0" SecTime="0" BeatTime="0" />\n'
        '\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t</WarpMarkers>',
        tpl, count=1, flags=re.DOTALL
    )
    remapped, id_start = _als_remap_ids(tpl, id_start)

    # If filter active, inject SimplerFilter into drum branch filter slot.
    # NOTE: SimplerFilter works in drum rack branches. It crashes in standalone
    # Simpler tracks (Live 12.3.7 issue) - those use an empty slot instead.
    if als_filter_type is not None:
        t15 = "\t" * 15; t16 = "\t" * 16; t17 = "\t" * 17
        t18 = "\t" * 18
        # freq = |tone| * 1000 Hz, clamped to [30, 1000]
        # Normalise: Koala LP max=-0.99, HP max=+0.30, both -> 1000Hz
        freq_str = f"{min(1000.0, max(30.0, (abs(koala_tone) / (0.99 if koala_tone < 0 else 0.30)) * 1000.0)):.6g}"

        def _uid_sf():
            nonlocal id_start
            v = id_start; id_start += 1; return v

        sf = (
            f"{t16}<SimplerFilter Id=\"0\">\n"
            f"{t17}<LegacyType>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"0\" />\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<MidiControllerRange><Min Value=\"0\" /><Max Value=\"5\" /></MidiControllerRange>\n"
            f"{t17}</LegacyType>\n"
            f"{t17}<Type>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"{als_filter_type}\" />\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<MidiControllerRange><Min Value=\"0\" /><Max Value=\"4\" /></MidiControllerRange>\n"
            f"{t17}</Type>\n"
            f"{t17}<CircuitLpHp>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"0\" />\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<MidiControllerRange><Min Value=\"0\" /><Max Value=\"4\" /></MidiControllerRange>\n"
            f"{t17}</CircuitLpHp>\n"
            f"{t17}<CircuitBpNoMo>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"0\" />\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<MidiControllerRange><Min Value=\"0\" /><Max Value=\"1\" /></MidiControllerRange>\n"
            f"{t17}</CircuitBpNoMo>\n"
            f"{t17}<Slope>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"true\" />\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<MidiCCOnOffThresholds><Min Value=\"64\" /><Max Value=\"127\" /></MidiCCOnOffThresholds>\n"
            f"{t17}</Slope>\n"
            f"{t17}<Freq>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"{freq_str}\" />\n"
            f"{t18}<MidiControllerRange><Min Value=\"30\" /><Max Value=\"22000\" /></MidiControllerRange>\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<ModulationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></ModulationTarget>\n"
            f"{t17}</Freq>\n"
            f"{t17}<LegacyQ>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"0.6999999881\" />\n"
            f"{t18}<MidiControllerRange><Min Value=\"0.3000000119\" /><Max Value=\"10\" /></MidiControllerRange>\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<ModulationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></ModulationTarget>\n"
            f"{t17}</LegacyQ>\n"
            f"{t17}<Res>\n{t18}<LomId Value=\"0\" />\n{t18}<Manual Value=\"0\" />\n"
            f"{t18}<MidiControllerRange><Min Value=\"0\" /><Max Value=\"1.25\" /></MidiControllerRange>\n"
            f"{t18}<AutomationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></AutomationTarget>\n"
            f"{t18}<ModulationTarget Id=\"{_uid_sf()}\"><LockEnvelope Value=\"0\" /></ModulationTarget>\n"
            f"{t17}</Res>\n"
            f"{t16}</SimplerFilter>\n"
        )
        remapped = re.sub(
            r'(<Filter>.*?<Slot>\s*)<Value />',
            lambda m: m.group(1) + f'<Value>\n{sf}{t15}</Value>',
            remapped, count=1, flags=re.DOTALL)

    # -- Chopper mode: Slice Simpler + SendingNote=35 + optional MidiRandom -
    if chopper_params:
        N = chopper_params['slice_count']
        is_auto = chopper_params.get('slice_mode', 1.0) == 0.0
        # Switch Simpler to Slice mode (Globals PlaybackMode=2)
        remapped = re.sub(r'<PlaybackMode Value="[^"]*"',
                          '<PlaybackMode Value="2"', remapped, count=1)
        # SlicingStyle: 3=Manual (Auto chop), 2=Region/Equal
        slicing_style = "3" if is_auto else "2"
        remapped = re.sub(r'<SlicingStyle Value="[^"]*"',
                          f'<SlicingStyle Value="{slicing_style}"', remapped, count=1)
        remapped = re.sub(r'<SlicingRegions Value="[^"]*"',
                          f'<SlicingRegions Value="{N}"', remapped, count=1)
        # NumVoices: 1=mono, 5=poly
        num_voices = "1" if chopper_params['mono'] else "5"
        remapped = re.sub(r'<NumVoices Value="[^"]*"',
                          f'<NumVoices Value="{num_voices}"', remapped, count=1)
        # SimplerSlicing PlaybackMode: 0=Gate, 2=Thru
        # ONE SHOT does not change slice playback mode (it's handled by NumVoices=1)
        if chopper_params['play_thru']:
            slicing_pm = "2"
        else:
            slicing_pm = "0"
        remapped = re.sub(r'(<SimplerSlicing>\s*)<PlaybackMode Value="[^"]*"',
                          lambda m: m.group(1) + f'<PlaybackMode Value="{slicing_pm}"',
                          remapped, count=1, flags=re.DOTALL)
        # SendingNote: Random=35 (B0), all other chopper=36 (C1)
        sending_note = '35' if chopper_params['trigger_mode'] == 2.0 else '36'
        remapped = remapped.replace('<SendingNote Value="60" />',
                                    f'<SendingNote Value="{sending_note}" />', 1)
        # Auto chop: inject ManualSlicePoints (tab=16 for drum branch Simpler)
        if is_auto and chopper_params.get('slice_starts'):
            msp_xml = _manual_slice_points_xml(chopper_params['slice_starts'], tab_level=16)
            remapped = remapped.replace('<ManualSlicePoints />\n', msp_xml, 1)
            if '<ManualSlicePoints />\n' not in remapped:
                remapped = re.sub(r'<ManualSlicePoints>\s*</ManualSlicePoints>',
                                  msp_xml.rstrip(), remapped, count=1)
        # Random trigger: inject MidiRandom inside drum branch Devices (tab=13)
        if chopper_params['trigger_mode'] == 2.0:
            mr_xml, id_start = _midi_random_device_xml(N, id_start, tab_level=13)
            remapped = remapped.replace('<Devices>\n', '<Devices>\n' + mr_xml, 1)
        # EQ: inject EQ8 after Simpler if pad EQ enabled (tab=13 for drum branch)
        eq_data_c = chopper_params.get('eq', {})
        if eq_data_c and str(eq_data_c.get('enabled', 'false')).lower() == 'true':
            eq8_xml, id_start = _eq8_device_xml(eq_data_c, id_start, tab_level=13)
            remapped = re.sub(r'(</OriginalSimpler>)(\s*</Devices>)',
                              lambda m: m.group(1) + '\n' + eq8_xml + m.group(2),
                              remapped, count=1)
    else:
        # Normal (non-chopper) pad: check for pad-level EQ
        eq_data_n = pad_data.get('eq', {})
        if eq_data_n and str(eq_data_n.get('enabled', 'false')).lower() == 'true':
            eq8_xml, id_start = _eq8_device_xml(eq_data_n, id_start, tab_level=13)
            remapped = re.sub(r'(</OriginalSimpler>)(\s*</Devices>)',
                              lambda m: m.group(1) + '\n' + eq8_xml + m.group(2),
                              remapped, count=1)

    # Bus mode: inject AudioBranchSendInfo into each pad's AudioBranchMixerDevice,
    # update the RoutingHelper target, and add TrackSendHolder entries.
    if bus_mode and rt_ids and midi_track_id is not None:
        # 1. Replace <SendInfos /> with populated AudioBranchSendInfo block
        send_infos_xml, id_start = _bus_audio_branch_send_infos(koala_bus, id_start)
        remapped = remapped.replace('<SendInfos />\n', send_infos_xml, 1)

        # 2. Update the RoutingHelper target in AudioBranchMixerDevice
        if koala_bus >= 0:
            # Find DrumGroupDevice ID from the track-level remapped XML is not
            # available here; it will be patched in _make_drum_rack_device_chain
            # after the drum device ID is known. Store a placeholder token.
            chain_idx   = koala_bus + 1   # R1..R4
            target_enum = koala_bus + 1
            track_num   = midi_track_id
            # We use a placeholder for drum_device_id - patched after remap
            target_str  = f'AudioOut/Track.{track_num}/DeviceIn.__DRUMDEVID__.R{chain_idx},ChainIn'
            upper_str   = f'__GROUPNAME__'
            lower_str   = f'__GROUPNAME__ | {_BUS_RETURN_CHAIN_LETTERS[koala_bus]} Return Chain | Chain In'
            remapped = re.sub(
                r'(<RoutingHelper>.*?<Target Value=")AudioOut/None(")',
                lambda m: m.group(1) + target_str + m.group(2),
                remapped, count=1, flags=re.DOTALL)
            remapped = re.sub(
                r'(<RoutingHelper>.*?<UpperDisplayString Value=")No Output(")',
                lambda m: m.group(1) + upper_str + m.group(2),
                remapped, count=1, flags=re.DOTALL)
            remapped = re.sub(
                r'(<RoutingHelper>.*?<LowerDisplayString Value=")[^"]*(")',
                lambda m: m.group(1) + lower_str + m.group(2),
                remapped, count=1, flags=re.DOTALL)
            remapped = re.sub(
                r'(<RoutingHelper>.*?<TargetEnum Value=")0(")',
                lambda m: m.group(1) + str(target_enum) + m.group(2),
                remapped, count=1, flags=re.DOTALL)

    return remapped, id_start


def _make_drum_rack_device_chain(adg_pads, group_index, id_start,
                                 bus_mode=False, midi_track_id=None,
                                 rt_ids=None, pad_bus_map=None):
    """
    Build a complete inner DeviceChain for a drum rack track.
    adg_pads:      list of (pad_num, pad_data, wav_name, rel_path, rel_path_type, wav_abs_path)
    bus_mode:      if True, inject bus routing into each branch and ReturnBranches block
    midi_track_id: ALS MidiTrack ID (needed for bus routing strings)
    rt_ids:        list of 4 ReturnTrack IDs [A,B,C,D] (bus_mode only)
    pad_bus_map:   dict pad_num -> koala bus int (bus_mode only)
    Returns (device_chain_xml, next_id_start)
    """
    GROUP_BASE_NOTES = [80, 80, 80, 80]
    base_note = GROUP_BASE_NOTES[group_index]

    # Build branches XML
    branches_xml = ""
    for pad_tuple in adg_pads:
        pad_num, pad_data, wav_name, rel_path = pad_tuple[0], pad_tuple[1], pad_tuple[2], pad_tuple[3]
        wav_abs    = pad_tuple[5]
        chopper_p  = pad_tuple[6] if len(pad_tuple) > 6 else None
        pad_in_bank    = pad_num % 16
        row            = pad_in_bank // 4
        col            = pad_in_bank % 4
        receiving_note = base_note - col + row * 4
        display_name   = os.path.splitext(wav_name)[0]

        koala_bus = (pad_bus_map or {}).get(pad_num, -1) if bus_mode else -1
        branch_xml, id_start = _make_drum_branch(
            rel_path, pad_data, display_name, receiving_note, id_start, wav_abs,
            bus_mode=bus_mode, midi_track_id=midi_track_id,
            rt_ids=rt_ids, koala_bus=koala_bus, chopper_params=chopper_p)
        branches_xml += branch_xml + "\n"

    # Get the drum rack DeviceChain template and inject branches.
    # The template was extracted with the outer MidiTrack </DeviceChain> included,
    # so strip that last one -- it belongs to the MidiTrack, not the device chain.
    rack_tpl = _tpl(_DRUM_RACK_TPL_B64)
    last_close = rack_tpl.rfind('</DeviceChain>')
    rack_tpl = rack_tpl[:last_close].rstrip()

    # Clear LastPresetRef (no .adg file on disk) and set UserName on the
    # DrumGroupDevice so Ableton displays the group name instead of "Drum Rack".
    rack_tpl = re.sub(
        r'<LastPresetRef>.*?</LastPresetRef>',
        '<LastPresetRef>\n\t\t\t\t\t\t\t\t\t<Value />\n\t\t\t\t\t\t\t\t</LastPresetRef>',
        rack_tpl, count=1, flags=re.DOTALL
    )
    # group_name e.g. "Group A" - set as UserName on DrumGroupDevice
    group_name_str = GROUPS[group_index]
    rack_tpl = rack_tpl.replace(
        '<UserName Value="" />',
        f'<UserName Value="{group_name_str}" />',
        1
    )
    # Remap the rack's own IDs BEFORE injecting branches so branch IDs
    # (already uniquely assigned per pad) are not remapped a second time.
    remapped, id_start = _als_remap_ids(rack_tpl, id_start)

    # Now inject the pre-remapped branches into the remapped rack
    remapped = remapped.replace(
        '<Branches>\n\t\t\t\t\t\t\t\t</Branches>',
        '<Branches>\n' + branches_xml + '\t\t\t\t\t\t\t\t</Branches>',
        1
    )

    # Bus mode: resolve placeholders, inject ReturnBranches
    if bus_mode and rt_ids and midi_track_id is not None:
        # Extract the DrumGroupDevice ID from the remapped XML
        dg_match = re.search(r'<DrumGroupDevice Id="(\d+)"', remapped)
        drum_device_id = int(dg_match.group(1)) if dg_match else 0
        group_name = GROUPS[group_index]

        # Resolve placeholders written by _make_drum_branch
        track_display = f'{list(GROUPS).index(group_name) + 1}-{group_name}'
        remapped = remapped.replace('__DRUMDEVID__', str(drum_device_id))
        remapped = remapped.replace('__GROUPNAME__', track_display)

        rb_xml, id_start = _bus_return_branches_xml(
            group_name, midi_track_id, drum_device_id, rt_ids, id_start)
        remapped = remapped.replace(
            '<ReturnBranches />\n', rb_xml, 1)

    return remapped, id_start


# Koala EQ type -> Ableton EQ Eight Mode
_KOALA_EQ_TYPE_TO_EQ8_MODE = {
    'lowshelf':  '1',
    'peaking':   '2',
    'highshelf': '4',
}

def _eq8_device_xml(eq_data, id_start, tab_level=13):
    """Build an Ableton EQ Eight device XML block from Koala EQ parameters.

    eq_data: dict with keys 'lo', 'mid', 'hi' - each containing
             freq (Hz), gain (dB), q, type (lowshelf/peaking/highshelf)
    Maps onto EQ8 bands 0/1/2 (lo/mid/hi). Bands 3-7 are inactive defaults.
    Tab level 13 for drum branch, 7 for standalone Simpler track.
    """
    def _uid():
        nonlocal id_start
        v = id_start; id_start += 1; return v

    T  = '\t' * tab_level        # device level
    T1 = '\t' * (tab_level + 1)  # param level
    T2 = '\t' * (tab_level + 2)
    T3 = '\t' * (tab_level + 3)
    T4 = '\t' * (tab_level + 4)

    dev_id  = _uid()
    on_at   = _uid()
    pt_id   = _uid()
    psr_id  = _uid()

    def _band_xml(band_idx, is_on, mode, freq, gain, q_val, id_start_ref):
        """Generate one Bands.N block."""
        nonlocal id_start
        on_at_a   = _uid(); mode_at_a = _uid(); freq_at_a = _uid()
        freq_mt_a = _uid(); gain_at_a = _uid(); gain_mt_a = _uid()
        q_at_a    = _uid(); q_mt_a    = _uid()
        on_at_b   = _uid(); freq_at_b = _uid(); freq_mt_b = _uid()
        gain_at_b = _uid(); gain_mt_b = _uid(); q_at_b = _uid(); q_mt_b = _uid()

        is_on_str = "true" if is_on else "false"
        gain_str  = f"{gain:.10g}"
        freq_str  = f"{freq:.10g}"
        q_str     = f"{q_val:.10g}"

        return (
            f"{T1}<Bands.{band_idx}>\n"
            f"{T2}<ParameterA>\n"
            f"{T3}<IsOn>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"{is_on_str}\" />\n"
            f"{T4}<AutomationTarget Id=\"{on_at_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<MidiCCOnOffThresholds><Min Value=\"64\" /><Max Value=\"127\" /></MidiCCOnOffThresholds>\n"
            f"{T3}</IsOn>\n"
            f"{T3}<Mode>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"{mode}\" />\n"
            f"{T4}<AutomationTarget Id=\"{mode_at_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<MidiControllerRange><Min Value=\"0\" /><Max Value=\"7\" /></MidiControllerRange>\n"
            f"{T3}</Mode>\n"
            f"{T3}<Freq>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"{freq_str}\" />\n"
            f"{T4}<MidiControllerRange><Min Value=\"10\" /><Max Value=\"22000\" /></MidiControllerRange>\n"
            f"{T4}<AutomationTarget Id=\"{freq_at_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<ModulationTarget Id=\"{freq_mt_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</ModulationTarget>\n"
            f"{T3}</Freq>\n"
            f"{T3}<Gain>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"{gain_str}\" />\n"
            f"{T4}<MidiControllerRange><Min Value=\"-15\" /><Max Value=\"15\" /></MidiControllerRange>\n"
            f"{T4}<AutomationTarget Id=\"{gain_at_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<ModulationTarget Id=\"{gain_mt_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</ModulationTarget>\n"
            f"{T3}</Gain>\n"
            f"{T3}<Q>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"{q_str}\" />\n"
            f"{T4}<MidiControllerRange><Min Value=\"0.1000000015\" /><Max Value=\"18\" /></MidiControllerRange>\n"
            f"{T4}<AutomationTarget Id=\"{q_at_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<ModulationTarget Id=\"{q_mt_a}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</ModulationTarget>\n"
            f"{T3}</Q>\n"
            f"{T2}</ParameterA>\n"
            f"{T2}<ParameterB>\n"
            f"{T3}<IsOn>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"false\" />\n"
            f"{T4}<AutomationTarget Id=\"{on_at_b}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<MidiCCOnOffThresholds><Min Value=\"64\" /><Max Value=\"127\" /></MidiCCOnOffThresholds>\n"
            f"{T3}</IsOn>\n"
            f"{T3}<Mode>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"1\" />\n"
            f"{T4}<MidiControllerRange><Min Value=\"0\" /><Max Value=\"7\" /></MidiControllerRange>\n"
            f"{T3}</Mode>\n"
            f"{T3}<Freq>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"40\" />\n"
            f"{T4}<MidiControllerRange><Min Value=\"10\" /><Max Value=\"22000\" /></MidiControllerRange>\n"
            f"{T4}<AutomationTarget Id=\"{freq_at_b}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<ModulationTarget Id=\"{freq_mt_b}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</ModulationTarget>\n"
            f"{T3}</Freq>\n"
            f"{T3}<Gain>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"0\" />\n"
            f"{T4}<MidiControllerRange><Min Value=\"-15\" /><Max Value=\"15\" /></MidiControllerRange>\n"
            f"{T4}<AutomationTarget Id=\"{gain_at_b}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<ModulationTarget Id=\"{gain_mt_b}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</ModulationTarget>\n"
            f"{T3}</Gain>\n"
            f"{T3}<Q>\n"
            f"{T4}<LomId Value=\"0\" />\n"
            f"{T4}<Manual Value=\"0.7071067095\" />\n"
            f"{T4}<MidiControllerRange><Min Value=\"0.1000000015\" /><Max Value=\"18\" /></MidiControllerRange>\n"
            f"{T4}<AutomationTarget Id=\"{q_at_b}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</AutomationTarget>\n"
            f"{T4}<ModulationTarget Id=\"{q_mt_b}\">\n{T4}\t<LockEnvelope Value=\"0\" />\n{T4}</ModulationTarget>\n"
            f"{T3}</Q>\n"
            f"{T2}</ParameterB>\n"
            f"{T1}</Bands.{band_idx}>\n"
        )

    # Map the 3 Koala bands to EQ8 positions matching Ableton layout.
    # lo  -> Band 0, Mode=2 (Bell at low freq)
    # mid -> Band 2, Mode=3 (Bell/Peak)
    # hi  -> Band 3, Mode=5 (High Cut)
    # This matches the reference mod file structure.
    lo_bd  = eq_data.get('lo',  {})
    mid_bd = eq_data.get('mid', {})
    hi_bd  = eq_data.get('hi',  {})

    Q = 0.7071067095  # Ableton EQ8 default Q

    # 8 bands: (band_idx, is_on, mode, freq, gain)
    band_specs = [
        (0, True,  '2', float(lo_bd.get('freq',  100.0)), float(lo_bd.get('gain',  0.0))),  # lo
        (1, False, '3', 200.0,                             0.0),                              # inactive
        (2, True,  '3', float(mid_bd.get('freq', 1000.0)), float(mid_bd.get('gain', 0.0))), # mid
        (3, True,  '5', float(hi_bd.get('freq',  8000.0)), float(hi_bd.get('gain',  0.0))), # hi
        (4, False, '3', 100.0,   0.0),
        (5, False, '3', 10000.0, 0.0),
        (6, False, '3', 5000.0,  0.0),
        (7, False, '6', 18000.0, 0.0),
    ]

    bands_xml = ""
    for band_idx, is_on, mode, freq, gain in band_specs:
        bands_xml += _band_xml(band_idx, is_on, mode, freq, gain, Q, id_start)

    # Global gain AutomationTarget
    gg_at = _uid(); gg_mt = _uid()
    sc_at = _uid()

    xml = (
        f"{T}<Eq8 Id=\"{dev_id}\">\n"
        f"{T1}<LomId Value=\"0\" />\n"
        f"{T1}<LomIdView Value=\"0\" />\n"
        f"{T1}<IsExpanded Value=\"false\" />\n"
        f"{T1}<BreakoutIsExpanded Value=\"false\" />\n"
        f"{T1}<On>\n"
        f"{T2}<LomId Value=\"0\" />\n"
        f"{T2}<Manual Value=\"true\" />\n"
        f"{T2}<AutomationTarget Id=\"{on_at}\">\n{T2}\t<LockEnvelope Value=\"0\" />\n{T2}</AutomationTarget>\n"
        f"{T2}<MidiCCOnOffThresholds><Min Value=\"64\" /><Max Value=\"127\" /></MidiCCOnOffThresholds>\n"
        f"{T1}</On>\n"
        f"{T1}<ModulationSourceCount Value=\"0\" />\n"
        f"{T1}<ParametersListWrapper LomId=\"0\" />\n"
        f"{T1}<Pointee Id=\"{pt_id}\" />\n"
        f"{T1}<LastSelectedTimeableIndex Value=\"0\" />\n"
        f"{T1}<LastSelectedClipEnvelopeIndex Value=\"0\" />\n"
        f"{T1}<LastPresetRef>\n"
        f"{T2}<Value>\n"
        f"{T3}<AbletonDefaultPresetRef Id=\"{psr_id}\">\n"
        f"{T4}<FileRef>\n"
        f"{T4}\t<RelativePathType Value=\"0\" />\n"
        f"{T4}\t<RelativePath Value=\"\" />\n"
        f"{T4}\t<Path Value=\"\" />\n"
        f"{T4}\t<Type Value=\"2\" />\n"
        f"{T4}\t<LivePackName Value=\"\" />\n"
        f"{T4}\t<LivePackId Value=\"\" />\n"
        f"{T4}\t<OriginalFileSize Value=\"0\" />\n"
        f"{T4}\t<OriginalCrc Value=\"0\" />\n"
        f"{T4}\t<SourceHint Value=\"\" />\n"
        f"{T4}</FileRef>\n"
        f"{T4}<DeviceId Name=\"Eq8\" />\n"
        f"{T3}</AbletonDefaultPresetRef>\n"
        f"{T2}</Value>\n"
        f"{T1}</LastPresetRef>\n"
        f"{T1}<LockedScripts />\n"
        f"{T1}<IsFolded Value=\"false\" />\n"
        f"{T1}<ShouldShowPresetName Value=\"true\" />\n"
        f"{T1}<UserName Value=\"\" />\n"
        f"{T1}<Annotation Value=\"\" />\n"
        f"{T1}<SourceContext><Value /></SourceContext>\n"
        f"{T1}<MpePitchBendUsesTuning Value=\"true\" />\n"
        f"{T1}<ViewData Value=\"{{}}\" />\n"
        f"{T1}<OverwriteProtectionNumber Value=\"3075\" />\n"
        f"{T1}<Precision Value=\"0\" />\n"
        f"{T1}<Mode Value=\"0\" />\n"
        f"{T1}<EditMode Value=\"false\" />\n"
        f"{T1}<SelectedBand Value=\"0\" />\n"
        f"{T1}<GlobalGain>\n"
        f"{T2}<LomId Value=\"0\" />\n"
        f"{T2}<Manual Value=\"0\" />\n"
        f"{T2}<MidiControllerRange><Min Value=\"-15\" /><Max Value=\"15\" /></MidiControllerRange>\n"
        f"{T2}<AutomationTarget Id=\"{gg_at}\">\n{T2}\t<LockEnvelope Value=\"0\" />\n{T2}</AutomationTarget>\n"
        f"{T2}<ModulationTarget Id=\"{gg_mt}\">\n{T2}\t<LockEnvelope Value=\"0\" />\n{T2}</ModulationTarget>\n"
        f"{T1}</GlobalGain>\n"
        f"{bands_xml}"
        f"{T1}<Scale>\n"
        f"{T2}<LomId Value=\"0\" />\n"
        f"{T2}<Manual Value=\"1\" />\n"
        f"{T2}<MidiControllerRange><Min Value=\"-2\" /><Max Value=\"2\" /></MidiControllerRange>\n"
        f"{T2}<AutomationTarget Id=\"{sc_at}\">\n{T2}\t<LockEnvelope Value=\"0\" />\n{T2}</AutomationTarget>\n"
        f"{T2}<ModulationTarget Id=\"{_uid()}\">\n{T2}\t<LockEnvelope Value=\"0\" />\n{T2}</ModulationTarget>\n"
        f"{T1}</Scale>\n"
        f"{T1}<SpectrumAnalyzer>\n"
        f"{T2}<LomId Value=\"0\" />\n"
        f"{T2}<LomIdView Value=\"0\" />\n"
        f"{T2}<IsExpanded Value=\"false\" />\n"
        f"{T2}<BreakoutIsExpanded Value=\"false\" />\n"
        f"{T2}<On>\n"
        f"{T3}<LomId Value=\"0\" />\n"
        f"{T3}<Manual Value=\"true\" />\n"
        f"{T3}<AutomationTarget Id=\"{_uid()}\">\n{T3}\t<LockEnvelope Value=\"0\" />\n{T3}</AutomationTarget>\n"
        f"{T3}<MidiCCOnOffThresholds><Min Value=\"64\" /><Max Value=\"127\" /></MidiCCOnOffThresholds>\n"
        f"{T2}</On>\n"
        f"{T2}<ModulationSourceCount Value=\"0\" />\n"
        f"{T2}<ParametersListWrapper LomId=\"0\" />\n"
        f"{T2}<Pointee Id=\"{_uid()}\" />\n"
        f"{T2}<LastSelectedTimeableIndex Value=\"0\" />\n"
        f"{T2}<LastSelectedClipEnvelopeIndex Value=\"0\" />\n"
        f"{T2}<LastPresetRef><Value /></LastPresetRef>\n"
        f"{T2}<LockedScripts />\n"
        f"{T2}<IsFolded Value=\"false\" />\n"
        f"{T2}<ShouldShowPresetName Value=\"true\" />\n"
        f"{T2}<UserName Value=\"\" />\n"
        f"{T2}<Annotation Value=\"\" />\n"
        f"{T2}<SourceContext><Value /></SourceContext>\n"
        f"{T2}<MpePitchBendUsesTuning Value=\"true\" />\n"
        f"{T2}<ViewData Value=\"{{}}\" />\n"
        f"{T2}<OverwriteProtectionNumber Value=\"3075\" />\n"
        f"{T2}<ScaleYBegin Value=\"0\" />\n"
        f"{T2}<ScaleYRange Value=\"80\" />\n"
        f"{T2}<AutoScaleY Value=\"false\" />\n"
        f"{T2}<ScaleXMode Value=\"1\" />\n"
        f"{T2}<ShowBins Value=\"false\" />\n"
        f"{T2}<ShowMax Value=\"true\" />\n"
        f"{T2}<AnalyzeOn Value=\"true\" />\n"
        f"{T2}<Length Value=\"2\" />\n"
        f"{T2}<Window Value=\"3\" />\n"
        f"{T2}<ChannelMode Value=\"2\" />\n"
        f"{T2}<NumAverages Value=\"1\" />\n"
        f"{T2}<MinRefreshTime Value=\"60\" />\n"
        f"{T1}</SpectrumAnalyzer>\n"
        f"{T1}<Live8ShelfScaleLegacyMode Value=\"false\" />\n"
        f"{T1}<AuditionOnOff Value=\"false\" />\n"
        f"{T1}<AdaptiveQFactor Value=\"1.12\" />\n"
        f"{T1}<AdaptiveQ>\n"
        f"{T2}<LomId Value=\"0\" />\n"
        f"{T2}<Manual Value=\"true\" />\n"
        f"{T2}<AutomationTarget Id=\"{_uid()}\">\n{T2}\t<LockEnvelope Value=\"0\" />\n{T2}</AutomationTarget>\n"
        f"{T2}<MidiCCOnOffThresholds><Min Value=\"64\" /><Max Value=\"127\" /></MidiCCOnOffThresholds>\n"
        f"{T1}</AdaptiveQ>\n"
        f"{T1}<AdaptiveQAffectsShelves Value=\"false\" />\n"
        f"{T}</Eq8>\n"
    )
    return xml, id_start

# ==============================================================================
# CHOPPER PAD HELPERS
# ==============================================================================

def _is_chopper_pad(pad):
    """Return True if pad is a Koala Chopper pad."""
    return pad.get('type') == 'synth' and pad.get('synth') == 'CHOPPER'


def _get_chopper_params(pad):
    """Extract chopper parameters from a chopper pad dict.

    Returns a dict with:
      slice_count, trigger_mode (0=Note,1=Velocity,2=Random),
      mono, one_shot, play_thru, vol, pan, pitch
    """
    sp  = pad.get('synthParams', {})
    pp  = sp.get('padParams', {})
    slices = pad.get('chops', {}).get('slices', [])
    eq = pp.get('eq', {})
    return {
        'slice_count':  max(1, len(slices)),
        'trigger_mode': float(sp.get('TRIGGER MODE', 0.0)),
        'slice_mode':   float(sp.get('SLICE MODE', 1.0)),  # 0=Auto, 1=Equal
        'slice_starts': [int(s.get('start', 0)) for s in slices],  # frames @48kHz
        'mono':         float(sp.get('MONO', 1.0)) == 1.0,
        'one_shot':     float(sp.get('ONE SHOT', 0.0)) == 1.0,
        'play_thru':    float(sp.get('PLAY THRU', 0.0)) == 1.0,
        'vol':          float(pp.get('vol', 1.0) or 1.0),
        'pan':          float(pp.get('pan', 0.5) or 0.5),
        'pitch':        float(pp.get('pitch', 0.0) or 0.0),
        'eq':           eq,
    }




def _manual_slice_points_xml(slice_starts_frames, tab_level=19):
    """Build ManualSlicePoints XML from Koala slice start positions (frames @48kHz).
    Each start frame is converted to seconds: TimeInSeconds = frame / 48000.
    """
    t = '\t' * tab_level
    lines = []
    for frame in slice_starts_frames:
        secs = frame / 48000.0
        lines.append(f'{t}<SlicePoint TimeInSeconds="{secs}" Rank="1" NormalizedEnergy="1" />')
    t_open  = '\t' * (tab_level - 1)
    return f'{t_open}<ManualSlicePoints>\n' + '\n'.join(lines) + f'\n{t_open}</ManualSlicePoints>\n'

def _midi_random_device_xml(slice_count, id_start, tab_level=7):
    """Build a MidiRandom device XML block for a chopper pad in Random mode.
    Choices is set to slice_count so the random range covers all slices.
    tab_level: indentation depth of the MidiRandom element (7 for standalone
      Simpler tracks, 13 for drum branch device chains).
    Returns (xml_string, new_id_start).
    """
    def _uid():
        nonlocal id_start
        v = id_start; id_start += 1; return v

    t7  = '\t' * tab_level
    t8  = '\t' * (tab_level + 1)
    t9  = '\t' * (tab_level + 2)
    t10 = '\t' * (tab_level + 3)
    t11 = '\t' * (tab_level + 4)

    dev_id    = _uid()
    on_at     = _uid()
    pointee   = _uid()
    preset_id = _uid()
    c_at = _uid(); c_mt = _uid()   # Chance
    ch_at= _uid(); ch_mt= _uid()   # Choices
    sc_at= _uid(); sc_mt= _uid()   # Scale
    si_at= _uid()                   # Sign
    al_at= _uid()                   # Alternate
    uc_at= _uid()                   # UseCurrentScale

    xml = (
        f'{t7}<MidiRandom Id="{dev_id}">\n'
        f'{t8}<LomId Value="0" />\n'
        f'{t8}<LomIdView Value="0" />\n'
        f'{t8}<IsExpanded Value="true" />\n'
        f'{t8}<BreakoutIsExpanded Value="false" />\n'
        f'{t8}<On>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="true" />\n'
        f'{t9}<AutomationTarget Id="{on_at}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<MidiCCOnOffThresholds>\n'
        f'{t10}<Min Value="64" />\n'
        f'{t10}<Max Value="127" />\n'
        f'{t9}</MidiCCOnOffThresholds>\n'
        f'{t8}</On>\n'
        f'{t8}<ModulationSourceCount Value="0" />\n'
        f'{t8}<ParametersListWrapper LomId="0" />\n'
        f'{t8}<Pointee Id="{pointee}" />\n'
        f'{t8}<LastSelectedTimeableIndex Value="2" />\n'
        f'{t8}<LastSelectedClipEnvelopeIndex Value="2" />\n'
        f'{t8}<LastPresetRef>\n'
        f'{t9}<Value>\n'
        f'{t10}<AbletonDefaultPresetRef Id="{preset_id}">\n'
        f'{t11}<FileRef>\n'
        f'{t11}<RelativePathType Value="7" />\n'
        f'{t11}<RelativePath Value="Devices/MIDI Effects/Random" />\n'
        f'{t11}<Path Value="/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Builtin/Devices/MIDI Effects/Random" />\n'
        f'{t11}<Type Value="2" />\n'
        f'{t11}<LivePackName Value="" />\n'
        f'{t11}<LivePackId Value="" />\n'
        f'{t11}<OriginalFileSize Value="0" />\n'
        f'{t11}<OriginalCrc Value="0" />\n'
        f'{t11}<SourceHint Value="" />\n'
        f'{t11}</FileRef>\n'
        f'{t11}<DeviceId Name="" />\n'
        f'{t10}</AbletonDefaultPresetRef>\n'
        f'{t9}</Value>\n'
        f'{t8}</LastPresetRef>\n'
        f'{t8}<LockedScripts />\n'
        f'{t8}<IsFolded Value="false" />\n'
        f'{t8}<ShouldShowPresetName Value="true" />\n'
        f'{t8}<UserName Value="" />\n'
        f'{t8}<Annotation Value="" />\n'
        f'{t8}<SourceContext>\n'
        f'{t9}<Value />\n'
        f'{t8}</SourceContext>\n'
        f'{t8}<MpePitchBendUsesTuning Value="true" />\n'
        f'{t8}<ViewData Value="{{}}" />\n'
        f'{t8}<OverwriteProtectionNumber Value="3075" />\n'
        f'{t8}<Chance>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="1" />\n'
        f'{t9}<MidiControllerRange>\n'
        f'{t10}<Min Value="0" />\n'
        f'{t10}<Max Value="1" />\n'
        f'{t9}</MidiControllerRange>\n'
        f'{t9}<AutomationTarget Id="{c_at}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<ModulationTarget Id="{c_mt}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</ModulationTarget>\n'
        f'{t8}</Chance>\n'
        f'{t8}<Choices>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="{slice_count}" />\n'
        f'{t9}<MidiControllerRange>\n'
        f'{t10}<Min Value="1" />\n'
        f'{t10}<Max Value="24" />\n'
        f'{t9}</MidiControllerRange>\n'
        f'{t9}<AutomationTarget Id="{ch_at}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<ModulationTarget Id="{ch_mt}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</ModulationTarget>\n'
        f'{t8}</Choices>\n'
        f'{t8}<Scale>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="1" />\n'
        f'{t9}<MidiControllerRange>\n'
        f'{t10}<Min Value="1" />\n'
        f'{t10}<Max Value="24" />\n'
        f'{t9}</MidiControllerRange>\n'
        f'{t9}<AutomationTarget Id="{sc_at}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<ModulationTarget Id="{sc_mt}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</ModulationTarget>\n'
        f'{t8}</Scale>\n'
        f'{t8}<Sign>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="0" />\n'
        f'{t9}<AutomationTarget Id="{si_at}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<MidiControllerRange>\n'
        f'{t10}<Min Value="0" />\n'
        f'{t10}<Max Value="2" />\n'
        f'{t9}</MidiControllerRange>\n'
        f'{t8}</Sign>\n'
        f'{t8}<Alternate>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="false" />\n'
        f'{t9}<AutomationTarget Id="{al_at}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<MidiCCOnOffThresholds>\n'
        f'{t10}<Min Value="64" />\n'
        f'{t10}<Max Value="127" />\n'
        f'{t9}</MidiCCOnOffThresholds>\n'
        f'{t8}</Alternate>\n'
        f'{t8}<UseCurrentScale>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="false" />\n'
        f'{t9}<AutomationTarget Id="{uc_at}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<MidiCCOnOffThresholds>\n'
        f'{t10}<Min Value="64" />\n'
        f'{t10}<Max Value="127" />\n'
        f'{t9}</MidiCCOnOffThresholds>\n'
        f'{t8}</UseCurrentScale>\n'
        f'{t7}</MidiRandom>\n'
    )
    return xml, id_start


def _make_simpler_device_chain(rel_path, pad_data, display_name, pad_label_str, id_start,
                               wav_abs_path=None, chopper_params=None):
    """
    Build a complete inner DeviceChain for a note-mode or chopper Simpler track.

    For normal pads applies: vol, pan, pitch, tune, speed, attack, release,
      tone, start, end, looping, oneshot, stretching, fadeIn, fadeOut, trim.

    chopper_params: dict from _get_chopper_params() - when set, switches the
      Simpler to Slice/Region mode and sets chopper-specific parameters.
      MidiRandom device is injected for Random trigger mode.

    wav_abs_path: absolute path to the extracted WAV on disk (used for trim).
    """
    tpl = _tpl(_SIMPLER_DC_TPL_B64)
    # Strip the extra outer MidiTrack </DeviceChain> captured in the template
    last_close = tpl.rfind('</DeviceChain>')
    tpl = tpl[:last_close].rstrip()

    # -- Chopper vs normal pad parameter extraction ------------------------
    _ALS_VOL_MIN = 0.0003162277571

    if chopper_params:
        # Chopper pads: playback params come from synthParams.padParams.
        # start/end/loop/oneshot/stretch/attack/release/fade/tone stay at
        # template defaults - Simpler slice mode handles playback.
        start_pt      = 0
        end_pt        = 0
        loop_on       = False
        is_warped     = "false"
        loop_mode     = "3"
        loop_on_val   = "false"
        playback_mode = "0"  # overridden below for slice Globals
        koala_vol   = chopper_params['vol']
        koala_pan   = chopper_params['pan']
        koala_pitch = chopper_params['pitch']
        als_vol     = max(_ALS_VOL_MIN, koala_vol ** 4)
        als_pan     = round(koala_pan * 2.0 - 1.0, 10)
        als_pitch   = int(round(koala_pitch))
        als_fine    = 0.0
        als_attack  = 0.1000000015
        als_release = 1.0
        als_fade_in = 0.0
        als_fade_out = 0.1000000015
        als_filter_type = None
    else:
        # -- Sample region / playback -------------------------------------
        koala_trim  = float(pad_data.get("trim", 0.0) or 0.0)
        trim_frames = 0
        if koala_trim > 0.0 and wav_abs_path and os.path.isfile(wav_abs_path):
            try:
                with wave.open(wav_abs_path, 'rb') as _wf:
                    total_frames = _wf.getnframes()
                trim_frames = int(round(koala_trim * total_frames))
            except Exception:
                trim_frames = 0

        start_pt = int(pad_data.get("start", 0) or 0) + trim_frames
        end_pt   = int(pad_data.get("end",   0) or 0)
        if end_pt > 0:
            start_pt = min(start_pt, end_pt - 1)
        loop_on   = str(pad_data.get("looping", "false")).lower() == "true"
        one_shot  = str(pad_data.get("oneshot", "false")).lower() == "true"
        is_warped = "true" if pad_data.get("stretching") is True else "false"
        loop_mode    = "0" if loop_on else "3"
        loop_on_val  = "true" if loop_on else "false"
        playback_mode = "1" if one_shot else "0"

        # -- Volume -------------------------------------------------------
        koala_vol = float(pad_data.get("vol", 1.0) or 1.0)
        als_vol   = max(_ALS_VOL_MIN, koala_vol ** 4)

        # -- Pan ----------------------------------------------------------
        koala_pan = float(pad_data.get("pan", 0.5) or 0.5)
        als_pan   = round(koala_pan * 2.0 - 1.0, 10)

        # -- Pitch + Speed + Tune -----------------------------------------
        koala_pitch = float(pad_data.get("pitch", 0.0) or 0.0)
        koala_speed = float(pad_data.get("speed", 1.0) or 1.0)
        if abs(koala_speed - 1.0) < 1e-6:
            speed_semitones_total = 0.0
        else:
            speed_semitones_total = 12.0 * math.log2(max(koala_speed, 1e-6))
        speed_semi_int = int(round(speed_semitones_total))
        speed_cents    = (speed_semitones_total - speed_semi_int) * 100.0
        als_pitch      = int(round(koala_pitch)) + speed_semi_int
        koala_tune     = float(pad_data.get("tune", 0.0) or 0.0)
        tune_cents     = koala_tune * 100.0
        als_fine       = max(-50.0, min(50.0, tune_cents + speed_cents))

        # -- Attack -------------------------------------------------------
        _ALS_ATK_MIN = 0.1000000015
        _ALS_ATK_MAX = 20000.0
        _KOA_ATK_MIN = 0.00011
        _KOA_ATK_MAX = 3.0
        koala_attack = float(pad_data.get("attack", _KOA_ATK_MIN) or _KOA_ATK_MIN)
        if koala_attack <= _KOA_ATK_MIN:
            als_attack = _ALS_ATK_MIN
        elif koala_attack >= _KOA_ATK_MAX:
            als_attack = _ALS_ATK_MAX
        else:
            _t = (math.log(koala_attack) - math.log(_KOA_ATK_MIN)) / \
                 (math.log(_KOA_ATK_MAX) - math.log(_KOA_ATK_MIN))
            als_attack = _ALS_ATK_MIN * (_ALS_ATK_MAX / _ALS_ATK_MIN) ** _t

        # -- Release ------------------------------------------------------
        _ALS_REL_MIN = 1.0
        _ALS_REL_MAX = 60000.0
        _KOA_REL_MAX = 3.0
        koala_release = float(pad_data.get("release", 0.0) or 0.0)
        if koala_release <= 0.0:
            als_release = _ALS_REL_MIN
        elif koala_release >= _KOA_REL_MAX:
            als_release = _ALS_REL_MAX
        else:
            _t = koala_release / _KOA_REL_MAX
            als_release = _ALS_REL_MIN * (_ALS_REL_MAX / _ALS_REL_MIN) ** _t

        # -- FadeIn / FadeOut ---------------------------------------------
        _ALS_FADE_MAX = 2000.0
        koala_fade_in  = float(pad_data.get("fadeIn",  0.0) or 0.0)
        koala_fade_out = float(pad_data.get("fadeOut", 0.0) or 0.0)
        als_fade_in    = koala_fade_in  * _ALS_FADE_MAX
        als_fade_out   = koala_fade_out * _ALS_FADE_MAX if koala_fade_out > 0.0 else 0.1000000015

        # -- Tone -> Filter -------------------------------------------------
        koala_tone = float(pad_data.get("tone", 0.0) or 0.0)
        if abs(koala_tone) < 1e-6:
            als_filter_type = None
        elif koala_tone < 0:
            als_filter_type = "0"   # Low Pass
        else:
            als_filter_type = "1"   # High Pass

    # -- Patch template ----------------------------------------------------
    tpl = re.sub(r'<UserName Value="[^"]*"',
                 f'<UserName Value="{pad_label_str}"', tpl, count=1)
    tpl = re.sub(r'<Name Value="[^"]*"',
                 f'<Name Value="{display_name}"', tpl, count=1)
    tpl = re.sub(r'<RelativePath Value="[^"]*\.wav[^"]*"',
                 f'<RelativePath Value="{rel_path}"', tpl, count=1)
    tpl = re.sub(r'<SampleStart Value="[^"]*"',
                 f'<SampleStart Value="{start_pt}"', tpl, count=1)
    tpl = re.sub(r'<SampleEnd Value="[^"]*"',
                 f'<SampleEnd Value="{end_pt}"', tpl, count=1)
    tpl = re.sub(r'(<LoopOn>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{loop_on_val}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)
    tpl = re.sub(r'<IsWarped Value="[^"]*"',
                 f'<IsWarped Value="{is_warped}"', tpl, count=1)
    # -- Globals PlaybackMode: chopper=2 (Slice), normal=0/1 (Classic/OneShot)
    globals_pm = "2" if chopper_params else playback_mode
    tpl = re.sub(r'<PlaybackMode Value="[^"]*"',
                 f'<PlaybackMode Value="{globals_pm}"', tpl, count=1)
    # -- ReleaseLoop Mode
    tpl = re.sub(r'(<ReleaseLoop>.*?<Mode Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{loop_mode}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)
    # -- Chopper-specific: SlicingStyle, SlicingRegions, NumVoices, SimplerSlicing
    if chopper_params:
        N = chopper_params['slice_count']
        is_auto = chopper_params.get('slice_mode', 1.0) == 0.0
        # SlicingStyle: 3=Manual (Auto chop), 2=Region/Equal
        slicing_style = "3" if is_auto else "2"
        tpl = re.sub(r'<SlicingStyle Value="[^"]*"',
                     f'<SlicingStyle Value="{slicing_style}"', tpl, count=1)
        tpl = re.sub(r'<SlicingRegions Value="[^"]*"',
                     f'<SlicingRegions Value="{N}"', tpl, count=1)
        num_voices = "1" if chopper_params['mono'] else "5"
        tpl = re.sub(r'<NumVoices Value="[^"]*"',
                     f'<NumVoices Value="{num_voices}"', tpl, count=1)
        # SimplerSlicing PlaybackMode: 0=Gate, 2=Thru
        # ONE SHOT does not change slice playback mode (it's handled by NumVoices=1)
        if chopper_params['play_thru']:
            slicing_pm = "2"
        else:
            slicing_pm = "0"
        tpl = re.sub(r'(<SimplerSlicing>\s*)<PlaybackMode Value="[^"]*"',
                     lambda m: m.group(1) + f'<PlaybackMode Value="{slicing_pm}"',
                     tpl, count=1, flags=re.DOTALL)
        # Auto chop: inject ManualSlicePoints
        if is_auto and chopper_params.get('slice_starts'):
            msp_xml = _manual_slice_points_xml(chopper_params['slice_starts'], tab_level=19)
            tpl = tpl.replace('<ManualSlicePoints />\n', msp_xml, 1)
            if '<ManualSlicePoints />\n' not in tpl:
                tpl = re.sub(r'<ManualSlicePoints>\s*</ManualSlicePoints>',
                             msp_xml.rstrip(), tpl, count=1)

    # Volume
    tpl = re.sub(r'(<Volume Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_vol:.10g}{m.group(2)}', tpl, count=1)

    # Pan
    tpl = re.sub(r'(<VolumeAndPan>.*?<Panorama>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_pan:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Pitch
    tpl = re.sub(r'(<Pitch>.*?<TransposeKey>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_pitch}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Tune / Speed fine
    tpl = re.sub(r'(<Pitch>.*?<TransposeFine>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_fine:.6g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Attack
    tpl = re.sub(r'(<AttackTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_attack:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Release
    tpl = re.sub(r'(<ReleaseTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_release:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # FadeIn
    tpl = re.sub(r'(<FadeInTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_fade_in:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # FadeOut
    tpl = re.sub(r'(<FadeOutTime>.*?<Manual Value=")[^"]*(")',
                 lambda m: f'{m.group(1)}{als_fade_out:.10g}{m.group(2)}',
                 tpl, count=1, flags=re.DOTALL)

    # Filter IsOn
    als_filter_on = "false" if als_filter_type is None else "true"
    tpl = re.sub(
        r'(<Filter>.*?<IsOn>.*?<Manual Value=")[^"]*(")',
        lambda m: f'{m.group(1)}{als_filter_on}{m.group(2)}',
        tpl, count=1, flags=re.DOTALL)
    # Patch SimplerFilter Type in template: 0=LP, 1=HP
    if als_filter_type is not None:
        tpl = re.sub(
            r'(<Filter>.*?<SimplerFilter\b.*?<Type>.*?<Manual Value=")[^"]*(")',
            lambda m: f'{m.group(1)}{als_filter_type}{m.group(2)}',
            tpl, count=1, flags=re.DOTALL)
        tpl = re.sub(
            r'(<Filter>.*?<SimplerFilter\b.*?<Freq>.*?<Manual Value=")[^"]*(")',
            lambda m: f'{m.group(1)}{min(1000.0, max(30.0, (abs(koala_tone) / (0.99 if koala_tone < 0 else 0.30)) * 1000.0)):.6g}{m.group(2)}',
            tpl, count=1, flags=re.DOTALL)

    # Clear WarpMarkers to a clean single-origin marker.
    # The template has hardcoded markers from the reference sample that would
    # incorrectly warp every pad to those timecodes.
    tpl = re.sub(
        r'<WarpMarkers>.*?</WarpMarkers>',
        '<WarpMarkers>\n'
        '\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t'
        '<WarpMarker Id="0" SecTime="0" BeatTime="0" />\n'
        '\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t</WarpMarkers>',
        tpl, count=1, flags=re.DOTALL
    )
    remapped, id_start = _als_remap_ids(tpl, id_start)


    # -- Inject MidiRandom for Random trigger mode ------------------------
    if chopper_params and chopper_params['trigger_mode'] == 2.0:
        mr_xml, id_start = _midi_random_device_xml(chopper_params['slice_count'], id_start)
        # Insert MidiRandom before OriginalSimpler inside <Devices>
        remapped = remapped.replace('<Devices>\n', '<Devices>\n' + mr_xml, 1)

    # -- Inject EQ Eight after Simpler if pad EQ is enabled ---------------
    eq_data = (chopper_params.get('eq', {}) if chopper_params
               else pad_data.get('eq', {}))
    if eq_data and str(eq_data.get('enabled', 'false')).lower() == 'true':
        eq8_xml, id_start = _eq8_device_xml(eq_data, id_start, tab_level=7)
        # Append after </OriginalSimpler> before </Devices>
        remapped = re.sub(r'(</OriginalSimpler>)(\s*</Devices>)',
                          lambda m: m.group(1) + '\n' + eq8_xml + m.group(2),
                          remapped, count=1)

    return remapped, id_start


# ==============================================================================
# BUS ROUTING HELPERS
# ==============================================================================

# Koala bus index -> (ReturnBranch letter, ReturnTrack name, ReturnTrack offset)
# The offset is the position (0-3) within the 4 added ReturnTracks.
# Bus 2 and 3 are swapped in Ableton's internal chain ordering - this matches
# the working reference file exactly.
#   bus 0 -> a Return Chain -> A-Bus (ReturnTrack offset 0)
#   bus 1 -> b Return Chain -> B-Bus (ReturnTrack offset 1)
#   bus 2 -> c Return Chain -> D-Bus (ReturnTrack offset 3)  <- swap
#   bus 3 -> d Return Chain -> C-Bus (ReturnTrack offset 2)  <- swap
_BUS_RETURN_CHAIN_LETTERS = ['a', 'b', 'c', 'd']
_BUS_RETURN_TRACK_NAMES   = ['A-Bus', 'B-Bus', 'C-Bus', 'D-Bus']
# Maps Koala bus index 0-3 to ReturnTrack offset 0-3 (clean 1:1)
_BUS_TO_RT_OFFSET = [0, 1, 2, 3]


def _bus_return_track_xml(track_id, bus_index, id_start, n_scenes, sc_params=None, sc_source_rt_id=None, sc_source_name=None, sc_bypass=False, bus_volume_db=0.0, bus_muted=False):
    """
    Generate a ReturnTrack XML block for one bus channel, using the blank ALS
    ReturnTrack as a template (same approach as MidiTracks).
    Strips any devices, renames the track, and sets 4 Sends holders.
    Returns (xml_string, new_id_start).
    """
    import base64 as _b64, io as _io
    blank_xml = _als_load_blank()
    # Extract the first ReturnTrack from the blank as template
    rt_blocks = _als_extract_blocks(blank_xml, "ReturnTrack")
    tpl = "\t\t\t" + rt_blocks[0].strip() + "\n"

    name = _BUS_RETURN_TRACK_NAMES[bus_index]

    # 1. Clear the device chain (strip Reverb/Delay etc, leave empty Devices)
    tpl = re.sub(r'<Devices>.*?</Devices>', '<Devices />', tpl,
                 count=1, flags=re.DOTALL)

    # 2. Set the track name
    tpl = re.sub(r'<EffectiveName Value="[^"]*"',
                 f'<EffectiveName Value="{name}"', tpl, count=1)
    # UserName - keep as "Bus" to match reference
    tpl = re.sub(r'<UserName Value="[^"]*"',
                 f'<UserName Value="Bus"', tpl, count=1)

    # 3. Set track colour to 11 (matches reference)
    tpl = re.sub(r'(<ReturnTrack[^>]*>.*?)<Color Value="[^"]*"',
                 lambda m: m.group(1) + '<Color Value="11"',
                 tpl, count=1, flags=re.DOTALL)

    # 4. Expand ClipSlots if needed (same as MidiTracks)
    tpl = _als_expand_clipslots(tpl, n_scenes)

    # 5. Remap all IDs to fresh unique values
    remapped, id_start = _als_remap_ids(tpl, id_start)

    # 6. Replace the 2 blank Sends holders with 4 (IDs 0-3)
    def _make_4_holders():
        nonlocal id_start
        xml = ""
        for i in range(4):
            h_id  = id_start; id_start += 1
            h_mid = id_start; id_start += 1
            xml += (
                f'\t\t\t\t\t\t\t<TrackSendHolder Id="{i}">\n'
                f'\t\t\t\t\t\t\t\t<Send>\n'
                f'\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
                f'\t\t\t\t\t\t\t\t\t<Manual Value="0.0003162277571" />\n'
                f'\t\t\t\t\t\t\t\t\t<MidiControllerRange>\n'
                f'\t\t\t\t\t\t\t\t\t\t<Min Value="0.0003162277571" />\n'
                f'\t\t\t\t\t\t\t\t\t\t<Max Value="1" />\n'
                f'\t\t\t\t\t\t\t\t\t</MidiControllerRange>\n'
                f'\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{h_id}">\n'
                f'\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
                f'\t\t\t\t\t\t\t\t\t</AutomationTarget>\n'
                f'\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{h_mid}">\n'
                f'\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
                f'\t\t\t\t\t\t\t\t\t</ModulationTarget>\n'
                f'\t\t\t\t\t\t\t\t</Send>\n'
                f'\t\t\t\t\t\t\t\t<EnabledByUser Value="true" />\n'
                f'\t\t\t\t\t\t\t</TrackSendHolder>\n'
            )
        return xml

    four_holders = _make_4_holders()
    remapped = re.sub(
        r'<Sends>\s*<TrackSendHolder.*?</Sends>',
        '<Sends>\n' + four_holders + '\t\t\t\t\t\t</Sends>',
        remapped, count=1, flags=re.DOTALL)

    # Apply volume (dB -> linear, clamped to ALS range)
    import math as _math
    _ALS_VOL_MIN = 0.0003162277571   # -70 dB
    _ALS_VOL_MAX = 1.99526238        # +6 dB
    als_volume = max(_ALS_VOL_MIN, min(_ALS_VOL_MAX, 10.0 ** (bus_volume_db / 20.0)))
    remapped = re.sub(
        r'(<Volume>\s*<LomId[^/]*/>[^<]*<Manual Value=")[^"]*(")',
        lambda m: f'{m.group(1)}{als_volume:.10g}{m.group(2)}',
        remapped, count=1, flags=re.DOTALL)

    # Apply mute: Koala mute=true -> ALS Speaker Manual=false
    if bus_muted:
        remapped = re.sub(
            r'(<Speaker>\s*<LomId[^/]*/>[^<]*<Manual Value=")[^"]*(")',
            lambda m: f'{m.group(1)}false{m.group(2)}',
            remapped, count=1, flags=re.DOTALL)

    # Inject sidechain compressor into Devices block if provided
    if sc_params is not None and sc_source_rt_id is not None:
        comp_xml, id_start = _bus_sidechain_compressor_xml(
            sc_params, sc_source_rt_id, sc_source_name or "Source",
            id_start, bypass=sc_bypass)
        remapped = remapped.replace(
            '<Devices />\n',
            '<Devices>\n' + comp_xml + '\t\t\t\t\t\t</Devices>\n',
            1)

    return remapped, id_start



def _bus_sidechain_compressor_xml(sc_params, source_rt_id, source_bus_name, id_counter,
                                   bypass=False, routing_type='PreFxOut'):
    """
    Build a Compressor2 XML block with sidechain configured.

    sc_params:       dict with keys threshold (dB), release (ms), output (dB)
    source_rt_id:    ALS ReturnTrack ID of the sidechain trigger bus
    source_bus_name: display name of trigger bus e.g. "A-Bus"
    id_counter:      running ID counter (int); returns (xml, new_id_counter)
    bypass:          if True, compressor is disabled

    Parameter mapping:
        threshold: koala dB [-60, 0] -> ALS linear: 10^((koala * 1.1 + 6) / 20)
        release:   koala ms [10, 1000] -> ALS ms: direct
        output:    koala dB [-12, +12] -> ALS Gain dB: direct
    """
    import math as _math

    ctr = [id_counter]
    def _uid():
        v = ctr[0]; ctr[0] += 1; return v

    koala_thresh = float(sc_params.get('threshold', -20.0))
    koala_release = float(sc_params.get('release', 100.0))
    koala_output  = float(sc_params.get('output',  0.0))

    als_threshold = 10.0 ** (koala_thresh / 20.0)
    # Clamp to ALS range
    als_threshold = max(0.0003162277571, min(1.99526238, als_threshold))
    als_release   = max(1.0, min(3000.0, koala_release))
    als_gain      = max(-36.0, min(36.0, koala_output))
    on_val        = "false" if bypass else "true"

    t6 = "\t" * 6
    t7 = "\t" * 7
    t8 = "\t" * 8
    t9 = "\t" * 9
    t10= "\t" * 10
    t11= "\t" * 11

    def _param(name, val, mn, mx, has_mod=True):
        """Generate an AutomationTarget (+optional ModulationTarget) parameter block."""
        at_id  = _uid()
        mt_id  = _uid() if has_mod else None
        xml  = f'{t7}<{name}>\n'
        xml += f'{t8}<LomId Value="0" />\n'
        xml += f'{t8}<Manual Value="{val}" />\n'
        xml += f'{t8}<MidiControllerRange>\n'
        xml += f'{t9}<Min Value="{mn}" />\n'
        xml += f'{t9}<Max Value="{mx}" />\n'
        xml += f'{t8}</MidiControllerRange>\n'
        xml += f'{t8}<AutomationTarget Id="{at_id}">\n'
        xml += f'{t9}<LockEnvelope Value="0" />\n'
        xml += f'{t8}</AutomationTarget>\n'
        if has_mod:
            xml += f'{t8}<ModulationTarget Id="{mt_id}">\n'
            xml += f'{t9}<LockEnvelope Value="0" />\n'
            xml += f'{t8}</ModulationTarget>\n'
        xml += f'{t7}</{name}>\n'
        return xml

    def _bool_param(name, val):
        at_id = _uid()
        xml  = f'{t7}<{name}>\n'
        xml += f'{t8}<LomId Value="0" />\n'
        xml += f'{t8}<Manual Value="{val}" />\n'
        xml += f'{t8}<AutomationTarget Id="{at_id}">\n'
        xml += f'{t9}<LockEnvelope Value="0" />\n'
        xml += f'{t8}</AutomationTarget>\n'
        xml += f'{t8}<MidiCCOnOffThresholds>\n'
        xml += f'{t9}<Min Value="64" />\n'
        xml += f'{t9}<Max Value="127" />\n'
        xml += f'{t8}</MidiCCOnOffThresholds>\n'
        xml += f'{t7}</{name}>\n'
        return xml

    on_id = _uid(); pt_id = _uid()
    sc_on_id = _uid()
    sc_vol_id = _uid(); sc_vol_mid = _uid()
    sc_dw_id = _uid();  sc_dw_mid  = _uid()

    xml = (
        f'{t6}<Compressor2 Id="0">\n'
        f'{t7}<LomId Value="0" />\n'
        f'{t7}<LomIdView Value="0" />\n'
        f'{t7}<IsExpanded Value="true" />\n'
        f'{t7}<BreakoutIsExpanded Value="false" />\n'
        f'{t7}<On>\n'
        f'{t8}<LomId Value="0" />\n'
        f'{t8}<Manual Value="{on_val}" />\n'
        f'{t8}<AutomationTarget Id="{on_id}">\n'
        f'{t9}<LockEnvelope Value="0" />\n'
        f'{t8}</AutomationTarget>\n'
        f'{t8}<MidiCCOnOffThresholds>\n'
        f'{t9}<Min Value="64" />\n'
        f'{t9}<Max Value="127" />\n'
        f'{t8}</MidiCCOnOffThresholds>\n'
        f'{t7}</On>\n'
        f'{t7}<ModulationSourceCount Value="0" />\n'
        f'{t7}<ParametersListWrapper LomId="0" />\n'
        f'{t7}<Pointee Id="{pt_id}" />\n'
        f'{t7}<LastSelectedTimeableIndex Value="1" />\n'
        f'{t7}<LastSelectedClipEnvelopeIndex Value="0" />\n'
        f'{t7}<LastPresetRef>\n'
        f'{t8}<Value>\n'
        f'{t9}<AbletonDefaultPresetRef Id="0">\n'
        f'{t10}<FileRef>\n'
        f'{t11}<RelativePathType Value="0" />\n'
        f'{t11}<RelativePath Value="" />\n'
        f'{t11}<Path Value="" />\n'
        f'{t11}<Type Value="2" />\n'
        f'{t11}<LivePackName Value="" />\n'
        f'{t11}<LivePackId Value="" />\n'
        f'{t11}<OriginalFileSize Value="0" />\n'
        f'{t11}<OriginalCrc Value="0" />\n'
        f'{t11}<SourceHint Value="" />\n'
        f'{t10}</FileRef>\n'
        f'{t10}<DeviceId Name="Compressor2" />\n'
        f'{t9}</AbletonDefaultPresetRef>\n'
        f'{t8}</Value>\n'
        f'{t7}</LastPresetRef>\n'
        f'{t7}<LockedScripts />\n'
        f'{t7}<IsFolded Value="false" />\n'
        f'{t7}<ShouldShowPresetName Value="true" />\n'
        f'{t7}<UserName Value="" />\n'
        f'{t7}<Annotation Value="" />\n'
        f'{t7}<SourceContext>\n'
        f'{t8}<Value>\n'
        f'{t9}<BranchSourceContext Id="0">\n'
        f'{t10}<OriginalFileRef />\n'
        f'{t10}<BrowserContentPath Value="view:X-AudioFx#Compressor" />\n'
        f'{t10}<LocalFiltersJson Value="" />\n'
        f'{t10}<PresetRef>\n'
        f'{t11}<AbletonDefaultPresetRef Id="0">\n'
        f'{t11}\t<FileRef>\n'
        f'{t11}\t\t<RelativePathType Value="0" />\n'
        f'{t11}\t\t<RelativePath Value="" />\n'
        f'{t11}\t\t<Path Value="" />\n'
        f'{t11}\t\t<Type Value="2" />\n'
        f'{t11}\t\t<LivePackName Value="" />\n'
        f'{t11}\t\t<LivePackId Value="" />\n'
        f'{t11}\t\t<OriginalFileSize Value="0" />\n'
        f'{t11}\t\t<OriginalCrc Value="0" />\n'
        f'{t11}\t\t<SourceHint Value="" />\n'
        f'{t11}\t</FileRef>\n'
        f'{t11}\t<DeviceId Name="Compressor2" />\n'
        f'{t11}</AbletonDefaultPresetRef>\n'
        f'{t10}</PresetRef>\n'
        f'{t10}<BranchDeviceId Value="device:ableton:audiofx:Compressor2" />\n'
        f'{t9}</BranchSourceContext>\n'
        f'{t8}</Value>\n'
        f'{t7}</SourceContext>\n'
        f'{t7}<MpePitchBendUsesTuning Value="true" />\n'
        f'{t7}<ViewData Value="{{}}" />\n'
        f'{t7}<OverwriteProtectionNumber Value="3075" />\n'
    )

    xml += _param('Threshold', f'{als_threshold:.10g}', '0.0003162277571', '1.99526238')
    xml += _param('Ratio', '4', '1', '340282326356119256160033759537265639424')
    xml += _param('ExpansionRatio', '1.14999998', '1', '2')
    xml += _param('Attack', '1', '0.009999999776', '1000')
    xml += _param('Release', f'{als_release:.10g}', '1', '3000')
    xml += _bool_param('AutoReleaseControlOnOff', 'false')
    xml += _param('Gain', f'{als_gain:.10g}', '-36', '36')
    xml += _bool_param('GainCompensation', 'false')
    xml += _param('DryWet', '1', '0', '1')
    xml += _param('Model', '1', '0', '2', has_mod=False)
    xml += _param('LegacyModel', '1', '0', '2', has_mod=False)
    xml += _bool_param('LogEnvelope', 'true')
    xml += _param('LegacyEnvFollowerMode', '0', '0', '2', has_mod=False)
    xml += _param('Knee', '6', '0', '18')
    xml += _param('LookAhead', '0', '0', '2', has_mod=False)
    xml += _bool_param('SideListen', 'false')

    # SideChain block
    xml += (
        f'{t7}<SideChain>\n'
        f'{t8}<OnOff>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="true" />\n'
        f'{t9}<AutomationTarget Id="{sc_on_id}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<MidiCCOnOffThresholds>\n'
        f'{t10}<Min Value="64" />\n'
        f'{t10}<Max Value="127" />\n'
        f'{t9}</MidiCCOnOffThresholds>\n'
        f'{t8}</OnOff>\n'
        f'{t8}<RoutedInput>\n'
        f'{t9}<Routable>\n'
        f'{t10}<Target Value="AudioIn/Track.{source_rt_id}/{routing_type}" />\n'
        f'{t10}<UpperDisplayString Value="{source_bus_name}" />\n'
        f'{t10}<LowerDisplayString Value="{"Pre FX" if routing_type == "PreFxOut" else "Post FX"}" />\n'
        f'{t10}<MpeSettings>\n'
        f'{t11}<ZoneType Value="0" />\n'
        f'{t11}<FirstNoteChannel Value="1" />\n'
        f'{t11}<LastNoteChannel Value="15" />\n'
        f'{t10}</MpeSettings>\n'
        f'{t10}<MpePitchBendUsesTuning Value="true" />\n'
        f'{t9}</Routable>\n'
        f'{t9}<Volume>\n'
        f'{t10}<LomId Value="0" />\n'
        f'{t10}<Manual Value="1" />\n'
        f'{t10}<MidiControllerRange>\n'
        f'{t11}<Min Value="0.0003162277571" />\n'
        f'{t11}<Max Value="15.8489332" />\n'
        f'{t10}</MidiControllerRange>\n'
        f'{t10}<AutomationTarget Id="{sc_vol_id}">\n'
        f'{t11}<LockEnvelope Value="0" />\n'
        f'{t10}</AutomationTarget>\n'
        f'{t10}<ModulationTarget Id="{sc_vol_mid}">\n'
        f'{t11}<LockEnvelope Value="0" />\n'
        f'{t10}</ModulationTarget>\n'
        f'{t9}</Volume>\n'
        f'{t8}</RoutedInput>\n'
        f'{t8}<DryWet>\n'
        f'{t9}<LomId Value="0" />\n'
        f'{t9}<Manual Value="1" />\n'
        f'{t9}<MidiControllerRange>\n'
        f'{t10}<Min Value="0" />\n'
        f'{t10}<Max Value="1" />\n'
        f'{t9}</MidiControllerRange>\n'
        f'{t9}<AutomationTarget Id="{sc_dw_id}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</AutomationTarget>\n'
        f'{t9}<ModulationTarget Id="{sc_dw_mid}">\n'
        f'{t10}<LockEnvelope Value="0" />\n'
        f'{t9}</ModulationTarget>\n'
        f'{t8}</DryWet>\n'
        f'{t7}</SideChain>\n'
    )

    xml += _bool_param('SideChainEq_On', 'true')
    xml += _param('SideChainEq_Mode', '5', '0', '5', has_mod=False)
    xml += _param('SideChainEq_Freq', '80', '30', '15000')
    xml += _param('SideChainEq_Q', '0.7071067691', '0.1000000015', '12')
    xml += _param('SideChainEq_Gain', '0', '-15', '15')

    xml += (
        f'{t7}<Live8LegacyMode Value="false" />\n'
        f'{t7}<ViewMode Value="2" />\n'
        f'{t7}<IsOutputCurveVisible Value="false" />\n'
        f'{t7}<RmsTimeShort Value="8" />\n'
        f'{t7}<RmsTimeLong Value="250" />\n'
        f'{t7}<ReleaseTimeShort Value="15" />\n'
        f'{t7}<ReleaseTimeLong Value="1500" />\n'
        f'{t7}<CrossfaderSmoothingTime Value="10" />\n'
        f'{t6}</Compressor2>\n'
    )

    return xml, ctr[0]

def _bus_return_branches_xml(group_name, midi_track_id, drum_device_id, rt_ids, id_counter):
    """
    Build the ReturnBranches XML block for one drum rack.
    group_name:     e.g. 'Group A'
    midi_track_id:  the ALS MidiTrack ID for this group (for routing strings)
    drum_device_id: the DrumGroupDevice element ID (extracted from remapped rack XML)
    rt_ids:         list of 4 ReturnTrack IDs [A-Bus, B-Bus, C-Bus, D-Bus]
    Returns (xml_string, new_id_counter)
    """
    # ReturnBranch ordering: a->rt_ids[0], b->rt_ids[1], c->rt_ids[2], d->rt_ids[3]
    # Clean 1:1 mapping - a=A-Bus, b=B-Bus, c=C-Bus, d=D-Bus
    rt_order = [rt_ids[0], rt_ids[1], rt_ids[2], rt_ids[3]]
    rt_names_order = [_BUS_RETURN_TRACK_NAMES[0], _BUS_RETURN_TRACK_NAMES[1],
                      _BUS_RETURN_TRACK_NAMES[2], _BUS_RETURN_TRACK_NAMES[3]]

    branches_xml = ''
    for i in range(4):
        letter    = _BUS_RETURN_CHAIN_LETTERS[i]
        rt_id     = rt_order[i]
        rt_name   = rt_names_order[i]
        rb_id     = i + 1
        target_enum = i + 1

        def _uid():
            nonlocal id_counter
            v = id_counter; id_counter += 1; return v

        on_id  = _uid()
        pt_id  = _uid()
        spk_id = _uid()
        vol_id = _uid()
        vol_mid= _uid()
        pan_id = _uid()
        pan_mid= _uid()
        # SendInfos for the return branch itself (one send back to the existing
        # blank-template return tracks - kept at minimum, same as reference)
        si_ids = [(_uid(), _uid()) for _ in range(4)]

        si_xml = ''
        for si_idx, (s_id, s_mid) in enumerate(si_ids):
            si_xml += (
                f'\t\t\t\t\t\t\t\t\t\t\t\t<AudioBranchSendInfo Id="{si_idx}">\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Send>\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Manual Value="0.0003162277571" />\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0.0003162277571" />\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="1" />\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{s_id}">\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{s_mid}">\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t</ModulationTarget>\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t</Send>\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t\t<EnabledByUser Value="true" />\n'
                f'\t\t\t\t\t\t\t\t\t\t\t\t</AudioBranchSendInfo>\n'
            )

        branches_xml += (
            f'\t\t\t\t\t\t\t\t\t<ReturnBranch Id="{rb_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<n>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<EffectiveName Value="{letter} Return Chain" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<UserName Value="" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Annotation Value="" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<MemorizedFirstClipName Value="" />\n'
            f'\t\t\t\t\t\t\t\t\t\t</n>\n'
            f'\t\t\t\t\t\t\t\t\t\t<IsSelected Value="false" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<DeviceChain>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<AudioToAudioDeviceChain Id="0">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Devices />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<SignalModulations />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</AudioToAudioDeviceChain>\n'
            f'\t\t\t\t\t\t\t\t\t\t</DeviceChain>\n'
            f'\t\t\t\t\t\t\t\t\t\t<BranchSelectorRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Min Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Max Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMin Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<CrossfadeMax Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t</BranchSelectorRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t<IsSoloed Value="false" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<SessionViewBranchWidth Value="55" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<IsHighlightedInSessionView Value="false" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<SourceContext>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Value />\n'
            f'\t\t\t\t\t\t\t\t\t\t</SourceContext>\n'
            f'\t\t\t\t\t\t\t\t\t\t<Color Value="15" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<AutoColored Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<AutoColorScheme Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<SoloActivatedInSessionMixer Value="false" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<DevicesListWrapper LomId="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t<MixerDevice>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<LomIdView Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<IsExpanded Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<BreakoutIsExpanded Value="false" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<On>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Manual Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{on_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<MidiCCOnOffThresholds>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="64" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="127" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</MidiCCOnOffThresholds>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</On>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<ModulationSourceCount Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<ParametersListWrapper LomId="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Pointee Id="{pt_id}" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<LastSelectedTimeableIndex Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<LastSelectedClipEnvelopeIndex Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<LastPresetRef>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Value />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</LastPresetRef>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<LockedScripts />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<IsFolded Value="false" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<ShouldShowPresetName Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<UserName Value="" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Annotation Value="" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<SourceContext>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Value />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</SourceContext>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<MpePitchBendUsesTuning Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<ViewData Value="{{}}" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<OverwriteProtectionNumber Value="3075" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Speaker>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Manual Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{spk_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<MidiCCOnOffThresholds>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="64" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="127" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</MidiCCOnOffThresholds>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</Speaker>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Volume>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Manual Value="1" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0.0003162277571" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="1.99526238" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{vol_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{vol_mid}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</ModulationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</Volume>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<Panorama>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Manual Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="-1" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="1" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{pan_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{pan_mid}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</ModulationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</Panorama>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<SendInfos>\n'
            f'{si_xml}'
            f'\t\t\t\t\t\t\t\t\t\t\t</SendInfos>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<RoutingHelper>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<Routable>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Target Value="AudioOut/Track.{rt_id}/TrackIn" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<UpperDisplayString Value="{rt_name[:1]}-Return" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<LowerDisplayString Value="Track In" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<MpeSettings>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<ZoneType Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<FirstNoteChannel Value="1" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LastNoteChannel Value="15" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t</MpeSettings>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<MpePitchBendUsesTuning Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</Routable>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t<TargetEnum Value="{target_enum}" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t</RoutingHelper>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t<SendsListWrapper LomId="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t</MixerDevice>\n'
            f'\t\t\t\t\t\t\t\t\t</ReturnBranch>\n'
        )

    full_xml = f'\t\t\t\t\t\t\t\t<ReturnBranches>\n{branches_xml}\t\t\t\t\t\t\t\t</ReturnBranches>\n'
    return full_xml, id_counter


def _bus_audio_branch_send_infos(koala_bus, id_counter):
    """
    Build the AudioBranchSendInfo block (4 entries) for one DrumBranch pad.
    koala_bus:  -1 = no bus (all silent), 0-3 = active bus index
    The active bus send gets Manual Value="1", others stay at minimum.

    Koala bus -> AudioBranchSendInfo Index (active one):
        bus 0 -> Index 0  (a Return Chain)
        bus 1 -> Index 1  (b Return Chain)
        bus 2 -> Index 2  (c Return Chain)
        bus 3 -> Index 3  (d Return Chain)
    Returns (xml_string, new_id_counter)
    """
    xml = '\t\t\t\t\t\t\t\t\t\t\t<SendInfos>\n'
    for idx in range(4):
        active = (koala_bus >= 0 and idx == koala_bus)
        val    = '1' if active else '0.0003162277571'

        def _uid():
            nonlocal id_counter
            v = id_counter; id_counter += 1; return v

        s_id  = _uid()
        s_mid = _uid()
        ab_id = _uid()
        xml += (
            f'\t\t\t\t\t\t\t\t\t\t\t\t<AudioBranchSendInfo Id="{ab_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Send>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LomId Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Manual Value="{val}" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<MidiControllerRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Min Value="0.0003162277571" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<Max Value="1" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t</MidiControllerRange>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<AutomationTarget Id="{s_id}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t</AutomationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t<ModulationTarget Id="{s_mid}">\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t<LockEnvelope Value="0" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t\t</ModulationTarget>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t</Send>\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<EnabledByUser Value="true" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t\t<Index Value="{idx}" />\n'
            f'\t\t\t\t\t\t\t\t\t\t\t\t</AudioBranchSendInfo>\n'
        )
    xml += '\t\t\t\t\t\t\t\t\t\t\t</SendInfos>\n'
    return xml, id_counter

def build_als(bpm, drum_tracks, simpler_tracks,
              drum_clips=None, simpler_clips=None, n_scenes=8,
              pad_bus_map=None, bus_sidechain_map=None, bus_mixer_map=None,
              master_sidechain=None, strip_default_returns=False):
    """
    Build ALS XML with all devices embedded inline.
    drum_tracks:        list of (group_name, group_index, adg_pads)
    simpler_tracks:     list of (pad_label_str, rel_path, pad_data, display_name, wav_abs_path)
    drum_clips:         dict group_name -> {slot_idx: (clip_name, num_bars, note_events)}
    simpler_clips:      dict pad_num    -> {slot_idx: (clip_name, num_bars, note_events)}
    n_scenes:           total number of scenes/ClipSlots to create (min 8, max 32)
    pad_bus_map:        dict pad_num -> koala bus int; if any value is 0-3, bus mode activates
    bus_sidechain_map:  dict bus_index (0-3) -> sidechain params from mixer.json
    """
    if drum_clips is None:        drum_clips = {}
    if simpler_clips is None:     simpler_clips = {}
    if pad_bus_map is None:       pad_bus_map = {}
    if bus_sidechain_map is None: bus_sidechain_map = {}
    if bus_mixer_map is None:    bus_mixer_map = {}
    n_scenes = max(8, min(32, n_scenes))

    # Detect bus mode: any pad with bus 0-3 triggers full bus setup for all groups
    bus_mode = (any(v >= 0 for v in pad_bus_map.values())
              or bool(bus_sidechain_map)
              or master_sidechain is not None)

    blank_xml      = _als_load_blank()
    midi_blocks    = _als_extract_blocks(blank_xml, "MidiTrack")
    return_blocks  = _als_extract_blocks(blank_xml, "ReturnTrack")
    template_track = midi_blocks[0]

    all_high = [int(m.group(1)) for m in re.finditer(r'Id="(\d+)"', blank_xml)
                if int(m.group(1)) > 100]
    id_start = (max(all_high) + 100) if all_high else 30000

    # Assign ReturnTrack IDs for the 4 bus channels if needed.
    # They sit after all MidiTracks in the Tracks block.
    # We base their IDs on the blank template's highest track ID + offset.
    blank_track_ids = [int(m.group(1)) for m in
                       re.finditer(r'<(?:MidiTrack|ReturnTrack|AudioTrack) Id="(\d+)"', blank_xml)]
    base_track_id = (max(blank_track_ids) + 1) if blank_track_ids else 20
    # 4 bus ReturnTrack IDs: base, base+1, base+2, base+3
    rt_ids = [base_track_id + i for i in range(4)]

    # In bus mode, generate the ReturnTracks first so we know their actual
    # remapped IDs before building drum racks (which embed routing strings).
    if bus_mode:
        # Pre-generate ReturnTracks so actual IDs are known before drum racks are built.
        # First pass: generate all 4 to get their remapped IDs (sidechain source needs real IDs).
        bus_rt_xml_pre = ""
        actual_rt_ids = []
        _temp_xmls = []
        for bi in range(4):
            mx = bus_mixer_map.get(bi, {})
            rt_xml, id_start = _bus_return_track_xml(
                rt_ids[bi], bi, id_start, n_scenes,
                bus_volume_db=mx.get('volume', 0.0),
                bus_muted=mx.get('mute', False))
            actual_id = re.search(r'<ReturnTrack Id="(\d+)"', rt_xml)
            actual_rt_ids.append(int(actual_id.group(1)) if actual_id else rt_ids[bi])
            _temp_xmls.append(rt_xml)
        rt_ids = actual_rt_ids   # now contains the real remapped IDs

        # Second pass: regenerate any bus that has sidechain, now that source IDs are known.
        for bi in range(4):
            sc = bus_sidechain_map.get(bi)
            if sc is not None:
                source_bus_idx = int(sc.get('source', 0))
                source_rt_id   = rt_ids[source_bus_idx]
                source_name    = _BUS_RETURN_TRACK_NAMES[source_bus_idx]
                mx = bus_mixer_map.get(bi, {})
                rt_xml, id_start = _bus_return_track_xml(
                    rt_ids[bi], bi, id_start, n_scenes,
                    sc_params=sc, sc_source_rt_id=source_rt_id,
                    sc_source_name=source_name, sc_bypass=sc.get('bypass', False),
                    bus_volume_db=mx.get('volume', 0.0),
                    bus_muted=mx.get('mute', False))
                _temp_xmls[bi] = rt_xml
        bus_rt_xml_pre = "".join(_temp_xmls)

        # After second pass, rt_ids[i] may have changed for any bus that was
        # regenerated with a sidechain. Re-extract actual IDs from final XMLs.
        final_rt_ids = []
        for rt_xml in _temp_xmls:
            actual_id = re.search(r'<ReturnTrack Id="(\d+)"', rt_xml)
            final_rt_ids.append(int(actual_id.group(1)) if actual_id else rt_ids[len(final_rt_ids)])
        rt_ids = final_rt_ids

    midi_xml  = ""
    track_idx = 0

    # Build a lookup: group letter -> list of simpler tracks belonging to that group
    # e.g. 'C' -> [(label, rel_path, pad_data, display_name, wav_abs, chopper_p), ...]
    simpler_by_group = {}
    for st in simpler_tracks:
        group_letter = st[0][0]  # first char of pad_label_str e.g. 'C' from 'C05'
        simpler_by_group.setdefault(group_letter, []).append(st)

    def _build_simpler_track(pad_label_str, rel_path, pad_data, display_name, wav_abs, chopper_p):
        nonlocal track_idx, id_start
        colour = _ALS_TRACK_COLOURS[track_idx % len(_ALS_TRACK_COLOURS)]
        device_chain_xml, id_start = _make_simpler_device_chain(
            rel_path, pad_data, display_name, pad_label_str, id_start,
            wav_abs_path=wav_abs, chopper_params=chopper_p)
        track_display_name = f"{pad_label_str} Chopper" if chopper_p else f"Pad {pad_label_str}"
        new_track, id_start = _als_remap_track(
            template_track, 12 + track_idx, track_display_name, colour, id_start, track_idx)
        new_track = new_track.replace("<DeviceChain>\n\t\t\t\t\t\t<Devices />\n\t\t\t\t\t\t<SignalModulations />\n\t\t\t\t\t</DeviceChain>",
                                      device_chain_xml, 1)
        clips_for_simpler = {}
        for pad_num, slots in simpler_clips.items():
            if pad_label(pad_num) == pad_label_str:
                clips_for_simpler = slots
                break
        clips_by_slot = {}
        for slot_idx, (clip_name, num_bars, note_events) in clips_for_simpler.items():
            clips_by_slot[slot_idx] = _midi_clip_xml(clip_name, num_bars, note_events,
                                                      clip_colour=colour)
        new_track = _als_expand_clipslots(new_track, n_scenes)
        if clips_by_slot:
            new_track = _inject_clips(new_track, clips_by_slot)
        return "\t\t\t" + new_track.strip() + "\n"

    for group_name, group_index, adg_pads in drum_tracks:
        colour = _ALS_TRACK_COLOURS[track_idx % len(_ALS_TRACK_COLOURS)]
        # The MidiTrack ID is assigned by _als_remap_track as (12 + track_idx).
        # We need it for bus routing strings.
        midi_track_id = 12 + track_idx
        device_chain_xml, id_start = _make_drum_rack_device_chain(
            adg_pads, group_index, id_start,
            bus_mode=bus_mode, midi_track_id=midi_track_id,
            rt_ids=rt_ids, pad_bus_map=pad_bus_map)
        new_track, id_start = _als_remap_track(
            template_track, 12 + track_idx, group_name, colour, id_start, track_idx)
        new_track = new_track.replace("<DeviceChain>\n\t\t\t\t\t\t<Devices />\n\t\t\t\t\t\t<SignalModulations />\n\t\t\t\t\t</DeviceChain>",
                                      device_chain_xml, 1)
        # Inject MIDI clips into ClipSlots for this drum group
        clips_for_group = drum_clips.get(group_name, {})
        clips_by_slot = {}
        for slot_idx, (clip_name, num_bars, note_events) in clips_for_group.items():
            clips_by_slot[slot_idx] = _midi_clip_xml(clip_name, num_bars, note_events,
                                                      clip_colour=colour)
        new_track = _als_expand_clipslots(new_track, n_scenes)
        if clips_by_slot:
            new_track = _inject_clips(new_track, clips_by_slot)
        midi_xml += "\t\t\t" + new_track.strip() + "\n"
        track_idx += 1

        # Immediately append Simpler tracks belonging to this group
        group_letter = group_name[-1]  # e.g. 'A' from 'Group A'
        for st in simpler_by_group.get(group_letter, []):
            midi_xml += _build_simpler_track(*st)
            track_idx += 1

    return_xml       = "\n".join(return_blocks)

    if bus_mode:
        # Bus mode: use the pre-generated ReturnTrack XML (generated before drum racks
        # so routing strings already contain correct IDs).
        # Omit original return_blocks entirely.
        tracks_content = midi_xml + bus_rt_xml_pre
    else:
        if strip_default_returns:
            # No-bus mode: omit the 2 default ReturnTracks entirely
            tracks_content = midi_xml
        else:
            tracks_content = midi_xml + "\t\t\t" + return_xml.strip() + "\n"

    new_tracks_block = "<Tracks>\n" + tracks_content + "\t\t</Tracks>"
    result = re.sub(r"<Tracks>.*?</Tracks>", new_tracks_block,
                    blank_xml, count=1, flags=re.DOTALL)

    # No-bus mode: also strip SendsPre entries and MidiTrack Sends holders
    if strip_default_returns and not bus_mode:
        result = re.sub(r'<SendsPre>.*?</SendsPre>',
                        '<SendsPre />\n', result, count=1, flags=re.DOTALL)
        result = re.sub(r'<Sends>\s*<TrackSendHolder.*?</Sends>',
                        '<Sends />', result, flags=re.DOTALL)

    # In bus mode, replace each MidiTrack's 2 blank TrackSendHolders (Id=0, Id=1)
    # with 4 bus TrackSendHolders (Id=4, 5, 6, 7), matching the reference file exactly.
    # All other tracks (MasterTrack, PreHearTrack) also need their 2 holders replaced with 4.
    if bus_mode:
        def _make_4_send_holders(start_id, indent="\t\t\t\t\t\t\t"):
            """Build 4 TrackSendHolder XML entries. start_id = first holder Id."""
            nonlocal id_start
            xml = ""
            for i in range(4):
                h_id  = id_start; id_start += 1
                h_mid = id_start; id_start += 1
                xml += (
                    f'{indent}<TrackSendHolder Id="{start_id + i}">\n'
                    f'{indent}\t<Send>\n'
                    f'{indent}\t\t<LomId Value="0" />\n'
                    f'{indent}\t\t<Manual Value="0.0003162277571" />\n'
                    f'{indent}\t\t<MidiControllerRange>\n'
                    f'{indent}\t\t\t<Min Value="0.0003162277571" />\n'
                    f'{indent}\t\t\t<Max Value="1" />\n'
                    f'{indent}\t\t</MidiControllerRange>\n'
                    f'{indent}\t\t<AutomationTarget Id="{h_id}">\n'
                    f'{indent}\t\t\t<LockEnvelope Value="0" />\n'
                    f'{indent}\t\t</AutomationTarget>\n'
                    f'{indent}\t\t<ModulationTarget Id="{h_mid}">\n'
                    f'{indent}\t\t\t<LockEnvelope Value="0" />\n'
                    f'{indent}\t\t</ModulationTarget>\n'
                    f'{indent}\t</Send>\n'
                    f'{indent}\t<EnabledByUser Value="true" />\n'
                    f'{indent}</TrackSendHolder>\n'
                )
            return xml

        # Replace each track's 2-holder <SendInfos> block with a 4-holder version.
        # Generate FRESH IDs per track so AutomationTargets are globally unique.
        # MidiTracks get holder IDs 4,5,6,7; others get IDs 0,1,2,3.
        def _replace_track_sends(xml, track_tag, sends_tag, holder_start_id, indent):
            """Replace send holders in every <track_tag>, generating unique IDs per track."""
            pattern = (
                r'(<%s [^>]+>(?:(?!</%s>).)*?)'
                r'(<%s>\s*<TrackSendHolder.*?</%s>)'
                % (track_tag, track_tag, sends_tag, sends_tag)
            )
            def replacer(m):
                holder_xml = _make_4_send_holders(holder_start_id)
                return m.group(1) + f'<{sends_tag}>\n' + holder_xml + indent + f'</{sends_tag}>'
            return re.sub(pattern, replacer, xml, flags=re.DOTALL)

        # MidiTrack / MainTrack / PreHearTrack use <Sends> tag, 6-tab indent close
        result = _replace_track_sends(result, 'MidiTrack',   'Sends', 4, '\t\t\t\t\t\t')
        result = _replace_track_sends(result, 'MainTrack',   'Sends', 0, '\t\t\t\t\t\t')
        result = _replace_track_sends(result, 'PreHearTrack','Sends', 0, '\t\t\t\t\t\t')
        # ReturnTracks: Sends holders are already set to 4 inside _bus_return_track_xml.
        # No further patching needed for ReturnTracks here.

        # Replace SendsPre: project-level list of pre/post send states, one per ReturnTrack.
        # Blank has 2 entries (Id=0, Id=1). Bus mode needs 4 entries with IDs 4,5,6,7
        # (matching the TrackSendHolder IDs used in the MidiTrack - same numbering scheme).
        new_sends_pre = (
            '<SendsPre>\n'
            '\t\t\t<SendPreBool Id="4" Value="false" />\n'
            '\t\t\t<SendPreBool Id="5" Value="false" />\n'
            '\t\t\t<SendPreBool Id="6" Value="false" />\n'
            '\t\t\t<SendPreBool Id="7" Value="false" />\n'
            '\t\t</SendsPre>'
        )
        result = re.sub(r'<SendsPre>.*?</SendsPre>', new_sends_pre, result,
                        count=1, flags=re.DOTALL)

    # Inject master sidechain compressor into MainTrack Devices if present.
    # Uses PostFxOut routing (trigger signal taken post-FX from the source bus).
    if master_sidechain is not None and bus_mode:
        source_bus_idx  = master_sidechain.get('source', 0)
        source_rt_id    = rt_ids[source_bus_idx]
        source_name     = _BUS_RETURN_TRACK_NAMES[source_bus_idx]
        comp_xml, id_start = _bus_sidechain_compressor_xml(
            master_sidechain, source_rt_id, source_name, id_start,
            bypass=master_sidechain.get('bypass', False),
            routing_type='PostFxOut')
        result = re.sub(
            r'(<MainTrack\b[^>]*>(?:(?!</MainTrack>).)*?)<Devices />',
            lambda m: m.group(1) + '<Devices>\n' + comp_xml + '\t\t\t\t\t</Devices>',
            result, count=1, flags=re.DOTALL)

    result = re.sub(r'(<NextPointeeId Value=")(\d+)(")',
                    lambda m: f'{m.group(1)}{id_start + 500}{m.group(3)}', result, count=1)
    result = _als_set_bpm(result, bpm)
    # Expand Scenes list
    result = _als_expand_scenes(result, n_scenes)
    # Expand ClipSlots in ReturnTracks/MasterTrack/PreHearTrack (outside <Tracks>)
    # MidiTracks were already expanded per-track before clip injection above
    tracks_end = result.find('</Tracks>')
    if tracks_end > 0 and n_scenes > 8:
        after_tracks = result[tracks_end:]
        after_tracks = _als_expand_all_clipslots(after_tracks, n_scenes)
        result = result[:tracks_end] + after_tracks
    return result


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    if len(sys.argv) > 1:
        args = sys.argv[1:]
        force_no_busses = '--no-busses' in args
        input_files = [a for a in args if not a.startswith('--')]
    else:
        force_no_busses = False
        raw = input("Drag your .koala file here:\n").strip()
        input_files = shlex.split(raw)

    for koala_file in input_files:
        koala_file = koala_file.strip("'\"")
        if not os.path.isfile(koala_file):
            print(f"WARNING:  File not found: {koala_file}")
            continue

        print(f"\n>> Processing: {koala_file}")

        with zipfile.ZipFile(koala_file, 'r') as z:
            names = z.namelist()
            sampler_names = [n for n in names if n.lower().endswith("sampler.json")]
            if not sampler_names:
                print("   WARNING:  No sampler.json found"); continue
            with z.open(sampler_names[0]) as f:
                sampler_data = json.load(f)
            if "sequence.json" not in names:
                print("   WARNING:  No sequence.json found"); continue
            with z.open("sequence.json") as f:
                seq_data = json.load(f)
            song_data = None
            if "song.json" in names:
                with z.open("song.json") as f:
                    song_data = json.load(f)
            mixer_data = None
            mixer_names = [n for n in names if n.lower().endswith("mixer.json")]
            if mixer_names:
                with z.open(mixer_names[0]) as f:
                    mixer_data = json.load(f)

        pads         = sampler_data.get("pads", [])
        samples_meta = {s["id"]: s for s in sampler_data.get("samples", [])}

        if song_data and song_data.get("name", "").strip():
            project_name = song_data["name"].strip()
        else:
            project_name = os.path.splitext(os.path.basename(koala_file))[0]

        bpm = float(seq_data.get("bpm", 120.0))
        keyboard_mode = False
        selected_pad  = -1
        if song_data:
            keyboard_mode = bool(song_data.get("keyboardMode", False))
            selected_pad  = int(song_data.get("selectedPad", -1))

        print(f"   Project: {project_name}")
        print(f"   BPM:     {bpm}")
        print(f"   Loaded {len(pads)} pads\n")

        koala_dir   = os.path.dirname(os.path.abspath(koala_file))
        out_dir     = os.path.join(koala_dir, f"{project_name} Project")
        samples_dir = os.path.join(out_dir, "Samples", "Imported")
        rev_dir     = os.path.join(out_dir, "Samples", "Processed", "Reverse")
        try:
            os.makedirs(samples_dir, exist_ok=True)
        except OSError as e:
            # Output directory may be read-only (e.g. USB drive, ZIP, network path).
            # Fall back to a writable temp directory beside the script.
            import tempfile
            fallback_base = os.path.join(tempfile.gettempdir(), f"{project_name} Project")
            print(f"   WARNING: Cannot write to {out_dir}: {e}")
            print(f"   Falling back to: {fallback_base}")
            out_dir     = fallback_base
            samples_dir = os.path.join(out_dir, "Samples", "Imported")
            rev_dir     = os.path.join(out_dir, "Samples", "Processed", "Reverse")
            os.makedirs(samples_dir, exist_ok=True)

        # Create Ableton Project Info folder and .cfg file
        proj_info_dir = os.path.join(out_dir, "Ableton Project Info")
        os.makedirs(proj_info_dir, exist_ok=True)
        cfg_path = os.path.join(proj_info_dir, "AbletonProject.cfg")
        if not os.path.exists(cfg_path):
            cfg_content = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
                ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n'
                '<dict>\n'
                '\t<key>Creator</key>\n'
                '\t<string>Ableton Live 12.3.2</string>\n'
                '\t<key>MajorVersion</key>\n'
                '\t<integer>5</integer>\n'
                '\t<key>MinorVersion</key>\n'
                '\t<string>12.0_12300</string>\n'
                '\t<key>SchemaChangeCount</key>\n'
                '\t<integer>1</integer>\n'
                '</dict>\n'
                '</plist>\n'
            )
            with open(cfg_path, 'w') as _f:
                _f.write(cfg_content)

        def sample_filename(sample_id, pad_data):
            meta = samples_meta.get(sample_id, {})
            orig = meta.get("metadata", {}).get("originalPath", "")
            if orig:
                base = os.path.splitext(os.path.basename(orig))[0]
            else:
                base = pad_data.get("label", "") or f"sample_{sample_id}"
            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in base).strip()[:80]
            return safe or f"sample_{sample_id}"

        extracted_wavs = {}
        with zipfile.ZipFile(koala_file, 'r') as z:
            all_ids = set()
            for pad in pads:
                try: all_ids.add(int(pad.get("sampleId", -1)))
                except: pass
            all_ids.discard(-1)
            used_fnames = {}
            for sample_id in sorted(all_ids):
                candidate = f"sampler/{sample_id}.wav"
                if candidate not in z.namelist():
                    print(f"   WARNING:  sampler/{sample_id}.wav not found"); continue
                ref_pad    = next((p for p in pads if int(p.get("sampleId", -1)) == sample_id), {})
                base_fname = sample_filename(sample_id, ref_pad) + ".wav"
                if base_fname in used_fnames and used_fnames[base_fname] != sample_id:
                    stem  = os.path.splitext(base_fname)[0]
                    fname = f"{stem}_{sample_id}.wav"
                else:
                    fname = base_fname
                used_fnames[fname] = sample_id
                dest = os.path.join(samples_dir, fname)
                if not os.path.exists(dest):
                    z.extract(candidate, samples_dir)
                    shutil.move(os.path.join(samples_dir, f"sampler/{sample_id}.wav"), dest)
                    print(f"   OK Extracted: {fname}")
                else:
                    print(f"   OK Reused:    {fname}")
                extracted_wavs[sample_id] = dest

        stray = os.path.join(samples_dir, "sampler")
        if os.path.exists(stray):
            shutil.rmtree(stray)

        reversed_wavs = {}
        for pad in pads:
            try:
                sample_id = int(pad.get("sampleId", -1))
                is_rev    = str(pad.get("reverse", "false")).lower() == "true"
            except: continue
            if is_rev and sample_id in extracted_wavs and sample_id not in reversed_wavs:
                os.makedirs(rev_dir, exist_ok=True)
                src_wav  = extracted_wavs[sample_id]
                rev_name = os.path.splitext(os.path.basename(src_wav))[0] + " R.wav"
                rev_dest = os.path.join(rev_dir, rev_name)
                if not os.path.exists(rev_dest):
                    reverse_wav(src_wav, rev_dest)
                    print(f"   OK Reversed:  {rev_name}")
                reversed_wavs[sample_id] = rev_dest

        # -- Step 1: Build drum rack tracks ------------------------------------
        print(f"\n-- Step 1: Building drum rack tracks")
        group_pads = [[] for _ in GROUPS]
        for pad in pads:
            try:
                pad_num   = int(pad.get("pad"))
                sample_id = int(pad.get("sampleId", -1))
                if sample_id < 0 or sample_id not in extracted_wavs: continue
                g_idx = get_group_index(pad_num)
                if g_idx >= 0:
                    group_pads[g_idx].append((pad_num, pad, sample_id))
            except: continue

        drum_tracks = []
        for i, group_name in enumerate(GROUPS):
            pads_in_group = group_pads[i]
            if not pads_in_group:
                print(f"   {group_name}: no pads -> skipped"); continue
            print(f"   {group_name}: {len(pads_in_group)} pads")
            adg_pads = []
            for pad_num, pad_data, sample_id in pads_in_group:
                is_reverse = str(pad_data.get("reverse", "false")).lower() == "true"
                if is_reverse and sample_id in reversed_wavs:
                    wav_path = reversed_wavs[sample_id]
                    rel_path = f"Samples/Processed/Reverse/{os.path.basename(wav_path)}"
                else:
                    wav_path = extracted_wavs[sample_id]
                    rel_path = f"Samples/Imported/{os.path.basename(wav_path)}"
                cp_pad = _get_chopper_params(pad_data) if _is_chopper_pad(pad_data) else None
                adg_pads.append((pad_num, pad_data, os.path.basename(wav_path), rel_path, 1, wav_path, cp_pad))
            drum_tracks.append((group_name, i, adg_pads))

        # Add empty drum rack tracks for groups that have sequence notes
        # but no assigned sampler pads. This ensures MIDI clips are created.
        groups_with_tracks = {t[0] for t in drum_tracks}
        for i, group_name in enumerate(GROUPS):
            if group_name in groups_with_tracks:
                continue
            lo = GROUP_DEFS[i][1]
            hi = GROUP_DEFS[i][2]
            group_has_notes = False
            for seq in seq_data.get("sequences", []):
                pat = (seq.get("noteSequence", {}) or {}).get("pattern", {}) or                        seq.get("pattern", {}) or {}
                for note in (pat.get("notes") or []):
                    if lo <= int(note.get("num", -1)) <= hi and int(note.get("length", 0)) > 0:
                        group_has_notes = True
                        break
                if group_has_notes:
                    break
            if group_has_notes:
                print(f"   {group_name}: no pads but has sequence notes -> adding empty drum rack")
                drum_tracks.append((group_name, i, []))

        # -- Step 1b: Build Simpler tracks for note-mode pads ------------------
        print(f"\n-- Step 1b: Building Simpler tracks for note-mode pads")
        # Collect chopper pad numbers so we exclude them from note-mode detection
        _chopper_nums_temp = {int(p.get("pad")) for p in pads
                              if _is_chopper_pad(p) and p.get("pad") is not None}
        note_mode_pad_nums = set()
        for seq in seq_data.get("sequences", []):
            notes = seq.get("noteSequence", {}).get("pattern", {}).get("notes") or []
            for n in notes:
                if n.get("pitch", 0.0) != 0.0 and n["num"] not in _chopper_nums_temp:
                    note_mode_pad_nums.add(n["num"])
        if keyboard_mode and selected_pad >= 0 and selected_pad not in _chopper_nums_temp:
            note_mode_pad_nums.add(selected_pad)

        pad_data_by_num = {int(p.get("pad")): p for p in pads if p.get("pad") is not None}
        if not note_mode_pad_nums:
            print("   (no note-mode pads)")

        simpler_tracks = []
        for pad_num in sorted(note_mode_pad_nums):
            pad_data_n = pad_data_by_num.get(pad_num, {})
            sample_id  = int(pad_data_n.get("sampleId", -1))
            is_reverse = str(pad_data_n.get("reverse", "false")).lower() == "true"
            if is_reverse and sample_id in reversed_wavs:
                wav_abs  = reversed_wavs[sample_id]
                rel_path = f"Samples/Processed/Reverse/{os.path.basename(wav_abs)}"
            elif sample_id in extracted_wavs:
                wav_abs  = extracted_wavs[sample_id]
                rel_path = f"Samples/Imported/{os.path.basename(wav_abs)}"
            else:
                print(f"   WARNING:  Pad {pad_num}: no sample found, skipping"); continue
            label_str    = pad_label(pad_num)
            display_name = os.path.splitext(os.path.basename(wav_abs))[0]
            simpler_tracks.append((label_str, rel_path, pad_data_n, display_name, wav_abs, None))
            print(f"   -> Pad {label_str}  ({display_name})")

        # -- Step 1c: Build Simpler tracks for chopper pads ------------------
        print(f"\n-- Step 1c: Building Simpler tracks for chopper pads")
        chopper_pad_info = {}  # pad_num -> chopper_params
        for pad in pads:
            if not _is_chopper_pad(pad):
                continue
            try:
                pad_num   = int(pad.get("pad"))
                sample_id = int(pad.get("sampleId", -1))
            except:
                continue
            if sample_id not in extracted_wavs:
                print(f"   WARNING:  Chopper pad {pad_num}: no sample, skipping")
                continue
            cp       = _get_chopper_params(pad)
            wav_abs  = extracted_wavs[sample_id]
            rel_path = f"Samples/Imported/{os.path.basename(wav_abs)}"
            label_str    = pad_label(pad_num)
            display_name = os.path.splitext(os.path.basename(wav_abs))[0]
            trigger_name = {0.0: "Note", 1.0: "Velocity", 2.0: "Random"}.get(cp['trigger_mode'], "?")
            chopper_pad_info[pad_num] = cp
            # Random is handled entirely in the drum rack (MidiRandom in branch)
            # so no separate Simpler track is needed for it.
            if cp['trigger_mode'] == 2.0:
                print(f"   -> Pad {label_str} [Chopper/Random, {cp['slice_count']} slices]  (drum rack only)")
                continue
            simpler_tracks.append((label_str, rel_path, pad, display_name, wav_abs, cp))
            print(f"   -> Pad {label_str} [Chopper/{trigger_name}, {cp['slice_count']} slices]  ({display_name})")
        if not chopper_pad_info:
            print("   (no chopper pads)")

        # Sort all Simpler tracks by pad number so they appear in ascending order
        # regardless of whether they were added in step 1b or 1c.
        simpler_tracks.sort(key=lambda t: pad_num_from_label(t[0]))

        # -- Step 2: Parse sequences into MIDI clips --------------------------
        print(f"\n-- Step 2: Parsing MIDI sequences")
        sequences = seq_data.get("sequences", [])
        print(f"   {len(sequences)} sequence(s), {len([s for s in sequences if s.get('noteSequence',{}).get('pattern',{}).get('notes')])} non-empty")

        # Build group_index_map for active drum tracks only
        group_index_map = {gname: gi for gname, gi, _ in drum_tracks}
        # note_mode_pad_nums already computed above
        drum_clips, simpler_clips = build_sequence_clips(
            seq_data, keyboard_mode, selected_pad,
            note_mode_pad_nums, group_index_map,
            chopper_pad_info=chopper_pad_info
        )
        total_clips = sum(len(v) for v in drum_clips.values()) + sum(len(v) for v in simpler_clips.values())
        print(f"   {total_clips} clip(s) generated")

        # -- Step 3: Build and write ALS ---------------------------------------
        print(f"\n-- Step 3: Building Ableton project")
        # n_scenes = index of last non-empty sequence (min 8, max 32)
        # Only create as many scenes as the project actually uses
        sequences_raw = seq_data.get("sequences", [])
        last_used = 0
        for si, sq in enumerate(sequences_raw):
            pat   = (sq.get("noteSequence", {}) or {}).get("pattern", {}) or                      sq.get("pattern", {}) or {}
            notes = pat.get("notes") or []
            if any(int(n.get("length", 0)) > 0 for n in notes):
                last_used = si + 1  # 1-based
        n_scenes = max(8, min(32, last_used))
        # Build pad_bus_map: pad_num -> koala bus (-1=master, 0-3=bus A/B/C/D)
        # Normal pads: bus at top-level pad["bus"]
        # Chopper pads: bus at pad["synthParams"]["padParams"]["bus"]
        pad_bus_map = {}
        for pad in pads:
            try:
                pnum = int(pad.get("pad"))
                if _is_chopper_pad(pad):
                    bus = pad.get("synthParams", {}).get("padParams", {}).get("bus", -1)
                else:
                    bus = pad.get("bus", -1)
                pad_bus_map[pnum] = int(bus) if bus is not None else -1
            except (TypeError, ValueError):
                pass

        # Build bus_sidechain_map from mixer.json (optional)
        # Maps bus index 0-3 -> sidechain params dict if SIDECHAIN FX is on that bus
        bus_sidechain_map = {}
        master_sidechain  = None
        if mixer_data:
            buses = mixer_data.get("buses", [])
            for bus_idx, bus_obj in enumerate(buses[:4]):
                chain = bus_obj.get("chain", [])
                sc_fx = next((fx for fx in chain if fx and fx.get("name") == "SIDECHAIN"), None)
                if sc_fx is not None:
                    params = sc_fx.get("parameters", {})
                    bus_sidechain_map[bus_idx] = {
                        "threshold": float(params.get("threshold", -20.0)),
                        "release":   float(params.get("release",   100.0)),
                        "output":    float(params.get("output",    0.0)),
                        "source":    int(float(params.get("source", 0.0))),
                        "bypass":    bool(sc_fx.get("bypass", False)),
                    }
                    src_name = _BUS_RETURN_TRACK_NAMES[bus_sidechain_map[bus_idx]["source"]]
                    print(f"   Sidechain: Bus {'ABCD'[bus_idx]} ducked by {src_name}")
            # Check master bus for sidechain
            master = mixer_data.get("master", {})
            master_chain = master.get("chain", [])
            sc_fx = next((fx for fx in master_chain if fx and fx.get("name") == "SIDECHAIN"), None)
            if sc_fx is not None:
                params = sc_fx.get("parameters", {})
                master_sidechain = {
                    "threshold": float(params.get("threshold", -20.0)),
                    "release":   float(params.get("release",   100.0)),
                    "output":    float(params.get("output",    0.0)),
                    "source":    int(float(params.get("source", 0.0))),
                    "bypass":    bool(sc_fx.get("bypass", False)),
                }
                src_name = _BUS_RETURN_TRACK_NAMES[master_sidechain["source"]]
                print(f"   Sidechain: Master ducked by {src_name}")

        # Build bus_mixer_map: volume and mute per bus from mixer.json
        bus_mixer_map = {}
        if mixer_data:
            buses = mixer_data.get("buses", [])
            for bus_idx, bus_obj in enumerate(buses[:4]):
                vol  = float(bus_obj.get("volume", 0.0))
                mute = bool(bus_obj.get("mute", False))
                solo = bool(bus_obj.get("solo", False))
                if vol != 0.0 or mute:
                    bus_mixer_map[bus_idx] = {"volume": vol, "mute": mute}
                if solo:
                    print(f"   WARNING:  Bus {'ABCD'[bus_idx]} is soloed in Koala - no ALS equivalent, skipping")

        # --no-busses flag: clear all bus-related data so build_als treats
        # this as a plain project regardless of what the koala file contains.
        if force_no_busses:
            pad_bus_map       = {k: -1 for k in pad_bus_map}
            bus_sidechain_map = {}
            bus_mixer_map     = {}
            master_sidechain  = None

        als_xml  = build_als(bpm, drum_tracks, simpler_tracks,
                             drum_clips=drum_clips, simpler_clips=simpler_clips,
                             n_scenes=n_scenes, pad_bus_map=pad_bus_map,
                             bus_sidechain_map=bus_sidechain_map,
                             bus_mixer_map=bus_mixer_map,
                             master_sidechain=master_sidechain,
                             strip_default_returns=(force_no_busses or not any(
                                 v >= 0 for v in pad_bus_map.values())))
        als_path = os.path.join(out_dir, f"{project_name}.als")
        _als_save(als_path, als_xml)
        n_drum    = len(drum_tracks)
        n_simpler = len(simpler_tracks)
        print(f"   -> {project_name}.als  ({n_drum} drum rack{'s' if n_drum!=1 else ''}, {n_simpler} Simpler track{'s' if n_simpler!=1 else ''}, {bpm} BPM)")

        print(f"\n>> Done!")
        print(f"   Output:  {out_dir}")
        print(f"   Samples: {samples_dir}\n")


if __name__ == "__main__":
    main()
