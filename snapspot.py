#!/usr/bin/env python
# Copyright (c) 2025 elParaguayo
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import argparse
import asyncio
import contextlib
import functools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from configparser import ConfigParser
from pathlib import Path

from spotipy import CacheFileHandler, Spotify
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth


class SnapSpotError(Exception):
    pass


GLOBAL_CONFIG_PATH = Path("~/.config/snapspot/config.ini").expanduser().as_posix()

SCOPES = (
    "user-read-playback-state,user-modify-playback-state,user-read-currently-playing"
)

LIBRESPOT = shutil.which("librespot")

if LIBRESPOT is None:
    raise SnapSpotError("librespot is not installed.")

# Define regexes to match librespot output

# Match Spotify Track ID
# 2025-01-25T11:12:04Z INFO  librespot_playback::player] Loading <As If You've Never Been Away - 2019 remaster> with Spotify URI <spotify:track:1VWvxVILVBs9TZ0ngye6DW>
RE_TRACK_ID = re.compile(r".*librespot_playback.*Spotify URI <(?P<uri>spotify:.*:.*)>")

# Match current user
# 2025-01-25T11:12:04Z INFO  librespot_core::session] Authenticated as 'elparaguayosnapspot' !
RE_USER = re.compile(r".*librespot_core.*Authenticated as '(?P<username>.*)'")

# Match playback status
# 2025-01-25T14:41:56Z DEBUG librespot_connect::state] updated connect play status playing: true, paused: false, buffering: true
RE_STATUS = re.compile(
    r"play status playing: (?P<playing>true|false), paused: (?P<paused>true|false), buffering"
)

# Match volumen
# 2025-01-25T14:50:58Z INFO  librespot_connect::spirc] delayed volume update for all devices: volume is now 65535
RE_VOLUME = re.compile(r"volume is now (?P<volume>[0-9]+)")


def needs_spotify_token(error_return_value=None):
    """
    Decorator to validate API token before trying to make API calls.

    Can take an optional parameter "error_return_value" to provide a custom
    return value if the token cannot be validated.
    """

    def _wrapper(func):
        @functools.wraps(func)
        def _refresh_token(self, *args, **kwargs):
            am = self.spotify.auth_manager
            token = am.cache_handler.get_cached_token()
            success = am.validate_token(token)
            if success is None:
                self.queue(self.error("Could not validate token."))
                return error_return_value

            return func(self, *args, **kwargs)

        return _refresh_token

    return _wrapper


class PlaybackStatus:
    Playing = "playing"
    Stopped = "stopped"
    Paused = "paused"


class SnapSpot:
    """
    A script plugin for snapcast.

    However, unlike other plugins, this plugin is also responsible for creating the audio stream.
    This is because librespot only provides the track name in its output so snapcast's built-in
    librespot stream is limited to the title for metadata.

    This plugin reads the spotify uri and uses the spotify api to retrieve additional information about the
    track in order to provide additional metadata.

    In addition, if the current user is the same as the web api user then it is possible to control the playback
    stream.
    """

    def __init__(
        self,
        client_id="",
        client_secret="",
        redirect_uri="",
        credentials_location=None,
        username="",
        password="",
        name="",
        bitrate="320",
        volume="100",
        onevent="",
        normalize=False,
        autoplay=None,
        cache="",
        disable_audio_cache=True,
        backend="pipe",
        audio_device="/tmp/spotpipe",
        params=list(),
        debug=False,
        **kwargs
    ):
        if not (client_id and client_secret):
            raise SnapSpotError("client_id and client_secret values must be set.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.credentials_location = credentials_location

        self.librespot_args = []
        if username and password:
            self.librespot_args.extend(["--username", username, "--password", password])
        if name:
            self.librespot_args.extend(["--name", name])
        self.librespot_args.extend(["--bitrate", bitrate])
        self.librespot_args.extend(["--initial-volume", volume])
        if onevent:
            self.librespot_args.extend(["--onevent", onevent])
        if normalize:
            self.librespot_args.append("--enable-volume-normalisation")
        if autoplay:
            self.librespot_args.extend(["--autoplay", autoplay])
        if cache:
            self.librespot_args.extend(["--cache", cache])
        if disable_audio_cache:
            self.librespot_args.append("--disable-audio-cache")
        if backend:
            self.librespot_args.extend(["--backend", backend])
        if audio_device:
            self.librespot_args.extend(["--device", audio_device])
        if params:
            self.librespot_args.extend(params)
        self.librespot_args.append("--verbose")

        self.proc = None

        self._metadata = {}
        self._properties = {}
        self._properties["playbackStatus"] = PlaybackStatus.Stopped
        self._properties["loopStatus"] = "none"
        self._properties["shuffle"] = False
        self._properties["volume"] = 100
        self._properties["mute"] = False
        self._properties["rate"] = 1.0
        self._properties["position"] = 0

        self.now_playing = ""
        self.track_cache = {}

        self.api_user = "<NOT SET>"
        self.playing_user = "<STARTUP>"
        self.set_user("", startup=True)

        self._librespot_ready = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._tasks = []

        self.verbose = debug

    def start(self):
        """Entry point to start the plugin."""
        self._loop = asyncio.new_event_loop()
        for sig in [signal.SIGINT, signal.SIGTERM]:
            self._loop.add_signal_handler(sig, self._stop_event.set)
        self._loop.run_until_complete(self._start())

    def queue(self, coroutine, done_callback=None):
        """
        Helper method to add a task to the loop.

        Where no callback is provided, a default no_op is used.
        """

        # Wrapper around queued tasks so we can cancel them
        # at exit
        def wrap(func):
            def _wrapped_task(task):
                func(task)
                self._tasks.remove(task)

            return _wrapped_task

        def no_op(_):
            pass

        task = asyncio.create_task(coroutine)
        self._tasks.append(task)
        task.add_done_callback(wrap(done_callback or no_op))

    def run_in_executor(self, func, *args):
        """Helper method to run synchronous functions in a thread."""
        return self._loop.run_in_executor(None, func, *args)

    async def _start(self):
        """
        Starts core functions of the plugin:
        - Creates a reader/writer to communicate with snapcast via stdin/stdout.
        - Starts librespot and starts loop to read output
        - Starts loop to listen for commands from snapcast
        """
        self.reader, self.writer = await self.connect_stdin_stdout()
        await self.debug("Started stdin reader/stdout writer.")
        self.queue(self.start_librespot())
        await self.debug("Successfully started librespot.")
        await self._do_api_login()
        await self._librespot_ready.wait()
        await self.send(method="Plugin.Stream.Ready")
        self.queue(self._read_stdin())
        await self._stop_event.wait()
        for task in self._tasks:
            task.cancel()

    async def _do_api_login(self):
        """Creates the Spotify API connection."""
        await self.debug("Opening Spotify API connection.")
        if self.credentials_location:
            handler = CacheFileHandler(cache_path=self.credentials_location)
        else:
            handler = None
        self.spotify = Spotify(
            auth_manager=SpotifyOAuth(
                client_id=self.client_id,
                client_secret=self.client_secret,
                redirect_uri=self.redirect_uri,
                scope=SCOPES,
                cache_handler=handler,
            ),
        )

        try:
            self.api_user = self.spotify.me()["id"]
            await self.debug(
                f"Successfully connected to Spotify api as {self.api_user}."
            )
        except SpotifyException:
            await self.error(
                "Could not get Spotify API user. No metadata or control will be available."
            )
            self.api_user = ""

    async def connect_stdin_stdout(self):
        """Creates streamreader/writer objects for communicating via stdin/stdout."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await self._loop.connect_read_pipe(lambda: protocol, sys.stdin)
        w_transport, w_protocol = await self._loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(w_transport, w_protocol, reader, self._loop)
        return reader, writer

    async def _read_stdin(self):
        while not self._stop_event.is_set():
            res = await self.reader.readline()

            if not res:
                break

            await self.parse_json_cmd(res.strip().decode())

        if not self._stop_event.is_set():
            self._stop_event.set()

    async def send(self, **kwargs):
        """Helper method to send json message to snapcast."""
        data = {"jsonrpc": "2.0"}
        data.update(kwargs)
        self.writer.write(f"{json.dumps(data)}\r\n".encode())
        await self.writer.drain()

    async def log(self, message, level="Info"):
        """Send log message to snapcast."""
        params = {"severity": level, "message": message}
        await self.send(method="Plugin.Stream.Log", params=params)

    async def debug(self, message):
        """Send debug message to snapcast."""
        await self.log(message, level="Debug")

    async def error(self, message):
        """Send error message to snapcast."""
        await self.log(message, level="Error")

    async def start_librespot(self):
        """Starts librespot process and loop to read output."""
        self.proc = await asyncio.create_subprocess_exec(
            LIBRESPOT,
            *self.librespot_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._read_librespot_output()

    async def _read_librespot_output(self):
        """Loops over librespot output to trigger actions."""
        await self.debug("Starting librespot reader")
        self._librespot_ready.set()
        while line := await self.proc.stderr.readline():
            line = line.decode()

            if user := RE_USER.match(line):
                self.set_user(user.group("username"))
            elif track := RE_TRACK_ID.match(line):
                await self.get_item(track.group("uri"))
            elif status := RE_STATUS.search(line):
                playing = status.group("playing") == "true"
                paused = status.group("paused") == "true"
                await self.update_status(playing, paused)
            elif volume := RE_VOLUME.search(line):
                await self.update_volume(int(volume.group("volume")))
            elif self.verbose:
                await self.debug(line)

    async def get_item(self, item):
        """Entry point to retrieve metadata for a Spotify item."""
        if item == self.now_playing:
            return

        items = item.split(":")
        if len(items) != 3:
            await self.log(f"Unexpected item ID {track}.")
            return

        if item in self.track_cache:
            await self.set_now_playing(item)
            return

        _, item_type, _ = items

        match item_type:
            case "track":
                self.get_track_info(item)
            case "episode":
                self.get_episode_info(item)
            case "audiobook":
                self.get_audiobook_info(item)
            case _:
                await self.log("Unexpected item type {item_type}.")
                return

    async def parse_json_cmd(self, line):
        """Entry point to parse command received from snapcast."""
        try:
            data = json.loads(line.strip())
        except json.JSONDecodeError:
            return line

        msg_id = data["id"]
        method = data["method"]
        params = data.get("params", dict())
        prefix, command = method.rsplit(".", 1)

        if prefix == "Plugin.Stream.Player":
            match command:
                case "Control":
                    match params["command"]:
                        case "play":
                            success, error = self.play()
                        case "pause":
                            success, error = self.pause()
                        case "playPause":
                            success, error = self.play_pause()
                        case "stop":
                            success, error = self.stop()
                        case "next":
                            success, error = self.next_track()
                        case "previous":
                            success, error = self.previous_track()
                        case "seek":
                            offset = params["params"]["offset"]
                            success, error = self.seek(offset)
                        case "setPosition":
                            position = params["params"]["position"]
                            success, error = self.set_position(position)

                    if success:
                        await self.send(result="ok", id=msg_id)
                    else:
                        await self.send(code=-32700, message=error)

                # TO DO
                case "SetProperty":
                    for prop, value in params.items():
                        match prop:
                            case "loopStatus":
                                # loopStatus: [string] the current repeat status, one of:
                                # none: the playback will stop when there are no more tracks to play
                                # track: the current track will start again from the begining once it has finished playing
                                # playlist: the playback loops through a list of tracks
                                pass
                            case "shuffle":
                                # [bool] play playlist in random order
                                pass
                            case "volume":
                                # [int] voume in percent, valid range [0..100]
                                pass
                            case "mute":
                                # [bool] the current mute state
                                pass
                            case "rate":
                                # rate: [float] the current playback rate, valid range (0..)
                                pass

                case "GetProperties":
                    await self.send(id=msg_id, result=self._properties)

    def set_user(self, user, startup=False):
        """
        Sets the current user and updates properties based on whether or
        not the plugin is able to control the user's playback
        (i.e. the playing user is the same as the api user).
        """
        if user == self.playing_user:
            return

        self.playing_user = user

        can_control = user == self.api_user

        self._properties["canGoNext"] = can_control
        self._properties["canGoPrevious"] = can_control
        self._properties["canPlay"] = can_control
        self._properties["canPause"] = can_control
        self._properties["canSeek"] = can_control
        self._properties["canControl"] = can_control

        if not startup:
            self.queue(self.notify_properties())

    async def update_status(self, playing, paused):
        match playing, paused:
            case True, True:
                status = PlaybackStatus.Paused
            case True, False:
                status = PlaybackStatus.Playing
            case _:
                status = PlaybackStatus.Stopped

        if self._properties["playbackStatus"] != status:
            await self.set_property("playbackStatus", status)

    async def update_volume(self, volume):
        percent = (volume * 100) // 65535
        await self.set_property("volume", percent)

    @needs_spotify_token()
    def get_track_info(self, item):
        """Gets metadata for a 'track' item."""
        task = self.run_in_executor(self._query_track, item)
        task.add_done_callback(self._set_metadata)

    @needs_spotify_token()
    def get_episode_info(self, item):
        """Gets metadata for an 'episode' item."""
        task = self.run_in_executor(self._query_episode, item)
        task.add_done_callback(self._set_metadata)

    @needs_spotify_token()
    def get_audiobook_info(self, item):
        """Gets metadata for an 'audiobook' item."""
        task = self.run_in_executor(self._query_audiobook, item)
        task.add_done_callback(self._set_metadata)

    def _query_track(self, item):
        """API call to query spotify database for track and returns metadata."""
        track = self.spotify.track(item)

        return item, {
            "title": track["name"],
            "artist": [artist["name"] for artist in track["artists"]],
            "album": track["album"]["name"],
            "artUrl": track["album"]["images"][0]["url"],
            "trackId": item,
        }

    def _query_episode(self, item):
        """API call to query spotify database for episode and returns metadata."""
        episode = self.spotify.episode(item)

        return item, {
            "title": episode["name"],
            "artist": [episode["show"]["name"]],
            "album": "",
            "artUrl": episode["images"][0]["url"],
            "trackId": item,
        }

    def _query_audiobook(self, item):
        """API call to query spotify database for audiobook and returns metadata."""
        audiobook = self.spotify.audiobook(item)

        return item, {
            "title": audiobook["name"],
            "artist": [author["name"] for author in audiobook["authors"]],
            "album": "",
            "artUrl": audiobook["images"][0]["url"],
            "trackId": item,
        }

    def _set_metadata(self, task):
        """Updates metadata and sends updated properties to snapcast."""
        if exc := task.exception():
            self.queue(self.error(f"Exception retrievinging spotify info: {exc}"))
            return

        item, metadata = task.result()

        self.track_cache[item] = metadata
        self.queue(self.set_now_playing(item))

    async def set_property(self, name, value, send_metadata=False):
        """Updates property value and sends properties to snapcast."""
        self._properties[name] = value
        await self.notify_properties(send_metadata=send_metadata)

    async def notify_properties(self, send_metadata=False):
        """Sends properties to snapcast."""
        params = self._properties.copy()
        if send_metadata:
            params["metadata"] = self._metadata.copy()
        await self.send(method="Plugin.Stream.Player.Properties", params=params)

    async def set_now_playing(self, item):
        """Gets metadata for currently playing track and sends data to snapcast."""
        self.now_playing = item
        self._metadata = self.track_cache[item]

        await self.notify_properties(send_metadata=True)

    @needs_spotify_token(error_return_value=(False, "Unable to validate API token."))
    def _send_spotify_command(self, cmd, *args, return_output=False, **kwargs):
        """Helper method to make api call to spotify."""
        try:
            func = getattr(self.spotify, cmd)
            output = func(*args, **kwargs)
            return output if return_output else True, None
        except SpotifyException as e:
            return False, f"{e.code} - {e.msg} - {e.reason}"

    def play(self):
        """Starts playback."""
        return self._send_spotify_command("start_playback")

    def stop(self):
        """Stops (pauses) playback."""
        return self._send_spotify_command("pause_playback")

    def pause(self):
        """Pauses playback."""
        return self._send_spotify_command("pause_playback")

    def play_pause(self):
        """Toggles playback status."""
        if self._properties["playbackStatus"] == PlaybackStatus.Playing:
            return self.pause()
        return self.play()

    def next_track(self):
        """Moves to next track."""
        return self._send_spotify_command("next_track")

    def previous_track(self):
        """Moves to previous track."""
        return self._send_spotify_command("previous_track")

    def seek(self, offset):
        """Seeks to current position + offset."""
        position, error = self._send_spotify_command(
            "current_user_playing_track", return_output=True
        )
        if not position:
            return False, error

        new_position = position["progress_ms"] + (offset * 1000)

        return self._send_spotify_command("seek_track", new_position)

    def set_position(self, pos):
        """Seeks to new position."""
        return self._send_spotify_command("seek_track", pos * 1000)


def check_file(value):
    """Make sure specified file exists."""
    if not Path(value).exists():
        raise argparse.ArgumentTypeError(f"File not found: {value}")
    return value


def vol(value):
    """Limit volume to a value between 0 and 100."""
    try:
        value = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Volume must be an integer.")

    return str(min(max(value, 0), 100))


def snapspot():
    parser = argparse.ArgumentParser(
        description="Script plugin for Snapcast server. Provides additional metadata for librespot streams."
    )

    # Arguments set by snapserver. We need to accept them but not show them in help.
    parser.add_argument("--stream", help=argparse.SUPPRESS)
    parser.add_argument("--snapcast-port", help=argparse.SUPPRESS)
    parser.add_argument("--snapcast-host", help=argparse.SUPPRESS)

    # Allow user to specify a different config file
    spotargs = parser.add_argument_group("snapspot args")
    spotargs.add_argument(
        "-c", "--config", type=check_file, help="Path to configuration file"
    )
    spotargs.add_argument(
        "--debug", action="store_true", help="Enable debugging."
    )

    # Spotify API arguments
    spotify = parser.add_argument_group("Spotity API args")
    spotify.add_argument("--client_id", help="Client ID for Spotify API")
    spotify.add_argument("--client_secret", help="Client secret for Spotify API")
    spotify.add_argument("--redirect_uri", help="Redirect URI for Spotify API")
    spotify.add_argument(
        "--cache_location",
        help=(
            "Location for storing Spotify API credentials. "
            "Must be writable by owner of snapserver process."
        ),
    )

    # Librespot configuration options
    librespot = parser.add_argument_group("Librespot configuration options")
    librespot.add_argument("--username", help="Username for librespot.")
    librespot.add_argument("--password", help="Password for librespot user.")
    librespot.add_argument("--devicename", help="Device name for librespot.")
    librespot.add_argument(
        "--bitrate",
        choices=["96", "160", "320"],
        default="320",
        help="Bitrate for audio stream.",
    )
    librespot.add_argument(
        "--volume", type=vol, help="Initial volume in percent %% [0-100]."
    )
    librespot.add_argument(
        "--onevent",
        type=check_file,
        help="Path to a script that gets run when one of librespot's events is triggered.",
    )
    normalize = librespot.add_mutually_exclusive_group()
    normalize.add_argument(
        "--enable_normalize",
        dest="normalize",
        action="store_const",
        const=True,
        help="Enable volume normalisation for librespot.",
    )
    normalize.add_argument(
        "--disable_normalize",
        dest="normalize",
        action="store_const",
        const=False,
        help="Disable volume normalisation for librespot.",
    )
    librespot.add_argument(
        "--autoplay",
        choices=["on", "off"],
        help="Autoplay similar songs when your music ends.",
    )
    librespot.add_argument(
        "--cache", help="Path to a directory where files will be cached."
    )
    cache = librespot.add_mutually_exclusive_group()
    cache.add_argument(
        "--enable_audio_cache",
        dest="disable_audio_cache",
        action="store_const",
        const=False,
        help="Enable caching of the audio data (disabled by default).",
    )
    cache.add_argument(
        "--disable_audio_cache",
        dest="disable_audio_cache",
        action="store_const",
        const=True,
        help="Disable caching of the audio data (disabled by default).",
    )
    librespot.add_argument(
        "--backend",
        help="Audio backend to use.",
    )
    librespot.add_argument(
        "--audio_device",
        help="Audio device to use.",
    )
    librespot.add_argument(
        "--params",
        nargs=argparse.REMAINDER,
        help=(
            "Optional string appended to the librespot invocation. "
            "Must be the last argument."
        ),
    )
    args = parser.parse_args()
    args_dict = vars(args)
    cmdline_args = {k: v for k, v in args_dict.items() if v is not None}

    # Load global settings from config file
    global_settings = {}
    cp = ConfigParser()

    if cp.read(Path(args.config or GLOBAL_CONFIG_PATH)):
        for section in ("spotify", "librespot"):
            if section in cp:
                for key, val in cp.items(section):
                    if not val:
                        continue
                    if key == "normalize":
                        global_settings[key] = cp.getboolean(
                            section, key, fallback=True
                        )
                    elif key == "disable_audio_cache":
                        global_settings[key] = cp.getboolean(
                            section, key, fallback=True
                        )
                    elif key == "volume":
                        global_settings = cp.getint(section, key, fallback=100)
                    elif key == "params":
                        global_settings[key] = shlex.split(val)
                    else:
                        global_settings[key] = val

        global_settings = {k: v for k, v in global_settings.items() if v}

    environment_settings = {
        "client_id": os.environ.get("SNAPSPOT_CLIENT_ID", None),
        "client_secret": os.environ.get("SNAPSPOT_CLIENT_SECRET", None),
    }
    environment_settings = {k: v for k, v in environment_settings.items() if v}

    # Session settings
    # Settings preference order:
    # 1: command line args
    # 2: environmental args
    # 3: global settings
    settings = {**global_settings, **environment_settings, **cmdline_args}
    if "config" in settings:
        del settings["config"]

    # Start the plugin
    try:
        plugin = SnapSpot(**settings)
    except SnapSpotError as e:
        sys.exit(f"Error. Cannot start snapspot: {e}")
    plugin.start()


if __name__ == "__main__":
    snapspot()
