# 🎬 Blob Cinema

**A full-length film playing purely from chained encrypted blobs. Zero video bytes on the page.**

The page holds only two things: the first blob's ID and one decryption key. Everything else — all 78 sections of the film — is pulled from the [Walrus](https://walrus.xyz) decentralized storage network, decrypted in your browser, hash-verified, and streamed into the video player in real time.

**No blob, no movie.** That's not a slogan, it's the architecture.

## How it works

```
┌─────────┐     ┌──────────┐     ┌──────────┐         ┌──────────┐
│  Page   │────▶│ Blob 1   │────▶│ Blob 2   │───...──▶│ Blob 78  │
│BLOB0+KEY│     │decrypt   │     │derive key│         │          │
│  only   │     │verify    │     │decrypt   │         │  THE END │
└─────────┘     │next→     │     │verify    │         └──────────┘
                └──────────┘     │next→     │
                                 └──────────┘
```

1. **Section** — The film (fragmented MP4) is cut into ~1 MB sections, aligned to MP4 fragment boundaries (never splitting a `moof`/`mdat` pair).
2. **Encrypt** — Each section is encrypted with NaCl `secretbox`. Keys chain: `key_{n+1} = SHA256(key_n || "|next")`.
3. **Link** — Each section's header embeds the *next* section's blob ID (leader → follower). Upload in reverse so the links resolve.
4. **Play** — The browser fetches section 1, decrypts it, verifies its SHA-256, appends it to a `MediaSource`, derives the next key, and follows the chain. Playback starts while later sections are still being fetched.

The viewer sees every step: a timeline strip lights up per section, a log shows each fetch/decrypt/verify, and a "break the chain" button proves the negative — substitute a fake blob ID and the film halts.

## Repository layout

```
blob-cinema/
├── chain/
│   └── build_chain.py      # Cut → encrypt → upload → verify (the builder)
├── player/
│   └── cinema.html         # The zero-video-bytes player page
├── requirements.txt
├── LICENSE                 # MIT
└── README.md
```

## Quick start

### 1. Prepare your film as fragmented MP4

```bash
ffmpeg -i input.mp4 -c copy -movflags frag_keyframe+empty_moov+default_base_moof film-fmp4.mp4
```

> ⚠️ **Audio matters.** Use stereo AAC (`-c:a aac -ac 2`). We learned the hard way: multi-channel audio with a mismatched channel configuration is silently rejected by browser demuxers. The video will appear fine; the audio track will kill the entire init segment.

### 2. Build the chain

```bash
pip install -r requirements.txt
python3 chain/build_chain.py --input film-fmp4.mp4 --manifest chain.json --epochs 5
```

This uploads each encrypted section to the Walrus testnet and writes:
- `chain.json` — **PRIVATE.** All blob IDs, all keys, all hashes. Never publish this.
- `chain-ticket.json` — Public. Only the first blob ID + first key. This is what the player page needs.

### 3. Point the player at your chain

Edit `player/cinema.html`:
```js
const BLOB0 = '...';  // from chain-ticket.json
const KEY0 = '...';   // from chain-ticket.json
const NSEC = 78;      // section count
```

Open it. Press play. Watch the film assemble itself from the network.

## The proof

- **View source.** The page contains zero video bytes. Just `BLOB0`, `KEY0`, and the player logic.
- **Watch the timeline.** Each block turns green as its section is fetched, decrypted, and hash-verified.
- **Break the chain.** The red button substitutes a nonexistent blob ID. Playback halts at that section. No blob, no movie.

## Design notes

- **Pure-JS crypto.** The page inlines TweetNaCl and a FIPS 180-4 SHA-256 implementation because `crypto.subtle` is unavailable on HTTP pages. No dependencies, no CDN.
- **iPhone compatible.** Uses `ManagedMediaSource` when available, falls back to `MediaSource`. Fragmented MP4 is required — progressive MP4 appends are rejected by mobile demuxers.
- **Testnet expiry.** Walrus testnet epochs are 1 day; blobs stored for 5 epochs dissolve after ~5 days. The page includes a self-destruct countdown. For permanence, use Walrus mainnet.
- **Keys are demo-only.** The test keys in this repo are for the public demo. For anything real, generate fresh keys and keep the manifest private.

## License

MIT. See [LICENSE](LICENSE). Built live with [Muse](https://muse.ai).
