"""Locations and categories library for the Telegram Spy Game."""

import random

LOCATIONS = [
    {"name": "🏥 مستشفى", "word": "مستشفى"},
    {"name": "✈️ مطار", "word": "مطار"},
    {"name": "🏫 مدرسة", "word": "مدرسة"},
    {"name": "🍕 مطعم", "word": "مطعم"},
    {"name": "🎬 سينما", "word": "سينما"},
    {"name": "⚽ ملعب كرة قدم", "word": "ملعب"},
    {"name": "🏖️ شاطئ البحر", "word": "شاطئ"},
    {"name": "🏛️ متحف", "word": "متحف"},
    {"name": "🥐 مخبز", "word": "مخبز"},
    {"name": "🛸 مركبة فضائية", "word": "فضاء"},
    {"name": "🚢 سفينة ركاب", "word": "سفينة"},
    {"name": "🏨 فندق", "word": "فندق"},
    {"name": "🛒 سوبرماركت", "word": "سوبرماركت"},
    {"name": "🏋️ نادي رياضي", "word": "جيم"},
    {"name": "🎪 سيرك", "word": "سيرك"},
]


def get_random_location() -> dict[str, str]:
    """Return a random secret location dict."""
    return random.choice(LOCATIONS)


def get_location_options(secret_word: str, count: int = 4) -> list[str]:
    """Return a list of location options including the secret_word for Spy guessing."""
    all_words = [loc["word"] for loc in LOCATIONS]
    other_words = [w for w in all_words if w != secret_word]
    random.shuffle(other_words)
    selected = [secret_word] + other_words[: count - 1]
    random.shuffle(selected)
    return selected
