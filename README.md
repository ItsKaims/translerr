# Translerr

Translerr is a self-hosted beta for translating existing English movie and TV
subtitles. It reads a matching English SRT, an embedded English text track, or
an English Blu-ray PGS track via local OCR. It writes a timed SRT in the selected
language beside the video for Jellyfin and other media servers.

## Install with Docker Compose

For the prebuilt Linux x86-64 image, grab `compose.image.yaml` and
`translerr.env.example` from the
[beta release](https://github.com/ItsKaims/translerr/releases/tag/v0.1.0).
Save the environment example as `.env` and follow the same path and permission
steps below. Start it with `docker compose -f compose.image.yaml up -d`.
The image is pinned to the beta version. You can override it using
`TRANSLERR_IMAGE`.

For a build from source:

1. Copy `.env.example` to `.env`. Set `MOVIES_PATH` and `TV_PATH` to existing
   library directories on the Docker host. If you only have one library, use an
   empty directory for the other.
2. Set `PUID` and `PGID` to a user and group that can read the videos and write to
   their directories. Use `id your-user` on the host to find the numbers.
3. Create `STATE_PATH` with mode `700` and make that same UID and GID its owner.
   The default path is `./state` beside the Compose file. For example, for
   UID and GID 1000, run `sudo install -d -m 700 -o 1000 -g 100 ./state`.
4. Run `docker compose up -d --build`.
5. Open http://127.0.0.1:8787 on the Docker host. From another machine, use
   `ssh -L 8787:127.0.0.1:8787 user@your-server` and open the same URL locally.

For OMV, create a project under **Services → Compose → Files**, paste
`compose.image.yaml` into **File** and your environment values into
**Environment**, then click **Up**. For a source build, use `compose.yaml`
and copy `Dockerfile`, `.dockerignore`, `app.py`, `index.html`, `admin.html`,
and `login.html` beside the YAML. An absolute `STATE_PATH` outside the Compose
project folder keeps OMV's project permissions separate from app data.

## First login

Sign in as `admin` with temporary password `translerr`. You must choose a new
password of at least 12 characters before you can access the library or admin
settings. An optional `TRANSLERR_SETUP_PASSWORD` overrides the temporary
password on a fresh installation. Set a unique one before opening access to
other hosts. Existing installations keep their stored password.

The default bind address is loopback. For HTTPS access through a reverse proxy,
set `TRANSLERR_SECURE_COOKIE=1` and keep port 8787 private. If you bind to a LAN
address for HTTP, restrict access to trusted devices. The login uses one
administrator account. Restarting the app signs users out.

## Translate and resume

In **Admin settings**, enter an OpenAI or Anthropic API key, load the available
models, and select a model that supports text generation. In the library,
choose the target language, select titles, and click **Translate selected**.

Translerr saves queue progress in SQLite after each title and automatically
resumes an interrupted queue on startup. If interrupted mid-title, that title
starts over, which may repeat API calls. A completed SRT is reused if the
app stopped just after saving it. Provider HTTP and connectivity errors pause
the queue. Correct the provider settings or quota, then choose **Resume queue**.
**Discard remaining items** clears the pending work and keeps saved subtitles.
If your media paths change, the queue may no longer find the original titles.

To update an existing target-language SRT, choose the target language and
provider, then click **Regenerate subtitle**. Select the video and an English
source. The existing subtitle stays available until translation succeeds, then
Translerr keeps it as a hidden `.bak` file next to the replacement.

## Privacy and storage

Translerr reads subtitle tracks and performs PGS OCR locally. It sends the
English subtitle dialogue and nearby cues as context to the selected provider.
It does not upload the video, audio, or subtitle images. API usage may incur
charges. The model receives the target language, while Translerr retains the
cue timing. Provider API keys are stored as plaintext in the private SQLite
database and are never returned by the settings API. Login passwords are
stored as salted hashes. Treat the state directory and its backups as secrets.

## Beta test checklist

- Try one matching `.en.srt`, one embedded English text track, and one English
  PGS track. Check timing, slang, and line breaks in the resulting subtitles.
- Queue two or more titles and restart after the first saves. Verify the
  completed file remains and the rest of the queue resumes.
- Check that an existing target-language sidecar or embedded track is excluded.
- Refresh the media server's library if the new SRT is not listed for playback.

OCR can misread stylized text. Translation quality depends on the model, and
embedded tracks must be tagged with a recognized language code.

## License

MIT. See [LICENSE](LICENSE). FFmpeg, Tesseract, and pgsrip are third-party
components distributed under their own licenses.
