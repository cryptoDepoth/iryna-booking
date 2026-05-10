#!/usr/bin/env python3
"""Build a sanitized assistant knowledge file from an Instagram export zip.

The output is JSONL and is intended to live outside git, for example:
~/.pashynska-data/assistant_knowledge.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from zipfile import ZipFile

from bs4 import BeautifulSoup


OWN_SENDER_RE = re.compile(r"PASHYNSKA|PHOTOGRAPHY CALGARY|IRYNA", re.I)
EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://\S+")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_.]{2,30}")

TOPIC_KEYWORDS = {
    "deposit": ("deposit", "e-transfer", "etransfer", "payment", "paid", "remaining", "reserve"),
    "pricing": ("price", "cost", "$", "gst", "package", "rate"),
    "availability": ("available", "availability", "slot", "time", "date", "book", "reservation"),
    "weather": ("rain", "weather", "reschedule", "forecast", "wind", "snow", "cloudy"),
    "delivery": ("ready", "gallery", "retouch", "retouched", "original", "download", "link"),
    "location": ("location", "park", "studio", "parking", "address", "map"),
    "newborn": ("newborn", "baby"),
    "maternity": ("maternity", "pregnan"),
    "family": ("family", "kids", "children", "cake smash"),
    "wedding": ("wedding", "elopement", "engagement"),
    "style": ("wear", "dress", "outfit", "style", "guide", "clothes"),
}


def normalize(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "")


def default_output_path() -> Path:
    configured = os.environ.get("ASSISTANT_KNOWLEDGE_PATH")
    if configured:
        return Path(configured).expanduser()
    backup_dir = os.environ.get("BACKUP_DIR", "")
    if backup_dir.endswith("/backups"):
        return Path(backup_dir[:-8]).expanduser() / "assistant_knowledge.jsonl"
    return Path.home() / ".pashynska-data" / "assistant_knowledge.jsonl"


def direct_message_text(box) -> str:
    body = box.select_one("div._3-95._a6-p")
    if not body:
        return ""
    outer = next(iter(body.find_all("div", recursive=False)), None)
    if not outer:
        text = body.get_text(" ", strip=True)
    else:
        children = [c for c in outer.find_all("div", recursive=False)]
        # In Instagram exports the second direct child is the actual message;
        # later children are usually link previews or reactions.
        text = children[1].get_text("\n", strip=True) if len(children) >= 2 else outer.get_text("\n", strip=True)
    text = normalize(text)
    return re.sub(r"\s+", " ", text).strip()


def is_noise(text: str) -> bool:
    low = text.lower().strip()
    if not low:
        return True
    if low in {"liked a message", "❤️", "❤", "👍", "🙏", "✨"}:
        return True
    if low.startswith("reacted "):
        return True
    if "sent an attachment" in low:
        return True
    if "replied to an ad" in low or low.startswith("view ad"):
        return True
    if "we have received your message. thank you for contacting us" in low:
        return True
    if len(re.sub(r"[\W_]+", "", low)) < 8:
        return True
    return False


def topic_tags(*texts: str) -> list[str]:
    haystack = " ".join(texts).lower()
    tags = [
        topic
        for topic, keywords in TOPIC_KEYWORDS.items()
        if any(keyword in haystack for keyword in keywords)
    ]
    return tags or ["general"]


def redactor_for_client_names(names: set[str]):
    candidates: set[str] = set()
    for raw in names:
        name = normalize(raw).strip()
        if not name or OWN_SENDER_RE.search(name) or name.lower() == "instagram user":
            continue
        if 3 <= len(name) <= 60:
            candidates.add(name)
        for part in re.findall(r"[A-ZА-ЯІЇЄҐ][A-Za-zА-Яа-яІіЇїЄєҐґ'-]{2,}", name):
            if part.lower() not in {"instagram", "user", "photo", "photography", "calgary"}:
                candidates.add(part)
    patterns = [re.compile(r"\b" + re.escape(c) + r"\b", re.I) for c in sorted(candidates, key=len, reverse=True)]

    def redact(text: str) -> str:
        text = URL_RE.sub("[link]", text)
        text = EMAIL_RE.sub("[email]", text)
        text = HANDLE_RE.sub("[instagram]", text)
        text = PHONE_RE.sub("[phone]", text)
        for pattern in patterns[:10]:
            text = pattern.sub("[client]", text)
        text = re.sub(r"\b(Hi|Hello|Good morning|Good afternoon|Good evening)\s+\[client\]\s*,", r"\1,", text)
        return re.sub(r"\s+", " ", text).strip()

    return redact


def parse_conversation(zip_file: ZipFile, name: str) -> list[dict[str, str | bool]]:
    soup = BeautifulSoup(zip_file.read(name), "html.parser")
    messages = []
    for box in soup.select("div.pam._3-95._2ph-._a6-g.uiBoxWhite.noborder"):
        heading = box.find("h2")
        if not heading:
            continue
        sender = normalize(heading.get_text(" ", strip=True))
        text = direct_message_text(box)
        if is_noise(text):
            continue
        messages.append({
            "sender": sender,
            "own": bool(OWN_SENDER_RE.search(sender)),
            "text": text,
        })
    return messages


def build_pairs(zip_path: Path) -> list[dict[str, object]]:
    pairs: list[dict[str, object]] = []
    with ZipFile(zip_path) as zip_file:
        message_files = [n for n in zip_file.namelist() if n.endswith("message_1.html")]
        for name in message_files:
            messages = parse_conversation(zip_file, name)
            if not messages:
                continue
            client_names = {str(m["sender"]) for m in messages if not m["own"]}
            redact = redactor_for_client_names(client_names)
            chronological = list(reversed(messages))
            for idx, msg in enumerate(chronological):
                if not msg["own"] or idx == 0:
                    continue
                prev = chronological[idx - 1]
                if prev["own"]:
                    continue
                client_message = redact(str(prev["text"]))[:650]
                iryna_reply = redact(str(msg["text"]))[:1000]
                if is_noise(client_message) or is_noise(iryna_reply):
                    continue
                digest = hashlib.sha1(f"{name}:{idx}:{client_message}:{iryna_reply}".encode("utf-8")).hexdigest()[:16]
                pairs.append({
                    "id": digest,
                    "topics": topic_tags(client_message, iryna_reply),
                    "client_message": client_message,
                    "iryna_reply": iryna_reply,
                    "source_hash": hashlib.sha1(name.encode("utf-8")).hexdigest()[:12],
                })
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", type=Path, help="Path to Instagram export zip")
    parser.add_argument("-o", "--output", type=Path, default=default_output_path(), help="Output JSONL path")
    parser.add_argument("--max-pairs", type=int, default=0, help="Limit output pairs; 0 writes all")
    args = parser.parse_args()

    if not args.zip_path.exists():
        raise SystemExit(f"Zip file not found: {args.zip_path}")

    pairs = build_pairs(args.zip_path)
    if args.max_pairs > 0:
        pairs = pairs[: args.max_pairs]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")

    topic_counts: dict[str, int] = {}
    for pair in pairs:
        for topic in pair.get("topics") or []:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1

    print(f"Wrote {len(pairs)} sanitized Q/A pairs to {args.output}")
    print("Top topics:", sorted(topic_counts.items(), key=lambda row: row[1], reverse=True)[:10])
    print("Raw Instagram archive was not copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
