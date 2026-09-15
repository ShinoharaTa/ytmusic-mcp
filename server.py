"""Local stdio MCP server for organizing YouTube Music with ytmusicapi.

Auth: browser-header credentials (see README.md). The credential file is never
read at import time, so the server starts and reports a clear error instead of
crashing when it is missing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from ytmusicapi import YTMusic

SERVER_ROOT = Path(__file__).resolve().parent
DEFAULT_AUTH_FILE = SERVER_ROOT / "browser.json"

# Destructive capability is opt-in: delete_playlist is only registered when
# YTMUSIC_MCP_ALLOW_DELETE is truthy.
ALLOW_DELETE = os.environ.get("YTMUSIC_MCP_ALLOW_DELETE", "").lower() in {"1", "true", "yes"}
# Guard against a runaway bulk edit from a single tool call.
MAX_BULK_ITEMS = int(os.environ.get("YTMUSIC_MCP_MAX_BULK", "100"))

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

mcp = MCPServer(
    "YouTube Music Organizer",
    version="0.1.0",
    instructions=(
        "Read and organize the signed-in user's YouTube Music account: playlists, "
        "library and liked songs. Tools whose names start with get_/list_/search are "
        "read-only. Write tools (create_playlist, add_songs_to_playlist, "
        "remove_songs_from_playlist, move_playlist_item, edit_playlist_details, "
        "rate_song) change the account and should only run when the user asked for "
        "that change. Always confirm the target playlist by listing it first: "
        "playlist IDs are not guessable. Removing songs and reordering need the "
        "setVideoId values returned by get_playlist, so call get_playlist first. "
        "To grow a playlist with similar music, start from a song the user likes "
        "and chain get_song_radio -> get_related_songs -> add_songs_to_playlist; "
        "search only finds titles the user already named."
    ),
)


# --------------------------------------------------------------------------- client


_client: YTMusic | None = None


def client() -> YTMusic:
    """Return a cached authenticated YTMusic client."""
    global _client
    if _client is not None:
        return _client

    configured = os.environ.get("YTMUSIC_AUTH_FILE")
    path = Path(configured).expanduser() if configured else DEFAULT_AUTH_FILE
    if not path.is_file():
        raise ToolError(
            f"Authentication file not found: {path}. Create it with the browser "
            "headers described in README.md, or point YTMUSIC_AUTH_FILE at it."
        )
    try:
        _client = YTMusic(str(path))
    except Exception as error:  # noqa: BLE001 - surface a usable message, not a traceback
        raise ToolError(
            f"Could not authenticate with {path} ({type(error).__name__}: {error}). "
            "The browser session may have expired; re-copy the request headers."
        ) from None
    return _client


def call(operation, what: str):
    """Run a ytmusicapi call, converting failures into tool errors."""
    try:
        return operation()
    except ToolError:
        raise
    except Exception as error:  # noqa: BLE001
        raise ToolError(f"{what} failed ({type(error).__name__}: {error}).") from None


# --------------------------------------------------------------------------- shaping

# ytmusicapi returns large dicts (thumbnails, menu tokens, feedback tokens...).
# Everything below trims them to what a language model actually needs.


def artists_of(item: dict[str, Any]) -> list[str]:
    return [a["name"] for a in (item.get("artists") or []) if a.get("name")]


def album_of(item: dict[str, Any]) -> str | None:
    album = item.get("album")
    if isinstance(album, dict):
        return album.get("name")
    return album if isinstance(album, str) else None


def track(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "videoId": item.get("videoId"),
        "title": item.get("title"),
        "artists": artists_of(item),
        "album": album_of(item),
        "duration": item.get("duration"),
    }
    # Needed to remove or reorder this item inside a playlist.
    if item.get("setVideoId"):
        out["setVideoId"] = item["setVideoId"]
    if item.get("likeStatus"):
        out["likeStatus"] = item["likeStatus"]
    return out


def playlist_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "playlistId": item.get("playlistId"),
        "title": item.get("title"),
        "count": item.get("count"),
        "description": item.get("description"),
        "author": (item.get("author") or {}).get("name")
        if isinstance(item.get("author"), dict)
        else item.get("author"),
    }


def normalize_playlist_id(value: str) -> str:
    """Accept a raw ID or a music.youtube.com URL."""
    value = value.strip()
    if not value:
        raise ToolError("playlist_id must not be blank.")
    if "list=" in value:
        value = value.split("list=", 1)[1].split("&", 1)[0]
    return value


def check_bulk(video_ids: list[str], verb: str) -> list[str]:
    ids = [v.strip() for v in video_ids if v and v.strip()]
    if not ids:
        raise ToolError("No video IDs were given.")
    if len(ids) > MAX_BULK_ITEMS:
        raise ToolError(
            f"Refusing to {verb} {len(ids)} items in one call (limit {MAX_BULK_ITEMS}). "
            "Split the request into smaller batches and confirm with the user."
        )
    return ids


# --------------------------------------------------------------------------- read tools


@mcp.tool(title="Search YouTube Music", annotations=READ_ONLY)
def search(
    query: str,
    filter: Literal["songs", "videos", "albums", "artists", "playlists", "community_playlists", "featured_playlists"]
    | None = "songs",
    limit: int = 10,
) -> dict[str, Any]:
    """Search YouTube Music and return matches with the videoIds needed to add them to a playlist."""
    if not query.strip():
        raise ToolError("query must not be blank.")
    results = call(lambda: client().search(query.strip(), filter=filter, limit=limit), "Search")
    shaped = []
    for item in results[:limit]:
        entry = track(item)
        # Artist hits carry their name under "artist" and have no "title".
        if not entry["title"] and item.get("artist"):
            entry["title"] = item["artist"]
        entry["resultType"] = item.get("resultType")
        if item.get("browseId"):
            entry["browseId"] = item["browseId"]
        if item.get("playlistId"):
            entry["playlistId"] = item["playlistId"]
        shaped.append(entry)
    return {"query": query, "count": len(shaped), "results": shaped}


@mcp.tool(title="List my playlists", annotations=READ_ONLY)
def list_playlists(limit: int = 50) -> dict[str, Any]:
    """List the playlists in the signed-in account's library, with their playlistIds."""
    results = call(lambda: client().get_library_playlists(limit=limit), "Listing playlists")
    return {"count": len(results), "playlists": [playlist_summary(p) for p in results]}


@mcp.tool(title="Get a playlist's tracks", annotations=READ_ONLY)
def get_playlist(playlist_id: str, limit: int = 100) -> dict[str, Any]:
    """Read one playlist's metadata and tracks. Returns setVideoId per track, required to remove or reorder it."""
    pid = normalize_playlist_id(playlist_id)
    data = call(lambda: client().get_playlist(pid, limit=limit), f"Reading playlist {pid}")
    tracks = [track(t) for t in (data.get("tracks") or [])]
    return {
        "playlistId": data.get("id", pid),
        "title": data.get("title"),
        "description": data.get("description"),
        "privacy": data.get("privacy"),
        "trackCount": data.get("trackCount"),
        "returned": len(tracks),
        "tracks": tracks,
    }


@mcp.tool(title="Get my liked songs", annotations=READ_ONLY)
def get_liked_songs(limit: int = 100) -> dict[str, Any]:
    """Read the account's 'Liked Music' (thumbs-up) playlist."""
    data = call(lambda: client().get_liked_songs(limit=limit), "Reading liked songs")
    tracks = [track(t) for t in (data.get("tracks") or [])]
    return {"trackCount": data.get("trackCount"), "returned": len(tracks), "tracks": tracks}


@mcp.tool(title="Get my library songs", annotations=READ_ONLY)
def get_library_songs(
    limit: int = 100,
    order: Literal["a_to_z", "z_to_a", "recently_added"] | None = None,
) -> dict[str, Any]:
    """Read the songs saved to the account's library."""
    results = call(lambda: client().get_library_songs(limit=limit, order=order), "Reading library songs")
    return {"count": len(results), "tracks": [track(t) for t in results]}


@mcp.tool(title="Get my library albums", annotations=READ_ONLY)
def get_library_albums(limit: int = 50) -> dict[str, Any]:
    """Read the albums saved to the account's library."""
    results = call(lambda: client().get_library_albums(limit=limit), "Reading library albums")
    return {
        "count": len(results),
        "albums": [
            {
                "browseId": a.get("browseId"),
                "playlistId": a.get("playlistId"),
                "title": a.get("title"),
                "artists": artists_of(a),
                "year": a.get("year"),
                "type": a.get("type"),
            }
            for a in results
        ],
    }


@mcp.tool(title="Show the signed-in account", annotations=READ_ONLY)
def whoami() -> dict[str, Any]:
    """Report which YouTube Music account the credentials belong to."""
    info = call(lambda: client().get_account_info(), "Reading account info")
    return {"accountName": info.get("accountName"), "channelHandle": info.get("channelHandle")}


# --------------------------------------------------------------------------- discovery tools

# These drive the "grow a playlist" workflow: start from one song the user likes,
# pull YouTube Music's own recommendations, then feed the videoIds to
# add_songs_to_playlist. All read-only.


@mcp.tool(title="Get a song or playlist radio", annotations=READ_ONLY)
def get_song_radio(
    video_id: str | None = None,
    playlist_id: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Expand one song (or an existing playlist) into YouTube Music's radio queue of similar tracks. Returns relatedBrowseId for use with get_related_songs."""
    if bool(video_id) == bool(playlist_id):
        raise ToolError("Give exactly one of video_id or playlist_id.")
    pid = normalize_playlist_id(playlist_id) if playlist_id else None
    data = call(
        lambda: client().get_watch_playlist(
            videoId=video_id.strip() if video_id else None,
            playlistId=pid,
            radio=True,
            limit=limit,
        ),
        "Building radio",
    )
    tracks = [track(t) for t in (data.get("tracks") or [])][:limit]
    return {
        "seed": {"videoId": video_id, "playlistId": pid},
        "returned": len(tracks),
        "relatedBrowseId": data.get("related"),
        "tracks": tracks,
    }


@mcp.tool(title="Get songs related to a song", annotations=READ_ONLY)
def get_related_songs(browse_id: str, max_per_section: int = 10) -> dict[str, Any]:
    """Get YouTube Music's related content for a song: similar songs, other performances, similar artists, recommended playlists. browse_id comes from get_song_radio's relatedBrowseId."""
    if not browse_id.strip():
        raise ToolError("browse_id must not be blank.")
    sections = call(lambda: client().get_song_related(browse_id.strip()), "Reading related songs")

    shaped: list[dict[str, Any]] = []
    about: str | None = None
    for section in sections:
        contents = section.get("contents")
        # The "About the artist" section returns a long biography string rather
        # than a list; keep a short excerpt instead of flooding the response.
        if isinstance(contents, str):
            about = contents[:500]
            continue
        items = []
        for entry in (contents or [])[:max_per_section]:
            if not isinstance(entry, dict):
                continue
            if entry.get("videoId"):
                items.append(track(entry))
            elif entry.get("playlistId") and not entry.get("videoId"):
                items.append(
                    {
                        "playlistId": entry["playlistId"],
                        "title": entry.get("title"),
                        "description": entry.get("description"),
                    }
                )
            elif entry.get("browseId"):
                items.append(
                    {
                        "browseId": entry["browseId"],
                        "title": entry.get("title"),
                        "type": entry.get("type"),
                        "year": entry.get("year"),
                        "subscribers": entry.get("subscribers"),
                    }
                )
        if items:
            shaped.append({"title": section.get("title"), "items": items})

    return {"browseId": browse_id.strip(), "sections": shaped, "aboutTheArtist": about}


@mcp.tool(title="Get an artist's top songs and albums", annotations=READ_ONLY)
def get_artist_overview(channel_id: str) -> dict[str, Any]:
    """Get an artist's popular songs, albums, singles and related artists. channel_id is a browseId from search(filter='artists')."""
    if not channel_id.strip():
        raise ToolError("channel_id must not be blank.")
    data = call(lambda: client().get_artist(channel_id.strip()), f"Reading artist {channel_id}")

    def section(key: str) -> list[dict[str, Any]]:
        value = data.get(key)
        return (value.get("results") or []) if isinstance(value, dict) else []

    def release(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "browseId": item.get("browseId"),
            "playlistId": item.get("audioPlaylistId"),
            "title": item.get("title"),
            "year": item.get("year"),
            "type": item.get("type"),
        }

    description = data.get("description")
    return {
        "channelId": data.get("channelId", channel_id.strip()),
        "name": data.get("name"),
        "description": description[:500] if isinstance(description, str) else None,
        "subscribers": data.get("subscribers"),
        "monthlyListeners": data.get("monthlyListeners"),
        "radioId": data.get("radioId"),
        "topSongs": [track(t) for t in section("songs")],
        "albums": [release(a) for a in section("albums")],
        "singles": [release(s) for s in section("singles")],
        "relatedArtists": [
            {"browseId": r.get("browseId"), "name": r.get("title"), "subscribers": r.get("subscribers")}
            for r in section("related")
        ],
    }


@mcp.tool(title="Get my listening history", annotations=READ_ONLY)
def get_history(limit: int = 50) -> dict[str, Any]:
    """Read the account's recently played songs, newest first."""
    results = call(lambda: client().get_history(), "Reading listening history")
    tracks = []
    for item in results[:limit]:
        entry = track(item)
        if item.get("played"):
            entry["played"] = item["played"]
        tracks.append(entry)
    return {"returned": len(tracks), "tracks": tracks}


@mcp.tool(title="Get my taste profile", annotations=READ_ONLY)
def get_taste_profile() -> dict[str, Any]:
    """Read the artists YouTube Music associates with this account's taste."""
    profile = call(lambda: client().get_tasteprofile(), "Reading taste profile")
    return {"count": len(profile), "artists": sorted(profile.keys())}


# --------------------------------------------------------------------------- write tools


@mcp.tool(title="Create a playlist", annotations=WRITE)
def create_playlist(
    title: str,
    description: str = "",
    privacy_status: Literal["PRIVATE", "PUBLIC", "UNLISTED"] = "PRIVATE",
    video_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new playlist in the account, optionally seeded with songs. Defaults to private."""
    if not title.strip():
        raise ToolError("title must not be blank.")
    ids = check_bulk(video_ids, "seed a new playlist with") if video_ids else None
    result = call(
        lambda: client().create_playlist(title.strip(), description, privacy_status=privacy_status, video_ids=ids),
        "Creating playlist",
    )
    if not isinstance(result, str):
        raise ToolError(f"YouTube Music rejected the playlist creation: {result}")
    return {
        "playlistId": result,
        "title": title.strip(),
        "privacyStatus": privacy_status,
        "seeded": len(ids) if ids else 0,
        "url": f"https://music.youtube.com/playlist?list={result}",
    }


@mcp.tool(title="Add songs to a playlist", annotations=WRITE)
def add_songs_to_playlist(
    playlist_id: str,
    video_ids: list[str],
    allow_duplicates: bool = False,
) -> dict[str, Any]:
    """Append songs to an existing playlist by videoId. Get videoIds from `search`."""
    pid = normalize_playlist_id(playlist_id)
    ids = check_bulk(video_ids, "add")
    result = call(
        lambda: client().add_playlist_items(pid, videoIds=ids, duplicates=allow_duplicates),
        f"Adding songs to playlist {pid}",
    )
    status = result.get("status") if isinstance(result, dict) else str(result)
    added = result.get("playlistEditResults", []) if isinstance(result, dict) else []
    return {"playlistId": pid, "requested": len(ids), "added": len(added) or None, "status": status}


@mcp.tool(title="Remove songs from a playlist", annotations=WRITE)
def remove_songs_from_playlist(playlist_id: str, video_ids: list[str]) -> dict[str, Any]:
    """Remove songs from a playlist by videoId. Only removes tracks that are actually present."""
    pid = normalize_playlist_id(playlist_id)
    ids = set(check_bulk(video_ids, "remove"))

    data = call(lambda: client().get_playlist(pid, limit=None), f"Reading playlist {pid}")
    matched = [
        {"videoId": t["videoId"], "setVideoId": t["setVideoId"]}
        for t in (data.get("tracks") or [])
        if t.get("videoId") in ids and t.get("setVideoId")
    ]
    if not matched:
        raise ToolError(
            f"None of the given videoIds are in playlist {pid} (or the playlist is not "
            "editable by this account, so no setVideoId was returned)."
        )

    call(lambda: client().remove_playlist_items(pid, matched), f"Removing songs from playlist {pid}")
    missing = sorted(ids - {m["videoId"] for m in matched})
    return {"playlistId": pid, "removed": len(matched), "notInPlaylist": missing}


@mcp.tool(title="Move a track within a playlist", annotations=WRITE)
def move_playlist_item(
    playlist_id: str,
    set_video_id: str,
    after_set_video_id: str | None = None,
) -> dict[str, Any]:
    """Reorder a playlist: move one track after another. Both IDs are setVideoId values from get_playlist. Omit after_set_video_id to move the track to the top."""
    pid = normalize_playlist_id(playlist_id)
    if not set_video_id.strip():
        raise ToolError("set_video_id must not be blank.")
    move: str | tuple[str, str] = (
        (set_video_id.strip(), after_set_video_id.strip()) if after_set_video_id else set_video_id.strip()
    )
    status = call(lambda: client().edit_playlist(pid, moveItem=move), f"Reordering playlist {pid}")
    return {"playlistId": pid, "moved": set_video_id, "after": after_set_video_id, "status": status}


@mcp.tool(title="Edit playlist title/description/privacy", annotations=WRITE)
def edit_playlist_details(
    playlist_id: str,
    title: str | None = None,
    description: str | None = None,
    privacy_status: Literal["PRIVATE", "PUBLIC", "UNLISTED"] | None = None,
) -> dict[str, Any]:
    """Rename a playlist or change its description or privacy. Does not touch its tracks."""
    pid = normalize_playlist_id(playlist_id)
    if title is None and description is None and privacy_status is None:
        raise ToolError("Nothing to change: give at least one of title, description, privacy_status.")
    status = call(
        lambda: client().edit_playlist(pid, title=title, description=description, privacyStatus=privacy_status),
        f"Editing playlist {pid}",
    )
    return {"playlistId": pid, "status": status}


@mcp.tool(title="Like or unlike a song", annotations=WRITE)
def rate_song(video_id: str, rating: Literal["LIKE", "DISLIKE", "INDIFFERENT"] = "LIKE") -> dict[str, Any]:
    """Set the thumbs-up/down state of a song. INDIFFERENT clears an existing rating."""
    if not video_id.strip():
        raise ToolError("video_id must not be blank.")
    call(lambda: client().rate_song(video_id.strip(), rating), f"Rating song {video_id}")
    return {"videoId": video_id.strip(), "rating": rating}


if ALLOW_DELETE:

    @mcp.tool(title="Delete a playlist", annotations=DESTRUCTIVE)
    def delete_playlist(playlist_id: str, confirm_title: str) -> dict[str, Any]:
        """Permanently delete a playlist. confirm_title must match the playlist's current title exactly."""
        pid = normalize_playlist_id(playlist_id)
        data = call(lambda: client().get_playlist(pid, limit=1), f"Reading playlist {pid}")
        actual = (data.get("title") or "").strip()
        if actual != confirm_title.strip():
            raise ToolError(
                f"confirm_title does not match. Playlist {pid} is titled {actual!r}. "
                "Refusing to delete."
            )
        status = call(lambda: client().delete_playlist(pid), f"Deleting playlist {pid}")
        return {"playlistId": pid, "title": actual, "status": status, "deleted": True}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
