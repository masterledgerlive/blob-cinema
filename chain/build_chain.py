#!/usr/bin/env python3
"""Blob Cinema chain builder: fragment-aligned sections -> encrypt -> Walrus.

Takes a fragmented-MP4 file, cuts it into fragment-aligned sections (never
splitting a moof/mdat pair), encrypts each section with NaCl secretbox using
chained keys, and uploads them to Walrus.

Chain design:
- Each section = JSON header + newline + encrypted media bytes.
- Header contains: section number, total count, SHA-256 of the media,
  and the NEXT section's blob ID (leader -> follower link).
- Keys chain: key_{n+1} = SHA256(key_n || "|next").
- Upload in REVERSE (last section first) so each section can embed the
  next section's blob ID.
- The page needs only the FIRST blob ID and FIRST key. Everything else
  is discovered by walking the chain.

Resumable: the manifest is checkpointed after every upload. Re-running
skips sections that already have blob IDs.

Usage:
    python3 build_chain.py --input film-fmp4.mp4 --manifest chain.json --epochs 5

Requirements:
    pip install pynacl requests

    The input must be a fragmented MP4 (fMP4) with an empty moov:
        ffmpeg -i input.mp4 -c copy -movflags frag_keyframe+empty_moov+default_base_moof film-fmp4.mp4

    IMPORTANT: Use stereo AAC audio. Multi-channel audio with mismatched
    channel configuration will be rejected by browser demuxers.
    (We learned this the hard way: 6-channel audio labeled as stereo
    fails silently in Chromium's MSE parser.)
"""
import argparse
import hashlib
import json
import os
import sys

from nacl.secret import SecretBox
from nacl.utils import random as nacl_random

import requests

PUBLISHER = "https://publisher.walrus-testnet.walrus.space"


def H(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def derive_key(prev: bytes, label: str) -> bytes:
    return H(prev + b"|" + label.encode())


def put_blob(cipher: bytes, epochs: int) -> str:
    r = requests.put(f"{PUBLISHER}/v1/blobs?epochs={epochs}", data=cipher, timeout=180)
    r.raise_for_status()
    info = r.json()
    blob_id = (info.get("newlyCreated") or info.get("alreadyCertified") or {}).get("blobObject", {}).get("blobId")
    if not blob_id:
        raise RuntimeError(f"unexpected publisher response: {json.dumps(info)[:200]}")
    return blob_id


def parse_boxes(data: bytes):
    """Yield (type, offset, size) for top-level boxes."""
    off = 0
    while off + 8 <= len(data):
        size = int.from_bytes(data[off:off + 4], "big")
        typ = data[off + 4:off + 8]
        if size < 8:
            break
        yield typ, off, size
        if typ == b"mdat":
            # mdat can be huge; skip to end
            off += size
        else:
            off += size


def find_sections(data: bytes, target_size: int = 1024 * 1024):
    """Cut data into fragment-aligned sections (never split moof/mdat pairs).

    Returns a list of (start, end) byte ranges. Section 1 starts at 0 and
    includes ftyp+moov (the init segment) plus complete fragment pairs.
    """
    # Find all moof offsets
    moofs = []
    off = 0
    while off + 8 <= len(data):
        size = int.from_bytes(data[off:off + 4], "big")
        typ = data[off + 4:off + 8]
        if size < 8:
            break
        if typ == b"moof":
            moofs.append(off)
        off += size
        if off > len(data):
            break

    if not moofs:
        raise ValueError("no moof boxes found - is this a fragmented MP4?")

    # Group moofs into sections of ~target_size, aligning to moof boundaries.
    # Each section ends at the start of a moof (so moof+mdat stay together).
    sections = []
    sec_start = 0
    i = 0
    while i < len(moofs):
        # Accumulate moofs until we hit target size
        j = i
        while j < len(moofs):
            # Section would run from sec_start to moofs[j+1] (or EOF)
            end = moofs[j + 1] if j + 1 < len(moofs) else len(data)
            if end - sec_start >= target_size and j > i:
                break
            j += 1
        end = moofs[j] if j < len(moofs) else len(data)
        # Ensure we include the mdat following the last moof
        # (find the end by scanning to the next moof or EOF)
        if j < len(moofs):
            # end is at moofs[j], which is the start of the next section
            pass
        else:
            end = len(data)
        sections.append((sec_start, end))
        sec_start = end
        i = j
        if i >= len(moofs):
            break

    # Handle trailing data after last moof (mfra etc.) - fold into last section
    if sections and sections[-1][1] < len(data):
        s, e = sections[-1]
        sections[-1] = (s, len(data))

    return sections


def main():
    ap = argparse.ArgumentParser(description="Build an encrypted Walrus blob chain from fMP4")
    ap.add_argument("--input", required=True, help="fragmented MP4 file")
    ap.add_argument("--manifest", required=True, help="output manifest JSON (private - contains all keys)")
    ap.add_argument("--epochs", type=int, default=5, help="Walrus storage epochs")
    ap.add_argument("--target-size", type=int, default=1024 * 1024, help="target section size in bytes")
    args = ap.parse_args()

    data = open(args.input, "rb").read()
    sections = find_sections(data, args.target_size)
    n_sec = len(sections)
    full_sha = hashlib.sha256(data).hexdigest()
    print(f"fMP4 {len(data)} bytes -> {n_sec} fragment-aligned sections, sha {full_sha[:16]}...")

    if os.path.exists(args.manifest):
        m = json.load(open(args.manifest))
        print(f"resuming: {sum(1 for s in m['sections'] if s['blob_id'])}/{n_sec} already uploaded")
        master = bytes.fromhex(m["master_hex"])
    else:
        master = nacl_random(32)
        m = {
            "name": os.path.basename(args.input),
            "bytes": len(data),
            "full_sha256": full_sha,
            "master_hex": master.hex(),
            "sections": [
                {"n": i + 1, "blob_id": None, "key_hex": None,
                 "media_sha256": None, "bytes": e - s}
                for i, (s, e) in enumerate(sections)
            ],
        }

    # Derive all keys upfront (deterministic from master)
    keys = [master]
    for i in range(1, n_sec):
        keys.append(derive_key(keys[-1], "next"))

    # Upload in reverse so each section can embed the next blob ID
    for idx in range(n_sec - 1, -1, -1):
        sec = m["sections"][idx]
        if sec["blob_id"]:
            continue
        s, e = sections[idx]
        media = data[s:e]
        media_sha = hashlib.sha256(media).hexdigest()

        next_blob = m["sections"][idx + 1]["blob_id"] if idx + 1 < n_sec else None
        header = {
            "n": idx + 1,
            "total": n_sec,
            "proof": {"section_sha256": media_sha},
            "next_blob": next_blob,
        }
        header_bytes = json.dumps(header, separators=(",", ":")).encode() + b"\n"
        plaintext = header_bytes + media

        box = SecretBox(keys[idx])
        nonce = nacl_random(24)
        cipher = nonce + box.encrypt(plaintext, nonce).ciphertext

        blob_id = put_blob(cipher, args.epochs)
        sec["blob_id"] = blob_id
        sec["key_hex"] = keys[idx].hex()
        sec["media_sha256"] = media_sha

        # Checkpoint
        json.dump(m, open(args.manifest, "w"), indent=1)
        print(f"  section {idx + 1}/{n_sec}: {blob_id[:16]}... ({len(cipher)} bytes)")

    # Write first-trigger ticket (public: only first blob + first key)
    ticket = {
        "name": m["name"],
        "blob_id": m["sections"][0]["blob_id"],
        "key_hex": m["sections"][0]["key_hex"],
        "sections": n_sec,
        "full_sha256": full_sha,
    }
    ticket_path = args.manifest.replace(".json", "-ticket.json")
    json.dump(ticket, open(ticket_path, "w"), indent=1)
    print(f"\nDone. Ticket: {ticket_path}")
    print(f"BLOB0={ticket['blob_id']}")
    print(f"KEY0={ticket['key_hex']}")
    print(f"NSEC={n_sec}")


if __name__ == "__main__":
    main()
