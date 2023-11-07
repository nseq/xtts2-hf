import sys
import io, os, stat
import subprocess
import random
from zipfile import ZipFile
import uuid
import time
import torch
import torchaudio

# By using XTTS you agree to CPML license https://coqui.ai/cpml
os.environ["COQUI_TOS_AGREED"] = "1"

# langid is used to detect language for longer text
# Most users expect text to be their own language, there is checkbox to disable it
import langid
import base64
import csv
from io import StringIO
import datetime

import gradio as gr
from scipy.io.wavfile import write
from pydub import AudioSegment

from TTS.api import TTS
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts
from TTS.utils.generic_utils import get_user_data_dir

HF_TOKEN = os.environ.get("HF_TOKEN")

from huggingface_hub import HfApi

# will use api to restart space on a unrecoverable error
api = HfApi(token=HF_TOKEN)
repo_id = "coqui/xtts"

# Use never ffmpeg binary for Ubuntu20 to use denoising for microphone input
print("Export newer ffmpeg binary for denoise filter")
ZipFile("ffmpeg.zip").extractall()
print("Make ffmpeg binary executable")
st = os.stat("ffmpeg")
os.chmod("ffmpeg", st.st_mode | stat.S_IEXEC)

# This will trigger downloading model
print("Downloading if not downloaded Coqui XTTS V2")
from TTS.utils.manage import ModelManager

model_name = "tts_models/multilingual/multi-dataset/xtts_v2"
ModelManager().download_model(model_name)
model_path = os.path.join(get_user_data_dir("tts"), model_name.replace("/", "--"))
print("XTTS downloaded")

config = XttsConfig()
config.load_json(os.path.join(model_path, "config.json"))

model = Xtts.init_from_config(config)
model.load_checkpoint(
    config,
    checkpoint_path=os.path.join(model_path, "model.pth"),
    vocab_path=os.path.join(model_path, "vocab.json"),
    eval=True,
    use_deepspeed=True,
)
model.cuda()

# This is for debugging purposes only
DEVICE_ASSERT_DETECTED = 0
DEVICE_ASSERT_PROMPT = None
DEVICE_ASSERT_LANG = None

supported_languages = config.languages

def predict(
    prompt,
    language,
    audio_file_pth,
    mic_file_path,
    use_mic,
    voice_cleanup,
    no_lang_auto_detect,
    agree,
):
    if agree == True:
        if language not in supported_languages:
            gr.Warning(
                f"Language you put {language} in is not in is not in our Supported Languages, please choose from dropdown"
            )

            return (
                None,
                None,
                None,
                None,
            )

        language_predicted = langid.classify(prompt)[
            0
        ].strip()  # strip need as there is space at end!

        # tts expects chinese as zh-cn
        if language_predicted == "zh":
            # we use zh-cn
            language_predicted = "zh-cn"

        print(f"Detected language:{language_predicted}, Chosen language:{language}")

        # After text character length 15 trigger language detection
        if len(prompt) > 15:
            # allow any language for short text as some may be common
            # If user unchecks language autodetection it will not trigger
            # You may remove this completely for own use
            if language_predicted != language and not no_lang_auto_detect:
                # Please duplicate and remove this check if you really want this
                # Or auto-detector fails to identify language (which it can on pretty short text or mixed text)
                gr.Warning(
                    f"It looks like your text isn’t the language you chose , if you’re sure the text is the same language you chose, please check disable language auto-detection checkbox"
                )

                return (
                    None,
                    None,
                    None,
                    None,
                )

        if use_mic == True:
            if mic_file_path is not None:
                speaker_wav = mic_file_path
            else:
                gr.Warning(
                    "Please record your voice with Microphone, or uncheck Use Microphone to use reference audios"
                )
                return (
                    None,
                    None,
                    None,
                    None,
                )

        else:
            speaker_wav = audio_file_pth

        # Filtering for microphone input, as it has BG noise, maybe silence in beginning and end
        # This is fast filtering not perfect

        # Apply all on demand
        lowpassfilter = denoise = trim = loudness = True

        if lowpassfilter:
            lowpass_highpass = "lowpass=8000,highpass=75,"
        else:
            lowpass_highpass = ""

        if trim:
            # better to remove silence in beginning and end for microphone
            trim_silence = "areverse,silenceremove=start_periods=1:start_silence=0:start_threshold=0.02,areverse,silenceremove=start_periods=1:start_silence=0:start_threshold=0.02,"
        else:
            trim_silence = ""

        if voice_cleanup:
            try:
                out_filename = (
                    speaker_wav + str(uuid.uuid4()) + ".wav"
                )  # ffmpeg to know output format

                # we will use newer ffmpeg as that has afftn denoise filter
                shell_command = f"./ffmpeg -y -i {speaker_wav} -af {lowpass_highpass}{trim_silence} {out_filename}".split(
                    " "
                )

                command_result = subprocess.run(
                    [item for item in shell_command],
                    capture_output=False,
                    text=True,
                    check=True,
                )
                speaker_wav = out_filename
                print("Filtered microphone input")
            except subprocess.CalledProcessError:
                # There was an error - command exited with non-zero code
                print("Error: failed filtering, use original microphone input")
        else:
            speaker_wav = speaker_wav

        if len(prompt) < 2:
            gr.Warning("Please give a longer prompt text")
            return (
                None,
                None,
                None,
                None,
            )
        if len(prompt) > 200:
            gr.Warning(
                "Text length limited to 200 characters for this demo, please try shorter text. You can clone this space and edit code for your own usage"
            )
            return (
                None,
                None,
                None,
                None,
            )
        global DEVICE_ASSERT_DETECTED
        if DEVICE_ASSERT_DETECTED:
            global DEVICE_ASSERT_PROMPT
            global DEVICE_ASSERT_LANG
            # It will likely never come here as we restart space on first unrecoverable error now
            print(
                f"Unrecoverable exception caused by language:{DEVICE_ASSERT_LANG} prompt:{DEVICE_ASSERT_PROMPT}"
            )

        try:
            metrics_text = ""
            t_latent = time.time()

            # note diffusion_conditioning not used on hifigan (default mode), it will be empty but need to pass it to model.inference
            try:
                (
                    gpt_cond_latent,
                    speaker_embedding,
                ) = model.get_conditioning_latents(audio_path=speaker_wav)
            except Exception as e:
                print("Speaker encoding error", str(e))
                gr.Warning(
                    "It appears something wrong with reference, did you unmute your microphone?"
                )
                return (
                    None,
                    None,
                    None,
                    None,
                )

            latent_calculation_time = time.time() - t_latent
            # metrics_text=f"Embedding calculation time: {latent_calculation_time:.2f} seconds\n"
            
            wav_chunks = []
            ## Direct mode
            """
            print("I: Generating new audio...")
            t0 = time.time()
            out = model.inference(
                prompt,
                language,
                gpt_cond_latent,
                speaker_embedding,
                diffusion_conditioning
            )
            inference_time = time.time() - t0
            print(f"I: Time to generate audio: {round(inference_time*1000)} milliseconds")
            metrics_text+=f"Time to generate audio: {round(inference_time*1000)} milliseconds\n"
            real_time_factor= (time.time() - t0) / out['wav'].shape[-1] * 24000
            print(f"Real-time factor (RTF): {real_time_factor}")
            metrics_text+=f"Real-time factor (RTF): {real_time_factor:.2f}\n"
            torchaudio.save("output.wav", torch.tensor(out["wav"]).unsqueeze(0), 24000)
            """
            
            print("I: Generating new audio in streaming mode...")
            t0 = time.time()
            chunks = model.inference_stream(
                prompt,
                language,
                gpt_cond_latent,
                speaker_embedding,
                #repetition_penalty=5.0,
                #temperature=0.75,
            )

            first_chunk = True
            for i, chunk in enumerate(chunks):
                if first_chunk:
                    first_chunk_time = time.time() - t0
                    metrics_text += f"Latency to first audio chunk: {round(first_chunk_time*1000)} milliseconds\n"
                    first_chunk = False
                wav_chunks.append(chunk)
                print(f"Received chunk {i} of audio length {chunk.shape[-1]}")
            inference_time = time.time() - t0
            print(
                f"I: Time to generate audio: {round(inference_time*1000)} milliseconds"
            )
            #metrics_text += (
            #    f"Time to generate audio: {round(inference_time*1000)} milliseconds\n"
            #)
            
            wav = torch.cat(wav_chunks, dim=0)
            print(wav.shape)
            real_time_factor = (time.time() - t0) / wav.shape[0] * 24000
            print(f"Real-time factor (RTF): {real_time_factor}")
            metrics_text += f"Real-time factor (RTF): {real_time_factor:.2f}\n"

            torchaudio.save("output.wav", wav.squeeze().unsqueeze(0).cpu(), 24000)

        except RuntimeError as e:
            if "device-side assert" in str(e):
                # cannot do anything on cuda device side error, need tor estart
                print(
                    f"Exit due to: Unrecoverable exception caused by language:{language} prompt:{prompt}",
                    flush=True,
                )
                gr.Warning("Unhandled Exception encounter, please retry in a minute")
                print("Cuda device-assert Runtime encountered need restart")
                if not DEVICE_ASSERT_DETECTED:
                    DEVICE_ASSERT_DETECTED = 1
                    DEVICE_ASSERT_PROMPT = prompt
                    DEVICE_ASSERT_LANG = language

                # just before restarting save what caused the issue so we can handle it in future
                # Uploading Error data only happens for unrecovarable error
                error_time = datetime.datetime.now().strftime("%d-%m-%Y-%H:%M:%S")
                error_data = [
                    error_time,
                    prompt,
                    language,
                    audio_file_pth,
                    mic_file_path,
                    use_mic,
                    voice_cleanup,
                    no_lang_auto_detect,
                    agree,
                ]
                error_data = [str(e) if type(e) != str else e for e in error_data]
                print(error_data)
                print(speaker_wav)
                write_io = StringIO()
                csv.writer(write_io).writerows([error_data])
                csv_upload = write_io.getvalue().encode()

                filename = error_time + "_" + str(uuid.uuid4()) + ".csv"
                print("Writing error csv")
                error_api = HfApi()
                error_api.upload_file(
                    path_or_fileobj=csv_upload,
                    path_in_repo=filename,
                    repo_id="coqui/xtts-flagged-dataset",
                    repo_type="dataset",
                )

                # speaker_wav
                print("Writing error reference audio")
                speaker_filename = (
                    error_time + "_reference_" + str(uuid.uuid4()) + ".wav"
                )
                error_api = HfApi()
                error_api.upload_file(
                    path_or_fileobj=speaker_wav,
                    path_in_repo=speaker_filename,
                    repo_id="coqui/xtts-flagged-dataset",
                    repo_type="dataset",
                )

                # HF Space specific.. This error is unrecoverable need to restart space
                api.restart_space(repo_id=repo_id)
            else:
                if "Failed to decode" in str(e):
                    print("Speaker encoding error", str(e))
                    gr.Warning(
                        "It appears something wrong with reference, did you unmute your microphone?"
                    )
                else:
                    print("RuntimeError: non device-side assert error:", str(e))
                    gr.Warning("Something unexpected happened please retry again.")
                return (
                    None,
                    None,
                    None,
                    None,
                )
        return (
            gr.make_waveform(
                audio="output.wav",
            ),
            "output.wav",
            metrics_text,
            speaker_wav,
        )
    else:
        gr.Warning("Please accept the Terms & Condition!")
        return (
            None,
            None,
            None,
            None,
        )


title = "Coqui🐸 XTTS"

description = """
<div>
<a style="display:inline-block" href='https://github.com/coqui-ai/TTS'><img src='https://img.shields.io/github/stars/coqui-ai/TTS?style=social' /></a>
<a style='display:inline-block' href='https://discord.gg/5eXr5seRrv'><img src='https://discord.com/api/guilds/1037326658807533628/widget.png?style=shield' /></a>
<a href="https://huggingface.co/spaces/coqui/xtts?duplicate=true">
<img style="margin-top: 0em; margin-bottom: 0em" src="https://bit.ly/3gLdBN6" alt="Duplicate Space"></a>
</div>

<a href="https://huggingface.co/coqui/XTTS-v2">XTTS</a> is a Voice generation model that lets you clone voices into different languages by using just a quick 6-second audio clip. 
<br/>
XTTS is built on previous research, like Tortoise, with additional architectural innovations and training to make cross-language voice cloning and multilingual speech generation possible. 
<br/>
This is the same model that powers our creator application <a href="https://coqui.ai">Coqui Studio</a> as well as the <a href="https://docs.coqui.ai">Coqui API</a>. In production we apply modifications to make low-latency streaming possible.
<br/>
Leave a star on the Github <a href="https://github.com/coqui-ai/TTS">🐸TTS</a>, where our open-source inference and training code lives.
<br/>
<p>For faster inference without waiting in the queue, you should duplicate this space and upgrade to GPU via the settings.
<br/>
</p>
<p>Language Selectors: 
Arabic: ar, Brazilian Portuguese: pt , Chinese: zh-cn, Czech: cs, Dutch: nl, English: en, French: fr, Italian: it, Polish: pl,<br/> 
Russian: ru, Spanish: es, Turkish: tr, Japanese: ja, Korean: ko, Hungarian: hu <br/> 
</p>
<img referrerpolicy="no-referrer-when-downgrade" src="https://static.scarf.sh/a.png?x-pxid=0d00920c-8cc9-4bf3-90f2-a615797e5f59" />
"""

article = """
<div style='margin:20px auto;'>
<p>By using this demo you agree to the terms of the Coqui Public Model License at https://coqui.ai/cpml</p>
<p>We collect data only for error cases for improvement.</p>
</div>
"""
examples = [
    [
        "Once when I was six years old I saw a magnificent picture",
        "en",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Lorsque j'avais six ans j'ai vu, une fois, une magnifique image",
        "fr",
        "examples/male.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Als ich sechs war, sah ich einmal ein wunderbares Bild",
        "de",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Cuando tenía seis años, vi una vez una imagen magnífica",
        "es",
        "examples/male.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Quando eu tinha seis anos eu vi, uma vez, uma imagem magnífica",
        "pt",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Kiedy miałem sześć lat, zobaczyłem pewnego razu wspaniały obrazek",
        "pl",
        "examples/male.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Un tempo lontano, quando avevo sei anni, vidi un magnifico disegno",
        "it",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Bir zamanlar, altı yaşındayken, muhteşem bir resim gördüm",
        "tr",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Когда мне было шесть лет, я увидел однажды удивительную картинку",
        "ru",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Toen ik een jaar of zes was, zag ik op een keer een prachtige plaat",
        "nl",
        "examples/male.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "Když mi bylo šest let, viděl jsem jednou nádherný obrázek",
        "cs",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "当我还只有六岁的时候， 看到了一副精彩的插画",
        "zh-cn",
        "examples/female.wav",
        None,
        False,
        False,
        False,
        True,
    ],
    [
        "かつて 六歳のとき、素晴らしい絵を見ました",
        "ja",
        "examples/female.wav",
        None,
        False,
        True,
        False,
        True,
    ],
    [
        "한번은 내가 여섯 살이었을 때 멋진 그림을 보았습니다.",
        "ko",
        "examples/female.wav",
        None,
        False,
        True,
        False,
        True,
    ],
        [
        "Egyszer hat éves koromban láttam egy csodálatos képet",
        "hu",
        "examples/male.wav",
        None,
        False,
        True,
        False,
        True,
    ],
]


gr.Interface(
    fn=predict,
    inputs=[
        gr.Textbox(
            label="Text Prompt",
            info="One or two sentences at a time is better. Up to 200 text characters.",
            value="Hi there, I'm your new voice clone. Try your best to upload quality audio",
        ),
        gr.Dropdown(
            label="Language",
            info="Select an output language for the synthesised speech",
            choices=[
                "en",
                "es",
                "fr",
                "de",
                "it",
                "pt",
                "pl",
                "tr",
                "ru",
                "nl",
                "cs",
                "ar",
                "zh-cn",
                "ja",
                "ko",
                "hu"
            ],
            max_choices=1,
            value="en",
        ),
        gr.Audio(
            label="Reference Audio",
            info="Click on the ✎ button to upload your own target speaker audio",
            type="filepath",
            value="examples/female.wav",
        ),
        gr.Audio(
            source="microphone",
            type="filepath",
            info="Use your microphone to record audio",
            label="Use Microphone for Reference",
        ),
        gr.Checkbox(
            label="Use Microphone",
            value=False,
            info="Notice: Microphone input may not work properly under traffic",
        ),
        gr.Checkbox(
            label="Cleanup Reference Voice",
            value=False,
            info="This check can improve output if your microphone or reference voice is noisy",
        ),
        gr.Checkbox(
            label="Do not use language auto-detect",
            value=False,
            info="Check to disable language auto-detection",
        ),
        gr.Checkbox(
            label="Agree",
            value=False,
            info="I agree to the terms of the Coqui Public Model License at https://coqui.ai/cpml",
        ),
    ],
    outputs=[
        gr.Video(label="Waveform Visual"),
        gr.Audio(label="Synthesised Audio", autoplay=True),
        gr.Text(label="Metrics"),
        gr.Audio(label="Reference Audio Used"),
    ],
    title=title,
    description=description,
    article=article,
    examples=examples,
).queue().launch(debug=True, show_api=True)
