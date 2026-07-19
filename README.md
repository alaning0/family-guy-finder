# Family Guy Episode Finder

Search Family Guy episodes by anything that happens in them — main plot or the
"Meanwhile…" subplot — and open the episode directly in Disney+.

**Live:** https://alaning0.github.io/family-guy-finder/

- On **mobile**, the ▶ button uses a `disneyplus://` deep link that opens the episode
  in the Disney+ app.
- On **desktop**, it opens the Disney+ web player in a new tab.
- 🎲 picks a random episode.

## How it works

`docs/` is a static page served by GitHub Pages. `docs/episodes.json` is built by
merging two public sources on (season, episode):

- **JustWatch** (GraphQL) — per-episode Disney+ playback IDs for the configured country
- **Wikipedia** — full episode-article Plot sections where they exist, otherwise the
  season page's episode summary

## Refreshing the data (new episodes)

```sh
python3 build/build.py            # add --country au etc. if your Disney+ region changes
git commit -am "refresh dataset" && git push
```

The build caches API responses under `build/cache/` (gitignored); use `--refresh` to
force refetching everything.

## Credits

Plot text from [Wikipedia](https://en.wikipedia.org/wiki/List_of_Family_Guy_episodes),
licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Streaming
links via JustWatch. This is an unofficial fan tool, not affiliated with Disney or Fox.
