import asyncio
import glob
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

import click
import pandas as pd
import psutil
from faster_whisper import WhisperModel
from numba import cuda
from pytube import Playlist, YouTube

# single_video_url = 'https://www.youtube.com/watch?v=sWAaJF9Wk0w'  # Single video URL
# playlist_url = 'https://www.youtube.com/playlist?list=PLjpPMe3LP1XKgqqzqz4j6M8-_M_soYxiV' # Playlist URL

def add_to_system_path(new_path):
    if new_path not in os.environ["PATH"].split(os.pathsep): # Check if the new path already exists in PATH
        os.environ["PATH"] = new_path + os.pathsep + os.environ["PATH"] # Add the new path to PATH
    if sys.platform == "win32" and ' ' in new_path and not new_path.startswith('"') and not new_path.endswith('"'): # For Windows, wrap the path in quotes if it contains spaces and isn't already quoted
        os.environ["PATH"] = f'"{new_path}"' + os.pathsep + os.environ["PATH"].replace(new_path, "")

def get_cuda_toolkit_path():
    home_dir = os.path.expanduser('~')  # Get the home directory of the current user
    if sys.platform in ["win32", "linux", "linux2", "darwin"]:  # Build the base path to the Anaconda 'pkgs' directory; Works for Windows, Linux, macOS
        anaconda_base_path = os.path.join(home_dir, "anaconda3", "pkgs")
    cuda_glob_pattern = os.path.join(anaconda_base_path, "cudatoolkit-*", "Library", "bin") # Construct the glob pattern for the cudatoolkit directory
    cuda_paths = glob.glob(cuda_glob_pattern) # Use glob to find directories that match the pattern
    if cuda_paths: # Select the first matching path (assuming there is at least one match)
        return cuda_paths[0]  # Return the first matched path; This is the path to the cudatoolkit directory
        # cuda_toolkit_path = "C:/Program Files/NVIDIA Corporation/CUDNN/v8.9/bin" # For Windows, use this path if the above doesn't work
        return cuda_toolkit_path
    return None

def initialize_transcription(use_spacy_for_sentence_splitting):
    cuda_toolkit_path = get_cuda_toolkit_path()
    print("CUDA Toolkit Path:", cuda_toolkit_path)
    if cuda_toolkit_path:
        add_to_system_path(cuda_toolkit_path)

    os.makedirs('downloaded_audio', exist_ok=True)
    os.makedirs('generated_transcript_combined_texts', exist_ok=True)
    os.makedirs('generated_transcript_metadata_tables', exist_ok=True)

    if use_spacy_for_sentence_splitting:
        import spacy
        import spacy.cli
        def download_spacy_model(model_name="en_core_web_sm"):
            try:
                return spacy.load(model_name) # Load the model if already installed
            except OSError: # If not installed, download it
                print(f"Downloading spaCy model {model_name}...")
                spacy.cli.download(model_name)
                return spacy.load(model_name)
        nlp = download_spacy_model()
        def sophisticated_sentence_splitter(text):
            text = remove_pagination_breaks(text)
            doc = nlp(text)
            sentences = [sent.text.strip() for sent in doc.sents]
            return sentences
        return sophisticated_sentence_splitter
    else:
        def sophisticated_sentence_splitter(text):
            text = remove_pagination_breaks(text)
            pattern = r'\.(?!\s*(com|net|org|io)\s)(?![0-9])'  # Split on periods that are not followed by a space and a top-level domain or a number
            pattern += r'|[.!?]\s+'  # Split on whitespace that follows a period, question mark, or exclamation point
            pattern += r'|\.\.\.(?=\s)'  # Split on ellipses that are followed by a space
            sentences = re.split(pattern, text)
            refined_sentences = []
            temp_sentence = ""
            for sentence in sentences:
                if sentence is not None:
                    temp_sentence += sentence
                    if temp_sentence.count('"') % 2 == 0:  # If the number of quotes is even, then we have a complete sentence
                        refined_sentences.append(temp_sentence.strip())
                        temp_sentence = ""
            if temp_sentence:
                refined_sentences.append(temp_sentence.strip())  # Add the remaining part as the last sentence
            return [s.strip() for s in refined_sentences if s.strip()]
        return sophisticated_sentence_splitter

def clean_filename(title):
    print("Cleaning title:", title)
    title = re.sub('[^\w\s-]', '', title)
    print("Cleaned title:", title)
    return re.sub('[-\s]+', '_', title).strip().lower()

async def download_audio(video):
    filename = clean_filename(video.title)
    base_filename = filename
    counter = 1
    audio_dir = 'downloaded_audio'
    audio_file_path = os.path.join(audio_dir, f"{filename}.mp3")
    while os.path.exists(audio_file_path):
        filename = f"{base_filename}_{counter}"
        audio_file_path = os.path.join(audio_dir, f"{filename}.mp3")
        counter += 1
    if not os.path.exists(audio_file_path):
        stream = video.streams.filter(only_audio=True).first()
        if stream is None:
            raise ValueError(f"No audio stream found for video: {video.title}")
        try:
            os.makedirs(audio_dir, exist_ok=True)
            audio_file_path = stream.download(output_path=audio_dir, filename=f"{filename}.mp3")
        except Exception as e:
            print(f"Error downloading video {video.title}: {e}")
            return None, None
    return audio_file_path, filename

async def compute_transcript_with_whisper_from_audio_func(
    audio_file_path,
    audio_file_name,
    audio_file_size_mb,
    disable_cuda_override,
    cpu_threads,
    sophisticated_sentence_splitter,
):
    cuda_toolkit_path = get_cuda_toolkit_path()
    if cuda_toolkit_path:
        add_to_system_path(cuda_toolkit_path)
    combined_transcript_text = ""
    combined_transcript_text_list_of_metadata_dicts = []
    list_of_transcript_sentences = []
    if cuda.is_available() and not disable_cuda_override:
        print("CUDA is available. Using GPU for transcription.")
        device = "cuda"
        compute_type = "float16"  # Use FP16 for faster computation on GPU
    else:
        print("CUDA not available. Using CPU for transcription.")
        device = "cpu"
        compute_type = "auto"  # Use default compute type for CPU

    model = WhisperModel("large-v3", device=device, compute_type=compute_type, cpu_threads=cpu_threads)
    request_time = datetime.utcnow()
    print(f"Computing transcript for {audio_file_name} which has a {audio_file_size_mb :.2f}MB file size...")
    segments, info = await asyncio.to_thread(model.transcribe, audio_file_path, beam_size=10, vad_filter=True)
    print(f"Transcription completed, post processing...")

    if not segments:
        print(f"No segments were returned for file {audio_file_name}.")
        return [], {}, "", [], request_time, datetime.utcnow(), 0, ""

    for segment in segments:
        print(f"Processing segment: [Start: {segment.start:.2f}s, End: {segment.end:.2f}s] for file {audio_file_name} with text: {segment.text} ")
        combined_transcript_text += segment.text + "\n"
        # sentences = sophisticated_sentence_splitter(segment.text)
        # list_of_transcript_sentences.extend(sentences)
        metadata = {
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": segment.text,
            "avg_logprob": round(segment.avg_logprob, 2)
        }
        combined_transcript_text_list_of_metadata_dicts.append(metadata)
    with open(f'generated_transcript_combined_texts/{audio_file_name}.md', 'w') as file:
        file.write(combined_transcript_text)
    # df = pd.DataFrame(combined_transcript_text_list_of_metadata_dicts)
    # df.to_csv(f'generated_transcript_metadata_tables/{audio_file_name}.csv', index=False)
    # df.to_json(f'generated_transcript_metadata_tables/{audio_file_name}.json', orient='records', indent=4)
    return combined_transcript_text, combined_transcript_text_list_of_metadata_dicts, list_of_transcript_sentences

def is_single_video(url):
    return 'playlist' not in url

async def process_video_or_playlist(
    url,
    max_simultaneous_downloads,
    disable_cuda_override,
    sophisticated_sentence_splitter,
    cpu_threads,
    use_oauth,
):
    if is_single_video(url):
        print(f"Processing a single video: {url}")
        # Create a YouTube instance with the cookies
        yt = YouTube(url, use_oauth=use_oauth, allow_oauth_cache=use_oauth)

        videos = [yt]
    else:
        print(f"Processing a playlist: {url}")
        playlist = Playlist(url)
        videos = playlist.videos
    download_semaphore = asyncio.Semaphore(max_simultaneous_downloads)
    async def download_and_transcribe(video):
        try:
            async with download_semaphore:
                audio_path, audio_filename = await download_audio(video)
                if audio_path and audio_filename:
                    audio_file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
                    await compute_transcript_with_whisper_from_audio_func(
                        audio_path, audio_filename, audio_file_size_mb, disable_cuda_override, cpu_threads, sophisticated_sentence_splitter
                    )
        except Exception as e:
            print(f"Error processing video {video.title}: {e}")
    tasks = [download_and_transcribe(video) for video in videos]
    await asyncio.gather(*tasks)

def normalize_logprobs(avg_logprob, min_logprob, max_logprob):
    range_logprob = max_logprob - min_logprob
    return (avg_logprob - min_logprob) / range_logprob if range_logprob != 0 else 0.5

def remove_pagination_breaks(text: str) -> str:
    text = re.sub(r'-(\n)(?=[a-z])', '', text) # Remove hyphens at the end of lines when the word continues on the next line
    text = re.sub(r'(?<=\w)(?<![.?!-]|\d)\n(?![\nA-Z])', ' ', text) # Replace line breaks that are not preceded by punctuation or list markers and not followed by an uppercase letter or another line break
    return text

@click.command()
@click.argument('url')
@click.option('--spacy', '-p', is_flag=True, default=False, help='Use SpaCy for sentence splitting.')
@click.option('--max-downloads', '-m', default=1, help='Maximum simultaneous YouTube downloads.')
@click.option('--cuda', '-c', is_flag=True, default=False, help='Use CUDA if available.')
@click.option('--cpu-threads', '-t', default=4, help='Number of CPU threads for Whisper transcription.')
@click.option('--oauth', '-o', is_flag=True, default=False, help='Use oauth to bypass age restrictions.')
def main(url, spacy, max_downloads, cuda, cpu_threads, oauth):
    use_spacy_for_sentence_splitting = 1 if spacy else 0
    max_simultaneous_youtube_downloads = max_downloads
    disable_cuda_override = 0 if cuda else 1
    print(f"spacy: {spacy}")
    print(f"max_downloads: {max_downloads}")
    print(f"cuda: {cuda}")

    sophisticated_sentence_splitter = initialize_transcription(use_spacy_for_sentence_splitting)

    asyncio.run(process_video_or_playlist(
        url, max_simultaneous_youtube_downloads, disable_cuda_override, sophisticated_sentence_splitter, cpu_threads, oauth
    ))

if __name__ == '__main__':
    main()


"""
NOTE: There was an age restriction error even with oauth=True, the solution is in pytube/__main__.py line 253
https://github.com/pytube/pytube/issues/1712

def bypass_age_gate(self):
    innertube = InnerTube(
        client='ANDROID',  # ANDROID instead of ANDROID_EMBED worked!
        use_oauth=self.use_oauth,
        allow_cache=self.allow_oauth_cache
    )
    innertube_response = innertube.player(self.video_id)

    playability_status = innertube_response['playabilityStatus'].get('status', None)

    # If we still can't access the video, raise an exception
    # (tier 3 age restriction)
    if playability_status == 'UNPLAYABLE':
        raise exceptions.AgeRestrictedError(self.video_id)

    self._vid_info = innertube_response
"""