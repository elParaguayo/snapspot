# SnapSpot
Script plugin for Snapcast to provide additional metadata from librespot

## Background
Snapcast's built-in librespot stream is great but it is only able to provide the
song title in the stream metadata. This limitation is because the librespot
app itself doesn't provide additional metadata.

SnapSpot is an alternative way to use librespot with Snapcast but with the benefit
of additional metadata by using Spotify's web API.

## How it works
SnapSpot is not a stream source for Snapcast. Instead, it is a stream control script.
However, unlike other control scripts, SnapSpot also runs the music player, reading its output
and sending metadata back to Snapcast.

## Installing
For now, installation is manual:
1. Copy the `snapspot.py` file the machine running snapserver
2. Ensure the file is executable
3. Copy ths `snapspot.ini` file to a folder that is readable by the user running the snapserver process
4. (Optional) edit the `GLOBAL_CONFIG_PATH` variable in `snapspot.py` to match the path from step 3.
5. Edit the config file (see below)
6. Create a stream in `/etc/snapserver.conf`:

```
pipe:///tmp/snapspotpipe?name=SnapSpot&mode=create&controlscript=<url_encoded_path_to_snapspot.py>
```

Notes:
* SnapSpot should be configured to set librespot's audio backend and output device.
Snapcast's stream source should then be set to match that output.
* If using a `pipe` source, the `mode` must be set to `create` as 
librespot expects the pipe to exist before it starts.

## Authorising Spotify Web API access
Firstly, you need to create your credentials for accessing the Spotify web API.
Please refer to the [Spotify docs](https://developer.spotify.com/documentation/web-api)
for details on how to create the client ID and client secret codes.

> ## To Do
>
> What's the best practice for authorising SnapSpot? The first time the app is run,
> it will open a web page to ask the user to authorise the app. However,
> this may not work on a headless server, in which case the app should be
> run on a separate machine and the credentials.json file copied to the
> server.

## Configuration
Configuring SnapSpot can be done in a couple of ways:
1. Using a configuration file
2. Passing command line arguments via `constrolscriptparams=` in `/etc/snapserver.conf'

Option 1 is preferred as option 2 would require all arguments to be url encoded.

The Spotify client ID and client secret can also be set by using the
`SNAPSPOT_CLIENT_ID` and `SNAPSPOT_CLIENT_SECRET` environmental variables.

The following configuration options are available:
|Option|Explanation|
|------|-----------|
|`-c`, `--config`| Path to a configuration file. (Not available in configuration file)|
|`--client_id`|Spotify web API client ID|
|`--client_secret`|Spotify web API client secret|
|`--redirect_uri`|Spotify web API authorisation redirect URI|
|`--credentials_location`|Path to save Spotify credentials|
|`--username`|Username for librespot. Leave blank to allow any user on network to connect|
|`--password`|Password for librespot user|
|`--name`|Librespot device name|
|`--bitrate`|Bitrate for audiostream (96, 160, 320) - default 320|
|`--volume`|Initial volume level (percent) - default 100|
|`--onevent`|Path to script to be run on librespot event|
|`--normalize`|Normalize volume|
|`--autoplay`|Autoplay similar songs when playlist finishes|
|`--cache`|Path to directory where files will be cached|
|`--disable_audio_cache`|Disable audio cache|
|`--backend`|Audio backend (e.g. `alsa`, `pipe`)|
|`--audio_device`|Audio device name (e.g. `/tmp/spotpipe`)|
|`--params`|Addition parameters to be passed to librespot (run `librespot -h` to see available options)|
