from array import array

from pipecat.frames.frames import MixerEnableFrame, MixerUpdateSettingsFrame

from hoa_voice_agent.voice.ambient_audio import AmbientNoiseMixer


async def test_ambient_mixer_adds_a_quiet_deterministic_texture() -> None:
    mixer = AmbientNoiseMixer(volume=0.4, seed=7)
    await mixer.start(24000)

    silence = b"\x00" * (240 * 2)
    mixed = await mixer.mix(silence)

    assert mixed != silence
    samples = array("h", mixed)
    assert max(abs(sample) for sample in samples) < 1000


async def test_ambient_mixer_can_be_disabled_and_tuned_at_runtime() -> None:
    mixer = AmbientNoiseMixer(volume=0.4, seed=7)
    await mixer.start(24000)
    silence = b"\x00" * (240 * 2)

    await mixer.process_frame(MixerEnableFrame(enable=False))
    assert await mixer.mix(silence) == silence

    await mixer.process_frame(MixerEnableFrame(enable=True))
    await mixer.process_frame(MixerUpdateSettingsFrame(settings={"volume": 0.0}))
    assert await mixer.mix(silence) == silence
